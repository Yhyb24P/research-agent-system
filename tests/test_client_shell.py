"""PX02-04: research shell parser and first command batch."""

import re
from typing import Any

import pytest

from researchd.client.shell import (
    ParsedCommand,
    ShellParseError,
    parse_line,
    resolve_agent_reference,
    run_shell,
)
from researchd.client.transport import StreamFrame


class _FakeClient:
    """Records transport calls and answers with canned projections."""

    def __init__(self) -> None:
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.streams: list[tuple[str, dict[str, Any]]] = []
        self.agents: list[dict[str, Any]] = [
            {
                "agent_id": "agent_alpha",
                "display_name": "Alpha",
                "roles": ("executor",),
                "enabled": True,
            },
            {
                "agent_id": "agent_beta",
                "display_name": "Beta",
                "roles": ("reviewer",),
                "enabled": False,
            },
        ]
        self.runs: list[dict[str, Any]] = [
            {
                "run_id": "run_one",
                "state": "ACTIVE",
                "work_orders": [],
                "pending_approval_ids": [],
            }
        ]

    def health(self) -> dict[str, Any]:
        return {"state": "READY", "ready": True}

    def get(self, path: str, **kwargs: Any) -> Any:
        self.gets.append(path)
        if path == "/api/agents":
            return self.agents
        if path == "/api/runs":
            return self.runs
        return []

    def post_command(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        self.posts.append((path, dict(payload or {})))
        return {"status": "ACCEPTED", "command_id": command_id or "cmd_fake"}

    def stream(self, path: str, **kwargs: Any) -> Any:
        self.streams.append((path, kwargs))
        if len(self.streams) == 1:
            return iter([StreamFrame(1, {"event_type": "RUN_CREATED"})])
        raise KeyboardInterrupt


def _drive(lines: list[str], client: _FakeClient) -> list[str]:
    output: list[str] = []
    iterator = iter(lines)

    def scripted_input() -> str:
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError

    run_shell(client, input_fn=scripted_input, print_fn=output.append)
    return output


def test_parse_status_quit_and_empty_line() -> None:
    assert parse_line("status") == ParsedCommand("status", (), {})
    assert parse_line("quit") == ParsedCommand("quit", (), {})
    assert parse_line("exit") == ParsedCommand("quit", (), {})
    assert parse_line("   ") == ParsedCommand("", (), {})


def test_parse_agent_subcommands() -> None:
    assert parse_line("agent list") == ParsedCommand("agent list", (), {})
    assert parse_line("agent use agent_alpha") == ParsedCommand(
        "agent use", ("agent_alpha",), {}
    )
    assert parse_line("agent remove agent_alpha") == ParsedCommand(
        "agent remove", ("agent_alpha",), {}
    )
    with pytest.raises(ShellParseError):
        parse_line("agent use")
    with pytest.raises(ShellParseError):
        parse_line("agent use a b")
    with pytest.raises(ShellParseError):
        parse_line("agent frobnicate")
    with pytest.raises(ShellParseError):
        parse_line("agent")


def test_parse_run_list() -> None:
    assert parse_line("run list") == ParsedCommand("run list", (), {})
    with pytest.raises(ShellParseError):
        parse_line("run list extra")
    with pytest.raises(ShellParseError):
        parse_line("run")


def test_parse_task_create_and_cancel() -> None:
    parsed = parse_line('task create ws_main "ship the report"')
    assert parsed.name == "task create"
    assert parsed.args == ("ws_main", "ship the report")
    with pytest.raises(ShellParseError):
        parse_line("task create ws_main")
    assert parse_line("task cancel run_one") == ParsedCommand(
        "task cancel", ("run_one",), {}
    )
    with pytest.raises(ShellParseError):
        parse_line("task cancel")
    with pytest.raises(ShellParseError):
        parse_line("task rename run_one")


def test_parse_msg_with_options() -> None:
    parsed = parse_line(
        "msg run_one hello world --to agent_alpha --classification SECRET"
    )
    assert parsed.name == "msg"
    assert parsed.args == ("run_one", "hello", "world")
    assert parsed.options == {"to": "agent_alpha", "classification": "SECRET"}
    with pytest.raises(ShellParseError):
        parse_line("msg run_one")
    with pytest.raises(ShellParseError):
        parse_line("msg run_one body --bogus value")
    with pytest.raises(ShellParseError):
        parse_line("msg run_one body --to")
    with pytest.raises(ShellParseError):
        parse_line("msg run_one body --classification BOGUS")


def test_parse_events_watch() -> None:
    assert parse_line("events watch") == ParsedCommand("events watch", (), {})
    assert parse_line("events watch run_one") == ParsedCommand(
        "events watch", ("run_one",), {}
    )
    with pytest.raises(ShellParseError):
        parse_line("events watch a b")
    with pytest.raises(ShellParseError):
        parse_line("events follow")


def test_parse_approve_and_reject() -> None:
    assert parse_line("approve wo_one grant_one") == ParsedCommand(
        "approve", ("wo_one", "grant_one"), {}
    )
    assert parse_line("reject wo_one appr_one") == ParsedCommand(
        "reject", ("wo_one", "appr_one"), {}
    )
    with pytest.raises(ShellParseError):
        parse_line("approve wo_one")
    with pytest.raises(ShellParseError):
        parse_line("reject wo_one a b")


def test_unknown_command_and_unbalanced_quotes_are_rejected() -> None:
    with pytest.raises(ShellParseError):
        parse_line("frobnicate")
    with pytest.raises(ShellParseError):
        parse_line('task create ws_main "unbalanced')


def test_resolve_agent_reference_matches_id_then_display_name() -> None:
    agents = [
        {"agent_id": "agent_alpha", "display_name": "Alpha", "enabled": True},
        {"agent_id": "agent_beta", "display_name": "Beta", "enabled": True},
    ]
    assert resolve_agent_reference(agents, "agent_alpha")["agent_id"] == "agent_alpha"
    assert resolve_agent_reference(agents, " beta ")["agent_id"] == "agent_beta"
    with pytest.raises(ShellParseError):
        resolve_agent_reference(agents, "nobody")


def test_shell_status_and_agent_working_set() -> None:
    client = _FakeClient()
    output = _drive(["status", "agent list", "agent use alpha", "quit"], client)
    assert "state=READY ready=True" in output
    assert "agent_alpha  enabled=True  Alpha  roles=executor" in output
    # Display names resolve case- and whitespace-insensitively.
    assert "current agent: agent_alpha" in output


def test_shell_agent_remove_clears_the_current_agent() -> None:
    client = _FakeClient()
    output = _drive(
        ["agent use alpha", "agent remove agent_alpha", "quit"], client
    )
    assert "current agent: agent_alpha" in output
    assert "removed agent_alpha from the working set" in output


def test_shell_rejects_disabled_agent_selection() -> None:
    client = _FakeClient()
    output = _drive(["agent use beta", "quit"], client)
    assert "agent agent_beta is disabled" in output


def test_shell_task_create_posts_the_typed_request() -> None:
    client = _FakeClient()
    _drive(['task create ws_main ship "the report"', "quit"], client)
    path, payload = client.posts[-1]
    assert path == "/api/runs"
    assert payload["workspace_id"] == "ws_main"
    assert payload["objective"] == "ship the report"


def test_shell_task_cancel_targets_the_run_route() -> None:
    client = _FakeClient()
    _drive(["task cancel run_one", "quit"], client)
    path, payload = client.posts[-1]
    assert path == "/api/runs/run_one/cancel"
    assert payload == {}


def test_shell_msg_generates_a_message_identity() -> None:
    client = _FakeClient()
    _drive(["msg run_one hello --to agent_alpha", "quit"], client)
    path, payload = client.posts[-1]
    assert path == "/api/collaboration-messages"
    assert re.fullmatch(r"msg_[0-9a-f]{32}", payload["message_id"])
    assert payload["run_id"] == "run_one"
    assert payload["body"] == "hello"
    assert payload["recipient_agent_id"] == "agent_alpha"
    assert payload["purpose"] == "operator-message"
    # The server applies the PROJECT_PRIVATE default when omitted.
    assert "classification" not in payload


def test_shell_approve_and_reject_payloads() -> None:
    client = _FakeClient()
    _drive(["approve wo_one grant_one", "reject wo_two appr_two", "quit"], client)
    assert client.posts[0] == (
        "/api/work-orders/wo_one/approve",
        {"grant_id": "grant_one"},
    )
    assert client.posts[1] == (
        "/api/work-orders/wo_two/reject",
        {"approval_id": "appr_two"},
    )


def test_shell_survives_parse_and_transport_errors() -> None:
    client = _FakeClient()
    output = _drive(["frobnicate", "agent use nobody", "quit"], client)
    assert "parse error: unknown command: frobnicate" in output
    assert "error: unknown agent reference: nobody" in output


def test_shell_streams_events_with_follow() -> None:
    client = _FakeClient()
    output = _drive(["events watch", "quit"], client)
    assert any("watching /api/system-stream" in line for line in output)
    assert '[1] {"event_type": "RUN_CREATED"}' in output
    assert client.streams == [
        ("/api/system-stream", {"after": None, "follow": True}),
        ("/api/system-stream", {"after": 1, "follow": True}),
    ]
    assert "stopped watching" in output
