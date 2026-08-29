"""Small terminal rendering client over the same LocalControlAPI resources."""
from typing import Any
from researchd.api.control import LocalControlAPI


def render_tui(api: LocalControlAPI, *, run_id: str | None = None) -> str:
    agents = api.agents()
    lines = ["Agents", "======"]
    for agent in agents:
        lines.append(f"{agent['agent_id']}  {agent['display_name']}  [{agent['trust_zone']}] enabled={agent['enabled']}")
        for runtime in agent["runtimes"]:
            provider = runtime["model_provider"] or "-"
            model = runtime["model_name"] or "-"
            lines.append(f"  Runtime {runtime['runtime_id']}  adapter={runtime['adapter_kind']}  provider={provider}  model={model}  enabled={runtime['enabled']}")
    lines.extend(["", "Research Runs", "============="])
    if run_id is not None:
        status = api.run_status(run_id)
        lines.append(f"{run_id}  state={status['state']}  active_attempts={len(status['active_attempt_ids'])}")
        lines.extend(["", "Timeline", "========"])
        for item in api.timeline(run_id):
            lines.append(f"{item['timestamp']}  {item['kind']}  {item['entity_id']}")
        delegations = api.delegations(run_id)
        approvals = api.approvals(run_id)
        artifacts = api.artifacts(run_id)
        lines.extend(["", "Delegations", "==========="])
        lines.extend(f"{item['delegation_id']}  purpose={item['purpose']}  state={item['state']}  agent={item['assigned_agent_id']}" for item in delegations)
        lines.extend(["", "Approvals", "=========", f"count={len(approvals)}"])
        lines.extend(["", "Artifacts", "=========", f"count={len(artifacts)}"])
    lines.extend(["", "System", "======", "Use the local control API for structured details."])
    return "\n".join(lines) + "\n"
