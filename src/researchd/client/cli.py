"""Console entrypoint for the daily ``research`` client.

``--config`` is optional: the effective config is discovered through
``researchd.client.config_discovery`` (``--config`` > ``RESEARCH_CONFIG`` >
trusted XDG/global config). Repository-local configs are only used when
explicitly named. With no subcommand the client reaches a READY daemon
(spawning ``researchd serve`` when needed) and opens the Textual workspace
when available, falling back to the interactive shell otherwise.
"""

import argparse
from collections.abc import Callable
from pathlib import Path

from researchd.client.agent_management import (
    add_aweswitch_agent,
    default_profile_ref,
    discover_aweswitch_profiles,
    list_agents,
    remove_agent,
)
from researchd.client.bootstrap import run_doctor, run_setup
from researchd.client.config_discovery import resolve_config_path
from researchd.bridge.aweswitch_agent import default_aweswitch_config
from researchd.client.lifecycle import interactive_entry, open_browser, run_init, run_status, stop_daemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research",
        description="Daily client for the researchd control daemon",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="explicit config path (default: RESEARCH_CONFIG or the trusted global config)",
    )
    subparsers = parser.add_subparsers(dest="command")
    setup = subparsers.add_parser("setup", help="create a trusted local Preview profile")
    setup.add_argument("--project", type=Path, default=None)
    setup.add_argument("--yes", action="store_true", help="accept the detected Git project")
    setup.add_argument("--port", type=int, default=8788)
    subparsers.add_parser("doctor", help="inspect the local Preview installation")
    agent = subparsers.add_parser("agent", help="manage trusted Preview Agents")
    agent_commands = agent.add_subparsers(dest="agent_action", required=True)
    agent_commands.add_parser("list", help="list installed Agents")
    agent_commands.add_parser("profiles", help="list non-secret aweswitch profiles")
    for action in ("add", "refresh"):
        command = agent_commands.add_parser(action, help=f"{action} a generated Agent")
        command.add_argument("role", choices=("planner", "coder", "reviewer"))
        command.add_argument("--profile", default=None)
    remove = agent_commands.add_parser("remove", help="disable an installed Agent")
    remove.add_argument("role", choices=("planner", "coder", "reviewer"))
    subparsers.add_parser("init", help="bootstrap state via researchd init")
    subparsers.add_parser("status", help="report daemon reachability and readiness")
    daemon = subparsers.add_parser("daemon", help="inspect or control the local daemon")
    daemon.add_argument("action", choices=("status", "stop", "restart"))
    subparsers.add_parser("tui", help="open the optional collaboration workspace")
    subparsers.add_parser("browser", help="open the local Browser Control Tower")
    console = subparsers.add_parser("console", help="open a detached projection console")
    console.add_argument("kind", choices=("collab", "agent", "system"))
    console.add_argument("agent_id", nargs="?")
    console.add_argument("--run", dest="run_id")
    return parser


def default_entry(config_path: Path) -> int:
    """Open the Textual workspace when available, else the line shell."""
    try:
        from researchd.client.tui_app import run_tui as _probe  # noqa: F401
    except ImportError:
        return interactive_entry(config_path)
    from researchd.client.tui import tui_entry

    return tui_entry(config_path)


def _require_config(
    args: argparse.Namespace,
    *,
    print_fn: Callable[[str], None] = print,
) -> Path | None:
    """Resolve the effective config path or print first-run guidance."""
    config = resolve_config_path(args.config)
    if config is None:
        print_fn(
            "no research configuration found; run `research setup` to create "
            "the trusted global config, or pass --config <path>"
        )
    return config


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "setup":
        result = run_setup(
            project=args.project,
            config_path=args.config,
            port=args.port,
            assume_yes=args.yes,
        )
        return 0 if result is not None else 1
    if args.command == "doctor":
        return run_doctor(args.config)
    if args.command is None:
        config = resolve_config_path(args.config)
        if config is None:
            result = run_setup()
            if result is None:
                return 2
            config = result.config_path
    else:
        config = _require_config(args)
        if config is None:
            return 2
    if args.command == "agent":
        if args.agent_action == "list":
            return list_agents(config)
        if args.agent_action == "profiles":
            try:
                profiles = discover_aweswitch_profiles(default_aweswitch_config())
            except ValueError as error:
                print(f"cannot read aweswitch profiles: {error}")
                return 1
            for profile in profiles:
                support = "managed" if profile["managed_bridge_supported"] else "metadata-only"
                print(f"{profile['provider']}:{profile['profile']}  {support}")
            return 0
        if args.agent_action == "remove":
            return remove_agent(config, args.role)
        profile_ref = args.profile or default_profile_ref()
        if profile_ref is None:
            print("select a supported profile with --profile aweswitch:<profile>")
            return 1
        return add_aweswitch_agent(config, args.role, profile_ref)
    if args.command == "init":
        return run_init(config)
    if args.command == "status":
        return run_status(config)
    if args.command == "daemon":
        if args.action == "status":
            return run_status(config)
        stopped = stop_daemon(config)
        if stopped != 0 or args.action == "stop":
            return stopped
        return interactive_entry(config, input_fn=lambda: "quit")
    if args.command == "tui":
        from researchd.client.tui import tui_entry

        return tui_entry(config)
    if args.command == "browser":
        return open_browser(config)
    if args.command == "console":
        from researchd.client.console import console_entry

        if args.kind != "agent" and args.agent_id is not None:
            parser.error("only the agent console accepts an Agent ID")
        return console_entry(
            config, args.kind, agent_id=args.agent_id, run_id=args.run_id,
        )
    return default_entry(config)


def entrypoint() -> int:
    return main()


__all__ = ["build_parser", "default_entry", "entrypoint", "main"]
