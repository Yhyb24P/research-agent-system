"""Minimal local status-view CLI wiring for a supplied LocalControlAPI."""

import argparse
import asyncio
import json
from collections.abc import Callable
from typing import Any

from researchd.api.control import LocalControlAPI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="researchd")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="show a run or WorkOrder status")
    status.add_argument("run_id")
    events = subparsers.add_parser("events", help="show the append-only run trace")
    events.add_argument("run_id")
    cancel = subparsers.add_parser("cancel", help="request run cancellation")
    cancel.add_argument("run_id")
    return parser


def dispatch(api: LocalControlAPI, argv: list[str] | None = None) -> dict[str, Any] | list[dict[str, Any]]:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        return api.run_status(args.run_id)
    if args.command == "events":
        return api.events(args.run_id)
    return asyncio.run(api.cancel_run(args.run_id))


def main(api_factory: Callable[[], LocalControlAPI]) -> int:
    payload = dispatch(api_factory())
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return 0


__all__ = ["build_parser", "dispatch", "main"]
