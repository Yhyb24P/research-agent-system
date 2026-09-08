"""PX07-02: governed remote attach / renew / detach acceptance matrix.

Covers the frozen acceptance items from 推进计划.md §A:
1. external DTO / shell accept only ``runtime_id``; injected endpoint, tenant,
   secret, actor or launch-spec fields are rejected; HTTP actor is HUMAN;
2. attach only accepts enabled A2A runtimes with a governed HTTPS/loopback
   endpoint, declared ``A2A/1.0`` protocol and a valid tenant; failures write
   no lease;
3. attach takes a daemon-owned lease; repeated attach and explicit renew
   extend the same lease; durable receipt replay adds no state; foreign
   owner leases conflict;
4. detach only releases the daemon-owned lease; wrong owner, unknown,
   detached or expired leases fail closed; attach/renew/detach never create
   a ``RuntimeSession``;
5. after attach the A2A runtime is selectable by the product selector, after
   detach it is not; a mixed PROCESS+A2A closed loop completes while the
   runtime is attached.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from researchd.adapters.a2a import REMOTE_EXECUTION_REQUEST_MEDIA_TYPE
from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlCommandRouter
from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.client.shell import ParsedCommand, parse_line
from researchd.collaboration.action_broker import AgentActionBroker
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.heterogeneous import (
    GovernedA2ARemoteAgentAdapter,
    ManagedProcessAgentAdapter,
)
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.registry import AgentRegistryService, RuntimeLeaseConflict
from researchd.collaboration.remote_attachment import RemoteAttachmentService
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.collaboration.selector import AgentSelector
from researchd.context.agent_context import AgentContextBuilder
from researchd.context.builder import ContextBuilder
from researchd.context.redaction import DeterministicRedactor
from researchd.daemon.command_service import DurableDaemonCommandService
from researchd.daemon.contracts import (
    DaemonCommandResult,
    ExternalCommandRequest,
    ExternalRemoteAgentAttachRequest,
    ExternalRemoteAgentDetachRequest,
    ExternalRemoteAgentRenewRequest,
    RemoteAgentAttachCommand,
    RemoteAgentDetachCommand,
    RemoteAgentRenewCommand,
)
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.domain.enums import (
    AgentAdapterKind,
    AgentTrustZone,
    Capability,
    DelegationPurpose,
    ResearchRunState,
)
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.executor.capability_broker import CapabilityBroker
from researchd.executor.contracts import CommandLimits
from researchd.orchestrator.engine import (
    OrchestrationError,
    OrchestrationLimits,
    ResearchOrchestrator,
)
from researchd.policy.engine import BudgetLimits, DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.runtime_sessions.contracts import ProcessLaunchSpec
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentInvocationRecord,
    AgentRuntimeLeaseEventRecord,
    AgentRuntimeRecord,
    AttemptRecord,
    AuditEventRecord,
    DelegationRecord,
    RuntimeSessionRecord,
    WorkspaceGrantRecord,
    WorkspaceRecord,
    WorkspaceTransportRecord,
    WorkOrderRecord,
)
from tests.integration.test_a2a_governed_execute import (
    FakeSandboxBackend,
    RecordingA2AClient,
    _ManagedRoutingClient,
)
from tests.integration.test_orchestrator import FakeVerifier
from tests.integration.test_storage import migrate

LOOPBACK_ENDPOINT = "http://127.0.0.1:8787/a2a"
MANAGED_ENDPOINT = "http://127.0.0.1:9100/turn"
DAEMON_OWNER = RemoteAttachmentService.owner_id

UNSAFE_ENDPOINTS = (
    "http://10.1.1.1:8787/a2a",
    "https://user:pass@example.com/a2a",
    "https://example.com/a2a?tenant=x",
    "https://example.com/a2a#frag",
)


class AttachFixture:
    """Registry-seeded fixture driving RemoteAttachmentService directly."""

    def __init__(self, tmp_path: Path) -> None:
        database = tmp_path / "attach.db"
        migrate(database)
        self.sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
        self.registry = AgentRegistryService(self.sessions)
        self.service = RemoteAttachmentService(self.registry)
        self.registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_att"), display_name="Attached remote",
            roles=("executor",), trust_zone=AgentTrustZone.REMOTE_PRIVATE,
        ))
        self.registry.register_runtime(AgentRuntime(
            runtime_id=AgentRuntimeId("runtime_att"), agent_id=AgentId("agent_att"),
            adapter_kind=AgentAdapterKind.A2A, runtime_name="attached A2A",
            endpoint_ref=LOOPBACK_ENDPOINT, protocols=("1.0",),
            metadata={"a2a_tenant": "tenant-att"},
        ))
        self.registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_proc"), display_name="Process planner",
            roles=("planner",), trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        ))
        self.registry.register_runtime(AgentRuntime(
            runtime_id=AgentRuntimeId("runtime_proc"), agent_id=AgentId("agent_proc"),
            adapter_kind=AgentAdapterKind.PROCESS, runtime_name="process",
            endpoint_ref=MANAGED_ENDPOINT,
        ))

    def runtime_row(self, runtime_id: str = "runtime_att") -> AgentRuntimeRecord:
        with self.sessions() as session:
            row = session.get(AgentRuntimeRecord, runtime_id)
        assert row is not None
        return row

    def lease_events(self, runtime_id: str = "runtime_att") -> list[str]:
        with self.sessions() as session:
            rows = session.scalars(select(AgentRuntimeLeaseEventRecord).where(
                AgentRuntimeLeaseEventRecord.runtime_id == runtime_id,
            ).order_by(AgentRuntimeLeaseEventRecord.observed_at,
                       AgentRuntimeLeaseEventRecord.event_id)).all()
        return [row.event_type for row in rows]

    def session_count(self) -> int:
        with self.sessions() as session:
            return int(session.scalar(select(func.count()).select_from(RuntimeSessionRecord)))

    def expire_lease(self, runtime_id: str = "runtime_att") -> None:
        with self.sessions.begin() as session:
            row = session.get(AgentRuntimeRecord, runtime_id)
            assert row is not None and row.runtime_lease_id is not None
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)


@pytest.fixture
def attach(tmp_path: Path) -> AttachFixture:
    return AttachFixture(tmp_path)


def test_attach_records_daemon_owned_lease_and_no_session(attach: AttachFixture) -> None:
    result = attach.service.attach("runtime_att")

    assert result["runtime_id"] == "runtime_att"
    assert str(result["lease_id"]).startswith("runtime_lease_")
    row = attach.runtime_row()
    assert row.runtime_lease_id == result["lease_id"]
    assert row.lease_owner_id == DAEMON_OWNER
    assert attach.lease_events() == ["ACQUIRED"]
    assert attach.session_count() == 0


def test_attach_rejects_non_a2a_runtime_without_lease(attach: AttachFixture) -> None:
    with pytest.raises(ValueError, match="requires an A2A AgentRuntime"):
        attach.service.attach("runtime_proc")

    row = attach.runtime_row("runtime_proc")
    assert row.runtime_lease_id is None and row.lease_owner_id is None
    assert attach.lease_events("runtime_proc") == []
    assert attach.session_count() == 0


@pytest.mark.parametrize("endpoint", UNSAFE_ENDPOINTS)
def test_attach_rejects_unsafe_endpoints(attach: AttachFixture, endpoint: str) -> None:
    attach.registry.update_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_att"), agent_id=AgentId("agent_att"),
        adapter_kind=AgentAdapterKind.A2A, runtime_name="attached A2A",
        endpoint_ref=endpoint, protocols=("1.0",), metadata={"a2a_tenant": "tenant-att"},
    ))

    with pytest.raises(ValueError, match="governed HTTPS/loopback endpoint"):
        attach.service.attach("runtime_att")

    row = attach.runtime_row()
    assert row.runtime_lease_id is None
    assert attach.lease_events() == []


def test_attach_rejects_missing_protocol(attach: AttachFixture) -> None:
    attach.registry.update_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_att"), agent_id=AgentId("agent_att"),
        adapter_kind=AgentAdapterKind.A2A, runtime_name="attached A2A",
        endpoint_ref=LOOPBACK_ENDPOINT, protocols=(), metadata={},
    ))

    with pytest.raises(ValueError, match="declared A2A/1.0 protocol"):
        attach.service.attach("runtime_att")
    assert attach.runtime_row().runtime_lease_id is None


@pytest.mark.parametrize("tenant", ["", "ten\tant", "t" * 129])
def test_attach_rejects_invalid_tenant(attach: AttachFixture, tenant: str) -> None:
    attach.registry.update_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_att"), agent_id=AgentId("agent_att"),
        adapter_kind=AgentAdapterKind.A2A, runtime_name="attached A2A",
        endpoint_ref=LOOPBACK_ENDPOINT, protocols=("1.0",), metadata={"a2a_tenant": tenant},
    ))

    with pytest.raises(ValueError, match="tenant is invalid"):
        attach.service.attach("runtime_att")
    assert attach.runtime_row().runtime_lease_id is None


def test_attach_rejects_disabled_runtime(attach: AttachFixture) -> None:
    attach.registry.set_runtime_enabled("runtime_att", False)

    with pytest.raises(ValueError, match="unavailable"):
        attach.service.attach("runtime_att")
    assert attach.runtime_row().runtime_lease_id is None


def test_attach_conflicts_with_foreign_owner_lease(attach: AttachFixture) -> None:
    attach.registry.acquire_runtime("runtime_att", owner_id="someone-else", lease_seconds=3600)

    with pytest.raises(RuntimeLeaseConflict):
        attach.service.attach("runtime_att")
    with pytest.raises(ValueError, match="no active daemon-owned attachment"):
        attach.service.renew("runtime_att")
    with pytest.raises(ValueError, match="no daemon-owned attachment"):
        attach.service.detach("runtime_att")
    assert attach.runtime_row().lease_owner_id == "someone-else"


def test_repeated_attach_and_renew_extend_the_same_lease(attach: AttachFixture) -> None:
    first = attach.service.attach("runtime_att")
    second = attach.service.attach("runtime_att")
    renewed = attach.service.renew("runtime_att")

    assert first["lease_id"] == second["lease_id"] == renewed["lease_id"]
    assert attach.lease_events() == ["ACQUIRED", "RENEWED", "RENEWED"]
    assert attach.runtime_row().runtime_lease_id == first["lease_id"]
    assert attach.session_count() == 0


def test_renew_and_detach_fail_closed_on_unknown_or_detached_lease(attach: AttachFixture) -> None:
    for operation in (attach.service.renew, attach.service.detach):
        with pytest.raises(ValueError):
            operation("runtime_ghost")

    attach.service.attach("runtime_att")
    assert attach.service.detach("runtime_att") == {"runtime_id": "runtime_att", "detached": True}
    with pytest.raises(ValueError, match="no active daemon-owned attachment"):
        attach.service.renew("runtime_att")
    with pytest.raises(ValueError, match="no daemon-owned attachment"):
        attach.service.detach("runtime_att")


def test_renew_and_detach_fail_closed_on_expired_lease(attach: AttachFixture) -> None:
    attach.service.attach("runtime_att")
    attach.expire_lease()

    with pytest.raises(ValueError, match="not active"):
        attach.service.renew("runtime_att")
    with pytest.raises(ValueError, match="not active"):
        attach.service.detach("runtime_att")
    assert attach.runtime_row().runtime_lease_id is not None


def test_attach_renew_detach_never_create_runtime_sessions(attach: AttachFixture) -> None:
    attach.service.attach("runtime_att")
    attach.service.renew("runtime_att")
    attach.service.detach("runtime_att")
    assert attach.session_count() == 0


INJECTED_FIELDS: dict[str, Any] = {
    "endpoint": "http://evil.example/a2a",
    "tenant": "tenant-forge",
    "secret": "hunter2",
    "actor_type": "SYSTEM",
    "actor_id": "forged-system",
    "launch_spec": {"argv": ["/bin/sh", "-c", "id"], "cwd": "/"},
}


@pytest.mark.parametrize("request_cls", [
    ExternalRemoteAgentAttachRequest,
    ExternalRemoteAgentDetachRequest,
    ExternalRemoteAgentRenewRequest,
])
def test_external_dto_rejects_injected_fields(
    request_cls: type[ExternalCommandRequest],
) -> None:
    payload: dict[str, Any] = {"command_id": "cmd_dto", "runtime_id": "runtime_att"}
    payload.update(INJECTED_FIELDS)

    with pytest.raises(ValidationError):
        request_cls.model_validate(payload)

    clean = request_cls.model_validate({"command_id": "cmd_dto", "runtime_id": "runtime_att"})
    assert getattr(clean, "runtime_id") == "runtime_att"


@pytest.mark.parametrize("runtime_id", ["runtime_", "agent_att", "runtime_bad!", ""])
def test_external_dto_runtime_id_pattern_rejects_malformed_ids(runtime_id: str) -> None:
    with pytest.raises(ValidationError):
        ExternalRemoteAgentAttachRequest.model_validate({
            "command_id": "cmd_dto", "runtime_id": runtime_id,
        })


class _RemoteDispatcher:
    """Records remote-agent commands; rejects everything else."""

    def __init__(self) -> None:
        self.commands: list[DomainModel] = []

    def __call__(self, command: DomainModel) -> DaemonCommandResult:
        assert isinstance(command, (
            RemoteAgentAttachCommand, RemoteAgentDetachCommand, RemoteAgentRenewCommand,
        ))
        self.commands.append(command)
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type=type(command).__name__.removesuffix("Command"),
            status="ACCEPTED",
            resource={"runtime_id": command.runtime_id},
        )


def _ready_router(tmp_path: Path) -> tuple[ControlCommandRouter, _RemoteDispatcher]:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = _RemoteDispatcher()
    daemon = ResearchDaemon(StartupBarrier({phase: lambda: None for phase in StartupPhase}), dispatcher)
    assert daemon.start().ready
    return ControlCommandRouter(LocalControlAPI(sessions), daemon), dispatcher


@pytest.mark.parametrize("action", ["attach", "renew", "detach"])
def test_http_routes_pin_human_actor_and_only_runtime_id(
    tmp_path: Path, action: str,
) -> None:
    router, dispatcher = _ready_router(tmp_path)

    status, response = asyncio.run(router.post(
        f"/api/remote-agents/{action}",
        {"command_id": f"cmd_ra_{action}", "runtime_id": "runtime_att"},
    ))

    assert status == 202
    assert response["command_type"] == f"RemoteAgent{action.capitalize()}"
    assert response["status"] == "ACCEPTED"
    command = dispatcher.commands[0]
    assert isinstance(command, (
        RemoteAgentAttachCommand, RemoteAgentDetachCommand, RemoteAgentRenewCommand,
    ))
    assert command.actor_type == "HUMAN"
    assert command.actor_id == "local-control-client"
    assert command.runtime_id == "runtime_att"


def test_http_routes_reject_injected_fields_before_dispatch(tmp_path: Path) -> None:
    router, dispatcher = _ready_router(tmp_path)
    payload: dict[str, Any] = {"command_id": "cmd_ra_forged", "runtime_id": "runtime_att"}
    payload.update(INJECTED_FIELDS)

    for action in ("attach", "renew", "detach"):
        with pytest.raises(ValidationError):
            asyncio.run(router.post(f"/api/remote-agents/{action}", dict(payload)))
    assert dispatcher.commands == []


def test_shell_remote_commands_send_only_runtime_id() -> None:
    from tests.test_client_shell import _FakeClient, _drive

    parsed = parse_line("remote attach runtime_att")
    assert parsed.name == "remote attach" and parsed.args == ("runtime_att",)
    parsed = parse_line("remote renew runtime_att")
    assert parsed.name == "remote renew" and parsed.args == ("runtime_att",)
    parsed = parse_line("remote detach runtime_att")
    assert parsed.name == "remote detach" and parsed.args == ("runtime_att",)

    client = _FakeClient()
    _drive([
        "remote attach runtime_att",
        "remote renew runtime_att",
        "remote detach runtime_att",
        "quit",
    ], client)
    assert client.posts == [
        ("/api/remote-agents/attach", {"runtime_id": "runtime_att"}),
        ("/api/remote-agents/renew", {"runtime_id": "runtime_att"}),
        ("/api/remote-agents/detach", {"runtime_id": "runtime_att"}),
    ]


def test_durable_attach_receipt_replays_without_extra_state(attach: AttachFixture) -> None:
    def dispatch(command: DomainModel) -> DaemonCommandResult:
        if isinstance(command, RemoteAgentAttachCommand):
            resource = attach.service.attach(command.runtime_id)
            return DaemonCommandResult(
                command_id=command.command_id,
                command_type="RemoteAgentAttach",
                status="ACCEPTED",
                resource=resource,
            )
        raise AssertionError(f"unexpected command {type(command).__name__}")

    service = DurableDaemonCommandService(attach.sessions, dispatch)
    command = RemoteAgentAttachCommand(
        command_id="cmd_attach_durable", actor_type="HUMAN",
        actor_id="operator", runtime_id="runtime_att",
    )
    first = asyncio.run(service.execute(command))
    replay = asyncio.run(service.execute(command))

    assert first.status == "ACCEPTED"
    assert replay == first
    assert attach.lease_events() == ["ACQUIRED"]
    assert attach.session_count() == 0


def test_attach_makes_a2a_selectable_and_detach_removes_it(attach: AttachFixture) -> None:
    selector = AgentSelector(
        attach.sessions,
        allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS, AgentAdapterKind.A2A}),
        supervised_adapter_kinds=frozenset({AgentAdapterKind.PROCESS}),
    )
    kinds = frozenset({AgentAdapterKind.PROCESS, AgentAdapterKind.A2A})

    assert selector.select(required_roles=("executor",), allowed_adapter_kinds=kinds) is None

    attach.service.attach("runtime_att")
    selection = selector.select(required_roles=("executor",), allowed_adapter_kinds=kinds)
    assert selection is not None and str(selection.agent_id) == "agent_att"

    attach.service.detach("runtime_att")
    assert selector.select(required_roles=("executor",), allowed_adapter_kinds=kinds) is None


class AttachedGovFixture:
    """Mixed PROCESS+A2A fixture whose A2A lease comes from RemoteAttachmentService."""

    def __init__(self, tmp_path: Path) -> None:
        database = tmp_path / "attached_gov.db"
        migrate(database)
        self.sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
        registry = AgentRegistryService(self.sessions)
        registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_planner"), display_name="Planner",
            roles=("planner",), trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        ))
        registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_reviewer"), display_name="Reviewer",
            roles=("reviewer",), trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        ))
        registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_a2a_exec"), display_name="Governed remote executor",
            roles=("executor",), trust_zone=AgentTrustZone.REMOTE_PRIVATE,
        ))
        for agent_id in ("agent_planner", "agent_reviewer"):
            registry.register_runtime(AgentRuntime(
                runtime_id=AgentRuntimeId(f"runtime_{agent_id}"), agent_id=AgentId(agent_id),
                adapter_kind=AgentAdapterKind.PROCESS, runtime_name=agent_id,
                endpoint_ref=MANAGED_ENDPOINT,
            ))
            registry.acquire_runtime(f"runtime_{agent_id}", owner_id="gov-fixture", lease_seconds=3600)
        registry.register_runtime(AgentRuntime(
            runtime_id=AgentRuntimeId("runtime_a2a_exec"), agent_id=AgentId("agent_a2a_exec"),
            adapter_kind=AgentAdapterKind.A2A, runtime_name="attached executor",
            endpoint_ref=LOOPBACK_ENDPOINT, protocols=("1.0",),
            metadata={"a2a_tenant": "tenant-att"},
        ))
        self.attachment = RemoteAttachmentService(registry)
        self.attachment.attach("runtime_a2a_exec")
        self.launch_profiles = RuntimeLaunchProfileService(self.sessions, registry)
        for agent_id in ("agent_planner", "agent_reviewer"):
            self.launch_profiles.register_process(
                f"runtime_{agent_id}", ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp"),
            )
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(WorkspaceRecord(workspace_id="ws_gov", name="gov", version=1, created_at=now, updated_at=now))
            session.flush()
            for agent_id in ("agent_planner", "agent_reviewer"):
                session.add(RuntimeSessionRecord(
                    runtime_session_id=f"rs_{agent_id}", runtime_id=f"runtime_{agent_id}",
                    launch_mode="PROCESS", supervisor_state="HEALTHY",
                    launch_spec_json={"argv": ["/usr/bin/true"], "cwd": "/tmp"},
                    launch_profile_sha256=self.launch_profiles.get(f"runtime_{agent_id}").spec_sha256,
                    reattach_state="NOT_APPLICABLE", started_at=now, last_health_at=now,
                    version=1, created_at=now, updated_at=now,
                ))
        self.v2_client = RecordingA2AClient()
        self.managed_client = _ManagedRoutingClient()
        self.broker = CapabilityBroker(
            FakeSandboxBackend(),
            ArtifactService(ContentAddressedArtifactStore(tmp_path / "artifacts"), self.sessions),
            self.sessions,
            command_limits=CommandLimits(
                wall_seconds=10, cpu_seconds=8, memory_mb=768,
                file_size_mb=16, output_bytes=128_000,
            ),
        )
        catalog = AgentAdapterCatalog(self.sessions)
        catalog.register(AgentAdapterKind.PROCESS, ManagedProcessAgentAdapter(
            self.sessions, self.launch_profiles, self.managed_client, self.broker,
            AgentActionBroker(self.sessions),
        ))
        catalog.register(AgentAdapterKind.A2A, GovernedA2ARemoteAgentAdapter(
            self.sessions, client_factory=self.v2_client.bind_endpoint,
        ))
        self.selector = AgentSelector(
            self.sessions,
            allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS, AgentAdapterKind.A2A}),
            supervised_adapter_kinds=frozenset({AgentAdapterKind.PROCESS}),
        )
        self.gateway = CollaborationGateway(
            None, None,
            delegations=DelegationService(self.sessions),
            invocations=InvocationService(self.sessions),
            selector=self.selector,
            catalog=catalog,
            context_builder=AgentContextBuilder(ContextBuilder(
                self.sessions,
                ContentAddressedArtifactStore(tmp_path / "artifacts"),
                DeterministicRedactor(),
            )),
            allowed_adapter_kinds_by_purpose={
                DelegationPurpose.PLAN: frozenset({AgentAdapterKind.PROCESS}),
                DelegationPurpose.EXECUTE: frozenset({AgentAdapterKind.PROCESS, AgentAdapterKind.A2A}),
                DelegationPurpose.REVIEW: frozenset({AgentAdapterKind.PROCESS}),
                DelegationPurpose.EVIDENCE: frozenset({AgentAdapterKind.PROCESS}),
                DelegationPurpose.SPECIALIST: frozenset({AgentAdapterKind.PROCESS}),
            },
        )
        self.orchestrator = ResearchOrchestrator(
            self.sessions, collaboration=self.gateway,
            policy=RecordingPolicyEngine(DeterministicPolicyEngine(), self.sessions),
            verifier=FakeVerifier(self.sessions),
            workspace_capabilities=frozenset({Capability.WORKSPACE_WRITE}),
            user_capabilities=frozenset({Capability.WORKSPACE_WRITE}),
            maximum_budget=BudgetLimits(100, 100, 0, 100, 100),
            limits=OrchestrationLimits(max_iterations=8, max_agent_turns=8),
        )

    def create_run(self) -> str:
        return self.orchestrator.create_run(workspace_id="ws_gov", objective="mixed process and a2a via attachment")

    def latest_order(self, run_id: str) -> WorkOrderRecord:
        with self.sessions() as session:
            row = session.scalar(select(WorkOrderRecord).where(
                WorkOrderRecord.run_id == run_id,
            ).order_by(WorkOrderRecord.created_at.desc()).limit(1))
        assert row is not None
        return row

    def latest_attempt(self, run_id: str) -> AttemptRecord:
        order = self.latest_order(run_id)
        with self.sessions() as session:
            row = session.scalar(select(AttemptRecord).where(
                AttemptRecord.work_order_id == order.work_order_id,
            ).order_by(AttemptRecord.created_at.desc()).limit(1))
        assert row is not None
        return row

    def advance_until_executing(self, run_id: str) -> None:
        for _ in range(16):
            progressed = asyncio.run(self.orchestrator.advance(run_id))
            if self.latest_order(run_id).state == "EXECUTING":
                return
            if not progressed:
                raise OrchestrationError("run stalled before execution")
        raise OrchestrationError("run did not reach execution")

    def seed_grant_for(self, attempt_id: str) -> None:
        with self.sessions() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            assert attempt is not None and attempt.delegation_id is not None
            delegation_id = attempt.delegation_id
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(WorkspaceGrantRecord(
                workspace_grant_id=f"grant_{delegation_id}", delegation_id=delegation_id,
                source_workspace_id="ws_gov", source_revision="1",
                source_manifest_sha256="e" * 64,
                access_mode="READ_WRITE", allowed_paths=["."], excluded_paths=[],
                classification_ceiling="LOCAL_ONLY",
                max_total_bytes=10_000_000, max_file_count=1_000, max_single_file_bytes=5_000_000,
                lease_seconds=3600, lease_started_at=now, lease_expires_at=now + timedelta(hours=1),
                renewal_policy="DENY", transport_kind="ARCHIVE",
                reconciliation_mode="ARTIFACT_ONLY", state="ACTIVE",
                cleanup_state="PENDING", version=1, created_at=now, updated_at=now,
            ))
            session.add(WorkspaceTransportRecord(
                workspace_transport_id=f"wst_{delegation_id}",
                workspace_grant_id=f"grant_{delegation_id}",
                transport_kind="ARCHIVE", transport_handle={"root": "att-archive"},
                remote_workspace_handle="att-archive", state="ACTIVE", created_at=now,
            ))


@pytest.fixture
def attached_gov(tmp_path: Path) -> AttachedGovFixture:
    return AttachedGovFixture(tmp_path)


def test_mixed_process_a2a_closed_loop_via_attachment(attached_gov: AttachedGovFixture) -> None:
    run_id = attached_gov.create_run()
    attached_gov.advance_until_executing(run_id)
    attempt = attached_gov.latest_attempt(run_id)
    attached_gov.seed_grant_for(attempt.attempt_id)
    snapshot = asyncio.run(attached_gov.orchestrator.run(run_id, max_steps=30))

    assert snapshot.state.value == ResearchRunState.COMPLETED.value
    with attached_gov.sessions() as session:
        delegations = {
            row.purpose: row for row in session.scalars(
                select(DelegationRecord).where(DelegationRecord.run_id == run_id),
            ).all()
        }
        assert delegations["PLAN"].assigned_agent_id == "agent_planner"
        assert delegations["EXECUTE"].assigned_agent_id == "agent_a2a_exec"
        assert delegations["REVIEW"].assigned_agent_id == "agent_reviewer"
        invocations = {
            row.purpose: row for row in session.scalars(
                select(AgentInvocationRecord).where(AgentInvocationRecord.run_id == run_id),
            ).all()
        }
        assert invocations["PLAN"].output_type == "PlanProposal"
        assert invocations["EXECUTE"].output_type == "ExecutorResult"
        assert invocations["REVIEW"].output_type == "ReviewDecision"
        assert all(row.status == "SUCCEEDED" for row in invocations.values())
        assert session.scalar(select(AuditEventRecord.event_type).where(
            AuditEventRecord.event_type == "A2A_TASK_DISPATCHED",
        )) == "A2A_TASK_DISPATCHED"
    payload = attached_gov.v2_client.payloads[0]
    assert len(attached_gov.v2_client.payloads) == 1
    assert payload["message"]["parts"][0]["mediaType"] == REMOTE_EXECUTION_REQUEST_MEDIA_TYPE
    assert attached_gov.v2_client.endpoints == [LOOPBACK_ENDPOINT]
    # The A2A lease is the daemon-owned attachment lease.
    with attached_gov.sessions() as session:
        runtime = session.get(AgentRuntimeRecord, "runtime_a2a_exec")
    assert runtime is not None and runtime.lease_owner_id == DAEMON_OWNER

    # Detaching the runtime removes it from the product selector.
    attached_gov.attachment.detach("runtime_a2a_exec")
    selection = attached_gov.selector.select(
        required_roles=("executor",),
        allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS, AgentAdapterKind.A2A}),
    )
    assert selection is None
