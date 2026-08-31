"""Console entrypoint for the daily ``research`` client.

``research init`` and ``research status`` are lifecycle commands; with
no subcommand the client reaches a READY daemon (spawning
``researchd serve`` when needed) and enters the interactive shell.
The first shell commands land in PX02-04.
"""

import argparse
from pathlib import Path

from researchd.client.lifecycle import interactive_entry, open_browser, run_init, run_status, stop_daemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research",
        description="Daily client for the researchd control daemon",
    )
    parser.add_argument("--config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return run_init(args.config)
    if args.command == "status":
        return run_status(args.config)
    if args.command == "daemon":
        if args.action == "status":
            return run_status(args.config)
        stopped = stop_daemon(args.config)
        if stopped != 0 or args.action == "stop":
            return stopped
        return interactive_entry(args.config, input_fn=lambda: "quit")
    if args.command == "tui":
        from researchd.client.tui import tui_entry

        return tui_entry(args.config)
    if args.command == "browser":
        return open_browser(args.config)
    if args.command == "console":
        from researchd.client.console import console_entry

        if args.kind != "agent" and args.agent_id is not None:
            parser.error("only the agent console accepts an Agent ID")
        return console_entry(
            args.config, args.kind, agent_id=args.agent_id, run_id=args.run_id,
        )
    return interactive_entry(args.config)


def entrypoint() -> int:
    return main()


__all__ = ["build_parser", "entrypoint", "main"]
