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
    events.add_argument("first")
    events.add_argument("second", nargs="?")
    events.add_argument("--after", dest="after_event_id")
    cancel = subparsers.add_parser("cancel", help="request run cancellation")
    cancel.add_argument("run_id")
    agent = subparsers.add_parser("agent", help="inspect registered agents")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("list")
    agent_inspect = agent_sub.add_parser("inspect")
    agent_inspect.add_argument("agent_id")
    delegation = subparsers.add_parser("delegation", help="inspect delegations")
    delegation_sub = delegation.add_subparsers(dest="delegation_command", required=True)
    delegation_list = delegation_sub.add_parser("list")
    delegation_list.add_argument("--run", dest="run_id")
    delegation_show = delegation_sub.add_parser("show")
    delegation_show.add_argument("delegation_id")
    run = subparsers.add_parser("run", help="inspect a research run")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    run_status = run_sub.add_parser("status")
    run_status.add_argument("run_id")
    return parser


def dispatch(api: LocalControlAPI, argv: list[str] | None = None) -> dict[str, Any] | list[dict[str, Any]]:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        return api.run_status(args.run_id)
    if args.command == "events":
        return api.events(args.second if args.first == "watch" else args.first, after_event_id=args.after_event_id)
    if args.command == "agent":
        return api.agents() if args.agent_command == "list" else api.agent(args.agent_id)
    if args.command == "delegation":
        return api.delegations(args.run_id) if args.delegation_command == "list" else api.delegation(args.delegation_id)
    if args.command == "run":
        return api.run_status(args.run_id)
    return asyncio.run(api.cancel_run(args.run_id))


def main(api_factory: Callable[[], LocalControlAPI]) -> int:
    payload = dispatch(api_factory())
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return 0


__all__ = ["build_parser", "dispatch", "main"]
