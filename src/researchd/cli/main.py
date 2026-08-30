"""Minimal local status-view CLI wiring for a supplied LocalControlAPI."""

import argparse
import asyncio
import json
from pathlib import Path
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from researchd.api.control import LocalControlAPI
from researchd.daemon.contracts import DaemonCommandResolveCommand
from researchd.daemon.reconciliation import (
    DaemonCommandResolutionService,
    build_builtin_observers,
)
from researchd.storage.db import create_sqlite_engine, session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="researchctl")
    parser.add_argument("--database", default="researchd.db", help="path to the controller SQLite database")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="show a run or WorkOrder status")
    status.add_argument("run_id")
    events = subparsers.add_parser("events", help="show the append-only run trace")
    events.add_argument("first")
    events.add_argument("second", nargs="?")
    events.add_argument("--after", dest="after_stream_offset", type=int, help="resume after a stream_offset")
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
    run_sub.add_parser("list")
    run_status = run_sub.add_parser("status")
    run_status.add_argument("run_id")
    receipt = subparsers.add_parser(
        "daemon-command",
        help="inspect and resolve durable daemon command receipts",
    )
    receipt_sub = receipt.add_subparsers(dest="receipt_command", required=True)
    receipt_list = receipt_sub.add_parser("list", help="list durable command receipts")
    receipt_list.add_argument(
        "--status",
        choices=["ACCEPTED", "COMPLETED", "REJECTED"],
        help="filter by receipt status",
    )
    receipt_resolve = receipt_sub.add_parser(
        "resolve",
        help="converge an ACCEPTED receipt through command-specific observation",
    )
    receipt_resolve.add_argument("target_command_id")
    receipt_resolve.add_argument(
        "--resource-ref",
        action="append",
        metavar="KEY=VALUE",
        help="family-specific resource identity, repeatable",
    )
    receipt_resolve.add_argument(
        "--abandon",
        action="store_true",
        help="abandon an undetermined outcome (OPERATOR_ABANDONED)",
    )
    receipt_resolve.add_argument(
        "--command-id",
        dest="command_id",
        help="stable identity for idempotent retries",
    )
    return parser


def _dispatch_args(api: LocalControlAPI, args: argparse.Namespace) -> dict[str, Any] | list[dict[str, Any]]:
    if args.command == "status":
        return api.run_status(args.run_id)
    if args.command == "events":
        return api.events(args.second if args.first == "watch" else args.first, after_stream_offset=args.after_stream_offset)
    if args.command == "agent":
        return api.agents() if args.agent_command == "list" else api.agent(args.agent_id)
    if args.command == "delegation":
        return api.delegations(args.run_id) if args.delegation_command == "list" else api.delegation(args.delegation_id)
    if args.command == "run":
        return api.runs() if args.run_command == "list" else api.run_status(args.run_id)
    return asyncio.run(api.cancel_run(args.run_id))


def dispatch(api: LocalControlAPI, argv: list[str] | None = None) -> dict[str, Any] | list[dict[str, Any]]:
    return _dispatch_args(api, build_parser().parse_args(argv))


def _parse_resource_ref(items: list[str] | None) -> dict[str, str]:
    resource_ref: dict[str, str] = {}
    for item in items or []:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise SystemExit("--resource-ref expects KEY=VALUE")
        resource_ref[key] = value
    return resource_ref


def _daemon_command(args: argparse.Namespace) -> int:
    database = Path(args.database)
    if not database.is_file():
        raise SystemExit(f"controller database does not exist: {database}")
    sessions = session_factory(create_sqlite_engine(database))
    if args.receipt_command == "list":
        payload = LocalControlAPI(sessions).daemon_commands(args.status)
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return 0
    command = DaemonCommandResolveCommand(
        command_id=args.command_id or f"resolve_{uuid4().hex}",
        actor_type="HUMAN",
        actor_id="researchctl",
        target_command_id=args.target_command_id,
        resource_ref=_parse_resource_ref(args.resource_ref),
        abandon=args.abandon,
    )
    service = DaemonCommandResolutionService(sessions, build_builtin_observers(sessions))
    result = service.resolve(command)
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True, ensure_ascii=False))
    return 0 if result.status == "ACCEPTED" else 1


def main(
    api_factory: Callable[[], LocalControlAPI] | None = None,
    argv: list[str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "daemon-command":
        return _daemon_command(args)
    if api_factory is None:
        database = Path(args.database)
        if not database.is_file():
            raise SystemExit(f"controller database does not exist: {database}")
        api = LocalControlAPI(session_factory(create_sqlite_engine(database)))
    else:
        api = api_factory()
    payload = _dispatch_args(api, args)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return 0


def entrypoint() -> int:
    return main()


__all__ = ["build_parser", "dispatch", "entrypoint", "main"]
