"""PH07 re-verification (5fd58af): trusted-config workspace grant provisioning.

Covers the P0-3 closure in 5fd58af "feat: provision execution workspace
grants from trusted config":

- ``workspace_sources`` configuration parsing and rejection rules
  (invalid IDs, relative roots, non-Git sources);
- EXECUTE delegations auto-create and provision exactly one grant,
  repeated calls reuse the ACTIVE grant;
- PENDING grants recover through provisioning; PROVISIONING/FAILED/EXPIRED
  grants never get silently rebuilt or replaced;
- missing sources fail closed, and grant fields derive only from the
  trusted deployment configuration (no client-injected paths/policies).

Post-hoc test: no source changes, no commits.
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.registry import AgentRegistryService
from researchd.daemon.composition import DaemonConfig, compose_daemon
from researchd.domain.enums import DelegationState
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    DelegationRecord,
    ResearchRunRecord,
    WorkspaceGrantRecord,
    WorkspaceRecord,
    WorkspaceTransportRecord,
)
from researchd.workspace.contracts import (
    WorkspaceAccessMode,
    WorkspaceGrant,
    WorkspaceGrantState,
    WorkspaceSource,
    WorkspaceTransportKind,
)
from researchd.workspace.service import (
    WorkspaceDelegationError,
    WorkspaceDelegationService,
)
from researchd.workspace.transports import GitWorktreeTransport
from tests.integration.test_storage import assert_migration_matches_models, migrate

WORKSPACE_ID = "ws_ph07"
RUN_ID = "run_ph07_grant"
DELEGATION_ID = "del_ph07_grant"
GRANT_ID = f"wsg_{DELEGATION_ID}"
AGENT_ID = "agent_executor"
RUNTIME_ID = "runtime_process"


def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("# PH07 source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "seed"],
        check=True,
    )
    return path


class GrantEnv:
    def __init__(self, tmp_path: Path, *, git_source: bool = True) -> None:
        self.tmp = tmp_path
        db_path = tmp_path / "grants.db"
        migrate(db_path)
        assert_migration_matches_models(db_path)
        self.sessions = session_factory(create_sqlite_engine(db_path))
        registry = AgentRegistryService(self.sessions)
        registry.register_profile(AgentProfile(
            agent_id=AgentId(AGENT_ID),
            display_name="Executor",
            roles=("executor",),
            skills=("code.modify",),
            trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        ))
        registry.register_runtime(AgentRuntime(
            runtime_id=AgentRuntimeId(RUNTIME_ID),
            agent_id=AgentId(AGENT_ID),
            adapter_kind=AgentAdapterKind.PROCESS,
            runtime_name="Managed process",
        ))
        self.source_root = tmp_path / "source"
        self.source_root.mkdir()
        if git_source:
            _git_repo(self.source_root)
        self.transport_root = tmp_path / "transport"
        self.transport_root.mkdir()
        self.service = WorkspaceDelegationService(
            self.sessions,
            ArtifactService(ContentAddressedArtifactStore(tmp_path / "artifacts"), self.sessions),
            (GitWorktreeTransport(self.transport_root),),
        )
        self.source = WorkspaceSource(root=self.source_root)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            # Flush parents before children: without ORM relationships the
            # unit-of-work flush order is not guaranteed to be parent-first,
            # and SQLite FK checks fail on a child INSERT whose parent row
            # has not been written yet.
            session.add(WorkspaceRecord(
                workspace_id=WORKSPACE_ID, name="PH07 grants",
                created_at=now, updated_at=now, version=1,
            ))
            session.flush()
            session.add(ResearchRunRecord(
                run_id=RUN_ID, workspace_id=WORKSPACE_ID,
                objective="grant provisioning", state="ACTIVE",
                created_at=now, updated_at=now, version=1,
            ))
            session.flush()
            session.add(DelegationRecord(
                delegation_id=DELEGATION_ID, run_id=RUN_ID,
                purpose="EXECUTE", required_roles_json=["executor"],
                required_skills_json=[], required_trust_zones_json=[],
                assigned_agent_id=AGENT_ID, assigned_runtime_id=RUNTIME_ID,
                state=DelegationState.ASSIGNED.value,
                idempotency_key=f"{DELEGATION_ID}-orchestration",
                created_at=now, updated_at=now, version=1,
            ))
        self.gateway = CollaborationGateway(
            None, None,
            delegations=DelegationService(self.sessions),
            workspace=self.service,
            workspace_sources={WORKSPACE_ID: self.source},
        )

    def grant_row(self) -> WorkspaceGrantRecord | None:
        with self.sessions() as session:
            row = session.scalar(select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.workspace_grant_id == GRANT_ID,
            ))
            if row is not None:
                session.expunge(row)
            return row

    def set_grant_state(self, state: WorkspaceGrantState) -> None:
        with self.sessions.begin() as session:
            row = session.get(WorkspaceGrantRecord, GRANT_ID)
            assert row is not None
            row.state = state.value

    def transport_rows(self) -> list[WorkspaceTransportRecord]:
        with self.sessions() as session:
            rows = list(session.scalars(select(WorkspaceTransportRecord).where(
                WorkspaceTransportRecord.workspace_grant_id == GRANT_ID,
            )))
            for row in rows:
                session.expunge(row)
            return rows


@pytest.fixture
def env(tmp_path: Path) -> GrantEnv:
    return GrantEnv(tmp_path)


# ----------------------------------------------------------------------
# 1. workspace_sources configuration parsing and rejection.
# ----------------------------------------------------------------------

def _config_doc(tmp_path: Path, sources: dict[str, object]) -> dict[str, object]:
    return {
        "database": str(tmp_path / "db.sqlite"),
        "artifact_root": str(tmp_path / "artifacts"),
        "state_root": str(tmp_path / "state"),
        "workspace_sources": sources,
    }


def test_workspace_sources_config_parses_defaults(tmp_path: Path) -> None:
    config = DaemonConfig.model_validate_json(json.dumps(_config_doc(tmp_path, {
        WORKSPACE_ID: {"root": str(tmp_path / "source")},
    })))
    source = config.workspace_sources[WORKSPACE_ID]
    assert source.root == tmp_path / "source"
    assert source.access_mode is WorkspaceAccessMode.READ_WRITE
    assert source.transport_kind is WorkspaceTransportKind.GIT_WORKTREE
    assert source.lease_seconds == 3600
    assert source.allowed_paths == (".",)


def test_workspace_sources_reject_invalid_workspace_id(tmp_path: Path) -> None:
    for bad_id in ("bad id!", "ws x", "", "x" * 129, "_leading"):
        with pytest.raises(ValidationError, match="workspace ID is invalid"):
            DaemonConfig.model_validate_json(json.dumps(_config_doc(tmp_path, {
                bad_id: {"root": str(tmp_path / "source")},
            })))


def test_workspace_sources_reject_relative_root(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must be absolute"):
        DaemonConfig.model_validate_json(json.dumps(_config_doc(tmp_path, {
            WORKSPACE_ID: {"root": "relative/source"},
        })))


def test_compose_daemon_rejects_non_git_source(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    config = DaemonConfig.model_validate_json(json.dumps(_config_doc(tmp_path, {
        WORKSPACE_ID: {"root": str(tmp_path / "plain")},
    })))
    with pytest.raises(ValueError, match="not a Git repository"):
        compose_daemon(config)

    _git_repo(tmp_path / "git-source")
    git_config = DaemonConfig.model_validate_json(json.dumps(_config_doc(tmp_path, {
        WORKSPACE_ID: {"root": str(tmp_path / "git-source")},
    })))
    compose_daemon(git_config)


# ----------------------------------------------------------------------
# 2-4. Grant lifecycle through the trusted gateway path.
# ----------------------------------------------------------------------

def test_execute_grant_auto_created_provisioned_and_reused(env: GrantEnv) -> None:
    env.gateway._ensure_execution_workspace(DELEGATION_ID)
    grant = env.grant_row()
    assert grant is not None
    assert grant.state == WorkspaceGrantState.ACTIVE.value
    assert grant.lease_expires_at is not None
    assert len(env.transport_rows()) == 1

    # A retried EXECUTE turn must reuse the ACTIVE grant, not mint a second one.
    env.gateway._ensure_execution_workspace(DELEGATION_ID)
    assert env.grant_row() is not None
    assert len(env.transport_rows()) == 1


def test_pending_grant_recovers_via_provision(env: GrantEnv) -> None:
    env.gateway._ensure_execution_workspace(DELEGATION_ID)
    # Simulate a crash that left the durable grant PENDING with the
    # worktree rolled back: recovery must re-provision, not fail closed.
    target = env.transport_root / GRANT_ID
    subprocess.run(
        ["git", "-C", str(env.source_root), "worktree", "remove", "--force", str(target)],
        check=True,
    )
    env.set_grant_state(WorkspaceGrantState.PENDING)
    env.gateway._ensure_execution_workspace(DELEGATION_ID)
    grant = env.grant_row()
    assert grant is not None
    assert grant.state == WorkspaceGrantState.ACTIVE.value


@pytest.mark.parametrize("state", [
    WorkspaceGrantState.PROVISIONING,
    WorkspaceGrantState.FAILED,
    WorkspaceGrantState.EXPIRED,
])
def test_abnormal_grant_states_fail_closed(env: GrantEnv, state: WorkspaceGrantState) -> None:
    env.gateway._ensure_execution_workspace(DELEGATION_ID)
    env.set_grant_state(state)
    with pytest.raises(WorkspaceDelegationError, match="not provisionable"):
        env.gateway._ensure_execution_workspace(DELEGATION_ID)
    grant = env.grant_row()
    assert grant is not None
    assert grant.state == state.value
    assert len(env.transport_rows()) == 1


def test_missing_workspace_source_fails_closed(tmp_path: Path) -> None:
    env = GrantEnv(tmp_path)
    bare_gateway = CollaborationGateway(
        None, None,
        delegations=DelegationService(env.sessions),
        workspace=env.service,
        workspace_sources={},
    )
    with pytest.raises(ValueError, match="source is not configured"):
        bare_gateway._ensure_execution_workspace(DELEGATION_ID)
    assert env.grant_row() is None


def test_gateway_rejects_non_git_source(tmp_path: Path) -> None:
    env = GrantEnv(tmp_path, git_source=False)
    with pytest.raises(ValueError, match="not a Git repository"):
        env.gateway._ensure_execution_workspace(DELEGATION_ID)
    assert env.grant_row() is None


def test_grant_fields_derive_only_from_trusted_config(tmp_path: Path) -> None:
    env = GrantEnv(tmp_path)
    env.source = WorkspaceSource(
        root=env.source_root,
        access_mode=WorkspaceAccessMode.READ_ONLY,
        allowed_paths=("docs/",),
        excluded_paths=(".secrets/",),
        lease_seconds=120,
    )
    env.gateway = CollaborationGateway(
        None, None,
        delegations=DelegationService(env.sessions),
        workspace=env.service,
        workspace_sources={WORKSPACE_ID: env.source},
    )
    env.gateway._ensure_execution_workspace(DELEGATION_ID)
    grant = env.grant_row()
    assert grant is not None
    assert grant.access_mode == WorkspaceAccessMode.READ_ONLY.value
    # Policy paths are normalized on persistence (trailing slashes dropped).
    assert tuple(grant.allowed_paths) == ("docs",)
    assert tuple(grant.excluded_paths) == (".secrets",)
    assert grant.lease_seconds == 120
    # The transport handle is materialized under the daemon-owned transport
    # root; no client-submitted path can influence it.
    (transport,) = env.transport_rows()
    assert Path(transport.remote_workspace_handle).is_relative_to(env.transport_root)
