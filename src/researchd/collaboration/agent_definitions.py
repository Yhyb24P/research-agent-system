"""Versioned Agent definitions spanning the collaboration and launch planes.

An ``AgentDefinition`` is the installable unit of PX01: one existing
``AgentProfile``, the ``AgentRuntime`` identities it owns, and the
``RuntimeLaunchProfile`` entries that make those runtimes launchable. It
reuses the existing frozen DTOs — no second profile model — and carries a
canonical digest so an installation can be identified and re-verified.
"""

from pydantic import PositiveInt, model_validator

from researchd.artifacts.hashing import sha256_bytes
from researchd.artifacts.provenance import canonical_json
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.domain.base import DomainModel
from researchd.runtime_sessions.contracts import RuntimeLaunchProfile


class AgentDefinition(DomainModel):
    """A referentially closed, versioned bundle of one agent's launch surface."""

    definition_version: PositiveInt = 1
    profile: AgentProfile
    runtimes: tuple[AgentRuntime, ...] = ()
    launch_profiles: tuple[RuntimeLaunchProfile, ...] = ()

    @model_validator(mode="after")
    def _referentially_closed(self) -> "AgentDefinition":
        runtime_ids = {runtime.runtime_id for runtime in self.runtimes}
        for runtime in self.runtimes:
            if runtime.agent_id != self.profile.agent_id:
                raise ValueError(
                    f"runtime {runtime.runtime_id} belongs to another agent",
                )
        for launch_profile in self.launch_profiles:
            if launch_profile.runtime_id not in runtime_ids:
                raise ValueError(
                    f"launch profile targets unknown runtime {launch_profile.runtime_id}",
                )
        return self

    def definition_sha256(self) -> str:
        """Digest the canonical form of the whole definition."""
        payload = {
            "definition_version": self.definition_version,
            "profile": self.profile.model_dump(mode="json"),
            "runtimes": [runtime.model_dump(mode="json") for runtime in self.runtimes],
            "launch_profiles": [
                profile.model_dump(mode="json") for profile in self.launch_profiles
            ],
        }
        return sha256_bytes(canonical_json(payload).encode("utf-8"))


__all__ = ["AgentDefinition"]
