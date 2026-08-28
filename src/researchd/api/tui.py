"""Small terminal rendering client over the same LocalControlAPI resources."""
from typing import Any
from researchd.api.control import LocalControlAPI


def render_tui(api: LocalControlAPI, *, run_id: str | None = None) -> str:
    agents = api.agents()
    lines = ["Agents", "======"]
    for agent in agents:
        lines.append(f"{agent['agent_id']}  {agent['display_name']}  [{agent['trust_zone']}] enabled={agent['enabled']}")
    lines.extend(["", "Research Runs", "============="])
    if run_id is not None:
        status = api.run_status(run_id)
        lines.append(f"{run_id}  state={status['state']}  active_attempts={len(status['active_attempt_ids'])}")
    lines.extend(["", "Approvals / Artifacts / System", "==============================", "Use the local control API for structured details."])
    return "\n".join(lines) + "\n"
