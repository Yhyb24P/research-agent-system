"""Line-oriented interactive shell for the daily ``research`` client.

The first command batch (PX02-04) covers status, the session-local
agent working set, run listing, task creation/cancellation,
collaboration messages, event watching and work-order
approve/reject. Every operation crosses the authenticated transport;
the shell keeps no state of its own beyond the session-local working
set, and ``agent remove`` only drops an agent from that local set —
registered agents are never mutated or deleted by the client.
"""

import json
import shlex
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from researchd.client.transport import StreamFrame, TransportError

_CLASSIFICATIONS = ("PUBLIC", "CLOUD_SAFE", "PROJECT_PRIVATE", "LOCAL_ONLY", "SECRET")
_MESSAGE_PURPOSES = ("DISCUSSION", "STATUS", "QUESTION", "DIRECTIVE", "NOTICE")


class ShellTransport(Protocol):
    """The authenticated transport surface the shell executes against."""

    def health(self) -> dict[str, Any]: ...

    def get(self, path: str, *, params: dict[str, str] | None = None) -> Any: ...

    def post_command(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]: ...

    def stream(
        self,
        path: str,
        *,
        after: int | None = None,
        follow: bool = False,
    ) -> Iterator[StreamFrame]: ...


class ShellParseError(ValueError):
    """The line is not a command the shell understands."""


@dataclass(frozen=True)
class ParsedCommand:
    """One parsed shell line; ``name`` joins command and subcommand."""

    name: str
    args: tuple[str, ...]
    options: dict[str, str]


@dataclass
class _ShellState:
    current_agent: str | None = None
    working_set: list[str] = field(default_factory=list)


def parse_line(line: str) -> ParsedCommand:
    """Parse one shell line; options take the form ``--key value``."""
    try:
        tokens = shlex.split(line.strip())
    except ValueError as error:
        raise ShellParseError(f"unparseable line: {error}") from error
    if not tokens:
        return ParsedCommand("", (), {})
    head = tokens[0]
    if head in {"quit", "exit"}:
        _require_arity(head, tokens[1:], 0)
        return ParsedCommand("quit", (), {})
    if head == "status":
        _require_arity("status", tokens[1:], 0)
        return ParsedCommand("status", (), {})
    if head == "agent":
        return _parse_agent(tokens[1:])
    if head == "run":
        return _parse_simple_subcommand(head, tokens[1:], {"list": 0})
    if head == "task":
        return _parse_task(tokens[1:])
    if head == "events":
        return _parse_simple_subcommand(head, tokens[1:], {"watch": (0, 1)})
    if head == "msg":
        return _parse_msg(tokens[1:])
    if head == "handoff":
        return _parse_handoff(tokens[1:])
    if head in {"approve", "reject"}:
        _require_arity(head, tokens[1:], 2)
        return ParsedCommand(head, tuple(tokens[1:]), {})
    raise ShellParseError(f"unknown command: {head}")


def _parse_agent(tokens: list[str]) -> ParsedCommand:
    subcommands = {"list": 0, "use": 1, "remove": 1}
    return _parse_simple_subcommand("agent", tokens, subcommands)


def _parse_task(tokens: list[str]) -> ParsedCommand:
    if not tokens or tokens[0] not in {"create", "cancel"}:
        raise ShellParseError("task requires 'create' or 'cancel'")
    subcommand = tokens[0]
    rest = tokens[1:]
    if subcommand == "create":
        if len(rest) < 2:
            raise ShellParseError("task create requires a workspace id and an objective")
        return ParsedCommand("task create", tuple(rest), {})
    _require_arity("task cancel", rest, 1)
    return ParsedCommand("task cancel", tuple(rest), {})


def _parse_simple_subcommand(
    head: str,
    tokens: list[str],
    subcommands: Mapping[str, int | tuple[int, int]],
) -> ParsedCommand:
    if not tokens:
        raise ShellParseError(f"{head} requires a subcommand")
    subcommand = tokens[0]
    if subcommand not in subcommands:
        raise ShellParseError(f"unknown {head} subcommand: {subcommand}")
    rest = tokens[1:]
    arity = subcommands[subcommand]
    if isinstance(arity, tuple):
        minimum, maximum = arity
        if not minimum <= len(rest) <= maximum:
            raise ShellParseError(
                f"{head} {subcommand} accepts {minimum} to {maximum} argument(s)"
            )
    elif len(rest) != arity:
        expected = "no arguments" if arity == 0 else f"exactly {arity} argument"
        raise ShellParseError(f"{head} {subcommand} accepts {expected}")
    return ParsedCommand(f"{head} {subcommand}", tuple(rest), {})


def _parse_msg(tokens: list[str]) -> ParsedCommand:
    positionals: list[str] = []
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            key = token[2:]
            if key not in {
                "to",
                "purpose",
                "classification",
                "reply-to",
                "delegation",
                "invocation",
            }:
                raise ShellParseError(f"unknown msg option: --{key}")
            if index + 1 >= len(tokens):
                raise ShellParseError(f"option --{key} requires a value")
            options[key] = tokens[index + 1]
            index += 2
        else:
            positionals.append(token)
            index += 1
    if len(positionals) < 2:
        raise ShellParseError("msg requires a run id and a body")
    if "classification" in options and options["classification"] not in _CLASSIFICATIONS:
        raise ShellParseError(
            "classification must be one of: " + ", ".join(_CLASSIFICATIONS)
        )
    if "purpose" in options and options["purpose"] not in _MESSAGE_PURPOSES:
        raise ShellParseError("purpose must be one of: " + ", ".join(_MESSAGE_PURPOSES))
    return ParsedCommand("msg", tuple(positionals), options)


def _parse_handoff(tokens: list[str]) -> ParsedCommand:
    if not tokens or tokens[0] not in {"list", "accept", "reject"}:
        raise ShellParseError("handoff requires list, accept, or reject")
    subcommand, rest = tokens[0], tokens[1:]
    if subcommand == "list":
        if len(rest) > 1:
            raise ShellParseError("handoff list accepts zero or one run id")
        return ParsedCommand("handoff list", tuple(rest), {})
    options: dict[str, str] = {}
    positionals: list[str] = []
    index = 0
    while index < len(rest):
        if rest[index] == "--target":
            if subcommand != "accept" or index + 1 >= len(rest):
                raise ShellParseError("--target is only valid for handoff accept")
            options["target"] = rest[index + 1]
            index += 2
        elif rest[index].startswith("--"):
            raise ShellParseError(f"unknown handoff option: {rest[index]}")
        else:
            positionals.append(rest[index])
            index += 1
    if len(positionals) < 2:
        raise ShellParseError(f"handoff {subcommand} requires a proposal id and reason")
    return ParsedCommand(f"handoff {subcommand}", tuple(positionals), options)


def _require_arity(name: str, rest: list[str], expected: int) -> None:
    if len(rest) != expected:
        wording = "no arguments" if expected == 0 else f"exactly {expected} argument(s)"
        raise ShellParseError(f"{name} accepts {wording}")


def resolve_agent_reference(
    agents: list[dict[str, Any]],
    ref: str,
) -> dict[str, Any]:
    """Match an exact agent_id first, then a unique normalized display name."""
    exact = [item for item in agents if item.get("agent_id") == ref]
    if exact:
        return exact[0]
    normalized = ref.strip().lower()
    matches = [
        item
        for item in agents
        if str(item.get("display_name", "")).strip().lower() == normalized
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ShellParseError(f"ambiguous agent reference: {ref}")
    raise ShellParseError(f"unknown agent reference: {ref}")


def run_shell(
    client: ShellTransport,
    *,
    input_fn: Callable[[], str] = input,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Run the line loop until quit/exit/EOF; errors never kill the shell."""
    state = _ShellState()
    while True:
        try:
            line = input_fn()
        except EOFError:
            break
        line = line.strip()
        if not line:
            continue
        try:
            command = parse_line(line)
        except ShellParseError as error:
            print_fn(f"parse error: {error}")
            continue
        if command.name == "quit":
            break
        try:
            _execute(command, client, state, print_fn)
        except (TransportError, ShellParseError) as error:
            print_fn(f"error: {error}")


def _execute(
    command: ParsedCommand,
    client: ShellTransport,
    state: _ShellState,
    print_fn: Callable[[str], None],
) -> None:
    if command.name == "status":
        health = client.health()
        print_fn(f"state={health.get('state')} ready={health.get('ready')}")
    elif command.name == "agent list":
        agents = client.get("/api/agents")
        if not agents:
            print_fn("(no agents)")
        for item in agents:
            print_fn(
                f"{item['agent_id']}  enabled={item['enabled']}  "
                f"{item['display_name']}  roles={','.join(item['roles'])}"
            )
    elif command.name == "agent use":
        agent = resolve_agent_reference(client.get("/api/agents"), command.args[0])
        if not agent["enabled"]:
            print_fn(f"agent {agent['agent_id']} is disabled")
            return
        state.current_agent = agent["agent_id"]
        if agent["agent_id"] not in state.working_set:
            state.working_set.append(agent["agent_id"])
        print_fn(f"current agent: {agent['agent_id']}")
    elif command.name == "agent remove":
        agent = resolve_agent_reference(client.get("/api/agents"), command.args[0])
        agent_id = agent["agent_id"]
        if agent_id in state.working_set:
            state.working_set.remove(agent_id)
        if state.current_agent == agent_id:
            state.current_agent = None
        print_fn(f"removed {agent_id} from the working set")
    elif command.name == "run list":
        runs = client.get("/api/runs")
        if not runs:
            print_fn("(no runs)")
        for item in runs:
            print_fn(
                f"{item['run_id']}  {item['state']}  "
                f"work_orders={len(item['work_orders'])}  "
                f"pending_approvals={len(item['pending_approval_ids'])}"
            )
    elif command.name == "task create":
        workspace_id = command.args[0]
        objective = " ".join(command.args[1:])
        envelope = client.post_command(
            "/api/runs",
            {"workspace_id": workspace_id, "objective": objective},
        )
        print_fn(f"{envelope['status']} {envelope['command_id']}")
    elif command.name == "task cancel":
        envelope = client.post_command(f"/api/runs/{command.args[0]}/cancel", {})
        print_fn(f"{envelope['status']} {envelope['command_id']}")
    elif command.name == "msg":
        run_id = command.args[0]
        body = " ".join(command.args[1:])
        payload: dict[str, Any] = {
            "message_id": f"msg_{uuid4().hex}",
            "run_id": run_id,
            "purpose": command.options.get("purpose", "DISCUSSION"),
            "body": body,
        }
        if "to" in command.options:
            payload["recipient_agent_id"] = command.options["to"]
        if "classification" in command.options:
            payload["classification"] = command.options["classification"]
        if "reply-to" in command.options:
            payload["reply_to_message_id"] = command.options["reply-to"]
        if "delegation" in command.options:
            payload["delegation_id"] = command.options["delegation"]
        if "invocation" in command.options:
            payload["invocation_id"] = command.options["invocation"]
        envelope = client.post_command("/api/collaboration-messages", payload)
        print_fn(f"{envelope['status']} {envelope['command_id']}")
    elif command.name == "handoff list":
        params = {"run": command.args[0]} if command.args else None
        proposals = client.get("/api/handoffs", params=params)
        if not proposals:
            print_fn("(no handoff proposals)")
        for proposal in proposals:
            print_fn(
                f"{proposal['proposal_id']}  {proposal['status']}  "
                f"{proposal['requested_mode']}  source={proposal['source_agent_id']}"
            )
    elif command.name in {"handoff accept", "handoff reject"}:
        proposal_id = command.args[0]
        payload = {
            "decision": command.name.removeprefix("handoff "),
            "reason": " ".join(command.args[1:]),
        }
        if "target" in command.options:
            payload["target_agent_id"] = command.options["target"]
        envelope = client.post_command(
            f"/api/handoffs/{proposal_id}/decision", payload,
        )
        print_fn(f"{envelope['status']} {envelope['command_id']}")
    elif command.name == "events watch":
        target = command.args[0] if command.args else None
        path = f"/api/runs/{target}/stream" if target else "/api/system-stream"
        print_fn(f"watching {path}; press Ctrl-C to stop")
        after: int | None = None
        try:
            while True:
                for frame in client.stream(path, after=after, follow=True):
                    print_fn(f"[{frame.offset}] {json.dumps(frame.data, sort_keys=True)}")
                    if frame.offset is not None:
                        after = frame.offset
        except KeyboardInterrupt:
            print_fn("stopped watching")
    elif command.name in {"approve", "reject"}:
        work_order_id, reference = command.args
        if command.name == "approve":
            envelope = client.post_command(
                f"/api/work-orders/{work_order_id}/approve",
                {"grant_id": reference},
            )
        else:
            envelope = client.post_command(
                f"/api/work-orders/{work_order_id}/reject",
                {"approval_id": reference},
            )
        print_fn(f"{envelope['status']} {envelope['command_id']}")


__all__ = [
    "ParsedCommand",
    "ShellParseError",
    "ShellTransport",
    "parse_line",
    "resolve_agent_reference",
    "run_shell",
]
