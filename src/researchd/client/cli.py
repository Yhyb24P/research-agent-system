"""Console entrypoint for the daily ``research`` client.

``research init`` and ``research status`` are lifecycle commands; with
no subcommand the client reaches a READY daemon (spawning
``researchd serve`` when needed) and enters the interactive shell.
The first shell commands land in PX02-04.
"""

import argparse
from pathlib import Path

from researchd.client.lifecycle import interactive_entry, run_init, run_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research",
        description="Daily client for the researchd control daemon",
    )
    parser.add_argument("--config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("init", help="bootstrap state via researchd init")
    subparsers.add_parser("status", help="report daemon reachability and readiness")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return run_init(args.config)
    if args.command == "status":
        return run_status(args.config)
    return interactive_entry(args.config)


def entrypoint() -> int:
    return main()


__all__ = ["build_parser", "entrypoint", "main"]
