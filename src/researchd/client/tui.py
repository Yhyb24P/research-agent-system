"""Lifecycle wrapper for the optional Textual collaboration workspace."""

from pathlib import Path

from researchd.client.lifecycle import DaemonNotReadyError, base_url_for, load_client_config, probe_health, spawn_daemon, wait_for_ready
from researchd.client.transport import ResearchClient, load_owner_token


def tui_entry(config_path: Path) -> int:
    try:
        from researchd.client.tui_app import run_tui
    except ImportError:
        print("Textual is not installed; install the project with the 'tui' extra")
        return 1
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
        run_tui(client, config_path=config_path)
    finally:
        client.close()
    return 0


__all__ = ["tui_entry"]
