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
        print("detached research console; Enter/r refreshes, q quits")
        while True:
            try:
                print(_render(client, kind, agent_id=agent_id, run_id=run_id))
            except TransportError as error:
                print(f"console refresh failed: {error}")
            try:
                command = input().strip().lower()
            except EOFError:
                break
            if command in {"q", "quit", "exit"}:
                break
            if command not in {"", "r", "refresh"}:
                print("Enter/r refreshes; q quits")
    finally:
        client.close()
    return 0


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
