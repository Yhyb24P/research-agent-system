"""Detached, projection-only terminal clients for collaboration workspaces."""

import json
from pathlib import Path
from typing import Literal

from researchd.client.lifecycle import (
    DaemonNotReadyError,
    base_url_for,
    load_client_config,
    probe_health,
    spawn_daemon,
    wait_for_ready,
)
from researchd.client.transport import ResearchClient, TransportError, load_owner_token


ConsoleKind = Literal["collab", "agent", "system"]


def console_entry(
    config_path: Path,
    kind: ConsoleKind,
    *,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> int:
    """Run a standalone read client; quitting it never stops researchd."""
    if kind == "agent" and agent_id is None:
        raise ValueError("agent console requires an Agent ID")
    config = load_client_config(config_path)
    health = probe_health(config)
    if health is None:
        spawn_daemon(config, config_path)
    if health is None or health.get("ready") is not True:
        try:
            wait_for_ready(config)
        except (DaemonNotReadyError, TimeoutError) as error:
            print(f"researchd is not ready: {error}")
            return 1
    client = ResearchClient(base_url_for(config), load_owner_token(config.state_root))
    try:
        selected_run = run_id or _select_run(client)
        print("detached live research console; press Ctrl-C to close this view")
        print(_render(client, kind, agent_id=agent_id, run_id=selected_run))
        path = (
            f"/api/runs/{selected_run}/stream"
            if selected_run is not None
            else "/api/system-stream"
        )
        after: int | None = None
        try:
            while True:
                for frame in client.stream(path, after=after, follow=True):
                    if frame.offset is not None:
                        after = frame.offset
                    print(_render(client, kind, agent_id=agent_id, run_id=selected_run))
        except KeyboardInterrupt:
            pass
        except TransportError as error:
            print(f"console stream failed: {error}")
    finally:
        client.close()
    return 0


def _select_run(client: ResearchClient) -> str | None:
    runs = client.get("/api/runs")
    if not runs:
        return None
    active = [item for item in runs if item.get("state") not in {"COMPLETED", "FAILED", "CANCELLED"}]
    selected = active[-1] if active else runs[-1]
    return str(selected["run_id"])


def _render(
    client: ResearchClient,
    kind: ConsoleKind,
    *,
    agent_id: str | None,
    run_id: str | None,
) -> str:
    if kind == "collab":
        if run_id is None:
            payload = {"runs": client.get("/api/runs"), "handoffs": client.get("/api/handoffs")}
        else:
            payload = {
                "run": client.get(f"/api/runs/{run_id}"),
                "messages": client.get(f"/api/runs/{run_id}/messages")["messages"],
                "handoffs": client.get("/api/handoffs", params={"run": run_id}),
            }
    elif kind == "agent":
        assert agent_id is not None
        payload = client.get(
            f"/api/agents/{agent_id}/console",
            params={"run": run_id} if run_id else None,
        )
    else:
        payload = {
            "health": client.health(),
            "runtime_sessions": client.get("/api/runtime-sessions"),
            "daemon_commands": client.get("/api/daemon-commands"),
        }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


__all__ = ["ConsoleKind", "console_entry"]
