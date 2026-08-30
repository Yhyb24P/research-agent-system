from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, text

from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.runtime_sessions.contracts import (
    ProcessLaunchSpec,
    RemoteHttpLaunchConfiguration,
)
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.db import create_sqlite_engine, session_factory
from tests.integration.test_storage import ROOT, assert_migration_matches_models, migrate


def _registry(database: Path) -> tuple[AgentRegistryService, RuntimeLaunchProfileService]:
    sessions = session_factory(create_sqlite_engine(database))
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_launch_catalog"),
        display_name="Launch catalog Agent",
        roles=("executor",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    return registry, RuntimeLaunchProfileService(sessions, registry)


def test_process_launch_profile_is_server_owned_and_digest_bound(tmp_path: Path) -> None:
    database = tmp_path / "profiles.db"
    migrate(database)
    registry, profiles = _registry(database)
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_process_catalog"),
        agent_id=AgentId("agent_launch_catalog"),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Catalog process",
    ))
    spec = ProcessLaunchSpec(argv=("/usr/bin/true", "--fixed"), cwd=str(tmp_path))

    stored = profiles.register_process("runtime_process_catalog", spec)
    resolved = profiles.resolve_process("runtime_process_catalog")

    assert resolved.launch_spec == spec
    assert resolved.spec_sha256 == stored.spec_sha256
    assert stored.configuration == {"launch_spec": spec.model_dump(mode="json")}
    with pytest.raises(ValueError, match="already exists"):
        profiles.register_process("runtime_process_catalog", spec)


def test_remote_profile_uses_registered_endpoint_not_caller_override(tmp_path: Path) -> None:
    database = tmp_path / "profiles.db"
    migrate(database)
    registry, profiles = _registry(database)
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_http_catalog"),
        agent_id=AgentId("agent_launch_catalog"),
        adapter_kind=AgentAdapterKind.HTTP,
        runtime_name="Catalog HTTP",
        endpoint_ref="https://agent.example.invalid",
    ))
    stored = profiles.register_remote_http(
        "runtime_http_catalog",
        RemoteHttpLaunchConfiguration(health_path="/ready"),
    )

    resolved = profiles.resolve_remote_http("runtime_http_catalog")

    assert resolved.launch_spec.endpoint == "https://agent.example.invalid"
    assert resolved.launch_spec.health_path == "/ready"
    assert resolved.spec_sha256 == stored.spec_sha256


def test_profile_mode_disabled_state_and_digest_mismatch_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "profiles.db"
    migrate(database)
    registry, profiles = _registry(database)
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_process_catalog"),
        agent_id=AgentId("agent_launch_catalog"),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Catalog process",
    ))
    profiles.register_process(
        "runtime_process_catalog",
        ProcessLaunchSpec(argv=("/usr/bin/true",), cwd=str(tmp_path)),
        enabled=False,
    )
    with pytest.raises(ValueError, match="unavailable"):
        profiles.resolve_process("runtime_process_catalog")

    engine = create_sqlite_engine(database)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE runtime_launch_profiles SET enabled = 1, spec_sha256 = :digest"
        ), {"digest": "0" * 64})
    with pytest.raises(ValueError, match="digest mismatch"):
        profiles.resolve_process("runtime_process_catalog")


def test_current_0021_database_upgrades_to_launch_catalog(tmp_path: Path) -> None:
    database = tmp_path / "current-0021.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    alembic_command.upgrade(config, "0021")

    alembic_command.upgrade(config, "head")

    assert_migration_matches_models(database)
    schema = inspect(create_sqlite_engine(database))
    assert "runtime_launch_profiles" in schema.get_table_names()
    assert "launch_profile_sha256" in {
        column["name"] for column in schema.get_columns("runtime_sessions")
    }
