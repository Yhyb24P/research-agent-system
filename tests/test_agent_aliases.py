"""PX01-04: CLI alias resolution over AgentProfile labels."""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.aliases import (
    AgentAliasAmbiguous,
    AgentAliasNotFound,
    AgentAliasService,
)
from researchd.collaboration.contracts import AgentProfile
from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import AgentTrustZone
from researchd.domain.ids import AgentId
from researchd.storage.db import create_sqlite_engine, session_factory
from tests.integration.test_storage import migrate


def _services(tmp_path: Path) -> tuple[AgentRegistryService, AgentAliasService]:
    database = tmp_path / "aliases.db"
    migrate(database)
    sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
    registry = AgentRegistryService(sessions)
    return registry, AgentAliasService(registry)


def _profile(
    agent_id: str,
    alias: str | None,
    *,
    enabled: bool = True,
) -> AgentProfile:
    labels = {"cli_alias": alias} if alias is not None else {}
    return AgentProfile(
        agent_id=AgentId(agent_id),
        display_name=agent_id,
        roles=("executor",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        labels=labels,
        enabled=enabled,
    )


def test_unique_alias_resolves_the_enabled_profile(tmp_path: Path) -> None:
    registry, service = _services(tmp_path)
    registry.register_profile(_profile("agent_alpha", "alpha"))

    profile = service.resolve("alpha")

    assert str(profile.agent_id) == "agent_alpha"


def test_alias_query_is_case_and_whitespace_insensitive(tmp_path: Path) -> None:
    registry, service = _services(tmp_path)
    registry.register_profile(_profile("agent_alpha", "Alpha"))

    assert str(service.resolve("  aLPHA ").agent_id) == "agent_alpha"


def test_unknown_alias_is_rejected(tmp_path: Path) -> None:
    registry, service = _services(tmp_path)
    registry.register_profile(_profile("agent_alpha", "alpha"))

    with pytest.raises(AgentAliasNotFound, match="gamma"):
        service.resolve("gamma")


def test_blank_alias_is_rejected(tmp_path: Path) -> None:
    _, service = _services(tmp_path)

    with pytest.raises(AgentAliasNotFound):
        service.resolve("   ")


def test_profile_without_cli_alias_is_ignored(tmp_path: Path) -> None:
    registry, service = _services(tmp_path)
    registry.register_profile(_profile("agent_bare", None))

    with pytest.raises(AgentAliasNotFound):
        service.resolve("bare")


def test_ambiguous_alias_among_enabled_profiles_is_rejected(tmp_path: Path) -> None:
    registry, service = _services(tmp_path)
    registry.register_profile(_profile("agent_one", "shared"))
    registry.register_profile(_profile("agent_two", "Shared"))

    with pytest.raises(AgentAliasAmbiguous) as raised:
        service.resolve("shared")
    assert raised.value.agent_ids == ("agent_one", "agent_two")


def test_disabled_profile_does_not_create_ambiguity(tmp_path: Path) -> None:
    registry, service = _services(tmp_path)
    registry.register_profile(_profile("agent_active", "alpha"))
    registry.register_profile(_profile("agent_retired", "ALPHA", enabled=False))

    assert str(service.resolve("alpha").agent_id) == "agent_active"


def test_disabled_only_alias_is_not_found(tmp_path: Path) -> None:
    registry, service = _services(tmp_path)
    registry.register_profile(_profile("agent_retired", "ghost", enabled=False))

    with pytest.raises(AgentAliasNotFound):
        service.resolve("ghost")


def test_disabling_a_profile_releases_its_alias(tmp_path: Path) -> None:
    registry, service = _services(tmp_path)
    registry.register_profile(_profile("agent_one", "shared"))
    registry.register_profile(_profile("agent_two", "shared"))
    registry.disable("agent_two")

    assert str(service.resolve("shared").agent_id) == "agent_one"
