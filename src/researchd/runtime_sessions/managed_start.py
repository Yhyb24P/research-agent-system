"""Agent-scoped resolution for managed runtime session launches.

The public ManagedAgentStart intent only names an Agent (and optionally one
of its runtimes). The daemon resolves the launch spec from the trusted
launch catalog and produces the internal RuntimeSession start/attach
command; callers never supply a launch body.
"""

import hashlib
from typing import Literal, cast

from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import AgentAdapterKind
from researchd.domain.ids import RuntimeSessionId
from researchd.runtime_sessions.contracts import (
    RuntimeSessionAttachCommand,
    RuntimeSessionStartCommand,
)
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService


def _session_id_for(command_id: str) -> RuntimeSessionId:
    """Derive a stable session identity so command replays map to one session."""
    digest = hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:32]
    return RuntimeSessionId(f"runtime_session_managed_{digest}")


class ManagedAgentStartService:
    """Resolve an agent-scoped start intent into an internal session command."""

    def __init__(
        self,
        registry: AgentRegistryService,
        launch_profiles: RuntimeLaunchProfileService,
    ) -> None:
        self.registry = registry
        self.launch_profiles = launch_profiles

    def resolve(
        self,
        agent_id: str,
        runtime_id: str | None,
        *,
        command_id: str,
        actor_type: str,
        actor_id: str,
    ) -> RuntimeSessionStartCommand | RuntimeSessionAttachCommand:
        agent = self.registry.get_agent(agent_id)
        if not agent.enabled:
            raise ValueError(f"agent is disabled: {agent_id}")
        if runtime_id is not None:
            runtime = self.registry.require_enabled_runtime(runtime_id)
            if str(runtime.agent_id) != agent_id:
                raise ValueError(f"runtime does not belong to agent: {runtime_id}")
        else:
            candidates = self.registry.list_enabled_runtimes(agent_id)
            if len(candidates) != 1:
                raise ValueError(
                    "runtime_id is required: agent has "
                    f"{len(candidates)} enabled runtimes"
                )
            runtime = candidates[0]
        # The authority boundary accepts a plain actor label; the internal
        # command validates it against the trusted literal union.
        actor = cast("Literal['HUMAN', 'SYSTEM']", actor_type)
        if runtime.adapter_kind is AgentAdapterKind.PROCESS:
            process_launch = self.launch_profiles.resolve_process(str(runtime.runtime_id))
            return RuntimeSessionStartCommand(
                command_id=command_id,
                runtime_session_id=_session_id_for(command_id),
                runtime_id=runtime.runtime_id,
                actor_type=actor,
                actor_id=actor_id,
                launch_spec=process_launch.launch_spec,
                launch_profile_sha256=process_launch.spec_sha256,
            )
        if runtime.adapter_kind is AgentAdapterKind.HTTP:
            remote_launch = self.launch_profiles.resolve_remote_http(str(runtime.runtime_id))
            return RuntimeSessionAttachCommand(
                command_id=command_id,
                runtime_session_id=_session_id_for(command_id),
                runtime_id=runtime.runtime_id,
                actor_type=actor,
                actor_id=actor_id,
                launch_spec=remote_launch.launch_spec,
                launch_profile_sha256=remote_launch.spec_sha256,
            )
        raise ValueError(
            f"launch mode is not supported for adapter: {runtime.adapter_kind}"
        )


__all__ = ["ManagedAgentStartService", "_session_id_for"]
