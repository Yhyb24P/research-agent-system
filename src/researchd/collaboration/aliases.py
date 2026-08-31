"""CLI alias resolution for registered Agent profiles.

An operator-typed alias names an Agent through the trusted
``AgentProfile.labels["cli_alias"]`` label. Comparison is
case-insensitive: both the stored label and the queried alias are
normalized by stripping surrounding whitespace and lower-casing, so
``Alpha`` and `` alpha `` resolve the same alias. Only enabled profiles
participate: a disabled profile neither resolves nor creates ambiguity.
When two or more enabled profiles claim the same normalized alias,
resolution is rejected as ambiguous — uniqueness among enabled profiles
is therefore enforced at resolution time.
"""

from researchd.collaboration.contracts import AgentProfile
from researchd.collaboration.registry import AgentRegistryService

CLI_ALIAS_LABEL = "cli_alias"


def normalize_cli_alias(alias: str) -> str:
    """Canonical alias form: surrounding whitespace stripped, lower-cased."""
    return alias.strip().lower()


class AgentAliasError(ValueError):
    """A CLI alias could not be resolved to a single enabled Agent."""


class AgentAliasNotFound(AgentAliasError):
    def __init__(self, alias: str) -> None:
        super().__init__(f"no enabled Agent profile claims CLI alias: {alias}")
        self.alias = alias


class AgentAliasAmbiguous(AgentAliasError):
    def __init__(self, alias: str, agent_ids: tuple[str, ...]) -> None:
        super().__init__(
            f"CLI alias is claimed by multiple enabled Agents: {alias} "
            f"({', '.join(agent_ids)})"
        )
        self.alias = alias
        self.agent_ids = agent_ids


class AgentAliasService:
    """Resolve a CLI alias to exactly one enabled Agent profile."""

    def __init__(self, registry: AgentRegistryService) -> None:
        self.registry = registry

    def resolve(self, alias: str) -> AgentProfile:
        normalized = normalize_cli_alias(alias)
        if not normalized:
            raise AgentAliasNotFound(alias)
        matches = tuple(
            profile
            for profile in self.registry.list_agents()
            if profile.enabled
            and normalize_cli_alias(profile.labels.get(CLI_ALIAS_LABEL, ""))
            == normalized
        )
        if not matches:
            raise AgentAliasNotFound(alias)
        if len(matches) > 1:
            raise AgentAliasAmbiguous(
                alias,
                tuple(str(profile.agent_id) for profile in matches),
            )
        return matches[0]


__all__ = [
    "AgentAliasAmbiguous",
    "AgentAliasError",
    "AgentAliasNotFound",
    "AgentAliasService",
    "CLI_ALIAS_LABEL",
    "normalize_cli_alias",
]
