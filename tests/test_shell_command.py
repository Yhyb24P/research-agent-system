"""Tests for /shell TUI command translation, agent-start dispatch, TUI layout,
language rendering, and daemon identity zombie/PID-reuse protection."""

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest

from researchd.client.transport import ResearchClient
from researchd.client.tui_app import ResearchWorkspace, _text
from researchd.daemon.identity import claim, current_identity, is_live


class _FakeClient:
    """Records HTTP calls; answers agent listing and start commands."""

    def __init__(self) -> None:
        self.agents: list[dict[str, Any]] = [
            {
                "agent_id": "agent_coder",
                "display_name": "Coder",
                "enabled": True,
                "runtimes": [],
            },
        ]
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def health(self) -> dict[str, Any]:
        return {"state": "READY", "ready": True}

    def get(self, path: str, **kwargs: Any) -> Any:
        if path == "/api/agents":
            return list(self.agents)
        if path == "/api/runs":
            return []
        if path == "/api/approvals":
            return []
        if path == "/api/handoffs":
            return []
        if path.endswith("/messages"):
            return {"messages": []}
        if "/console" in path:
            agent_id = path.split("/")[3]
            agent = next(
                (a for a in self.agents if a["agent_id"] == agent_id),
                {"agent_id": agent_id, "display_name": agent_id, "enabled": True, "runtimes": []},
            )
            return {"agent": agent, "runtime_sessions": [], "invocations": []}
        raise AssertionError(f"unexpected GET {path}")

    def post_command(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        self.posts.append((path, dict(payload or {})))
        return {"status": "ACCEPTED", "command_id": command_id or "cmd_test_123"}

    def stream(self, path: str, **kwargs: Any) -> Any:
        return iter(())


def _make_app(client: _FakeClient, language: str = "en") -> ResearchWorkspace:
    return ResearchWorkspace(cast(ResearchClient, client), language=language)


# ------------------------------------------------------------------
# Items 1-4: /shell translation and dispatch
# ------------------------------------------------------------------


def test_shell_coder_translates_to_agent_start() -> None:
    """Item 1: /shell coder must translate to exactly 'agent start coder'."""
    client = _FakeClient()
    app = _make_app(client)
    translated = app._translate_command("/shell coder")
    assert translated == "agent start coder"


def test_shell_dispatches_post_to_resolved_agent_start() -> None:
    """Item 2: the final HTTP call is POST /api/agents/{id}/start."""
    client = _FakeClient()
    app = _make_app(client)

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            translated = app._translate_command("/shell coder")
            assert translated is not None
            app.execute_command(translated)
            await pilot.pause(0.5)

    asyncio.run(scenario())
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/api/agents/agent_coder/start"


def test_shell_payload_contains_no_argv_cwd_actor_or_session() -> None:
    """Item 3: payload must not contain argv/cwd/actor/runtime session ID."""
    client = _FakeClient()
    app = _make_app(client)

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            app.execute_command("agent start coder")
            await pilot.pause(0.5)

    asyncio.run(scenario())
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/api/agents/agent_coder/start"
    forbidden = {"argv", "cwd", "actor", "runtime_session_id", "session_id"}
    for key in forbidden:
        assert key not in payload, f"payload must not contain {key!r}"
    assert payload == {}


def test_shell_missing_agent_fails_with_zero_dispatch() -> None:
    """Item 4a: /shell with no agent -> None (usage shown), zero dispatch."""
    from textual.widgets import Static

    client = _FakeClient()
    app = _make_app(client, language="en")

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            translated = app._translate_command("/shell")
            # /shell with no trailing agent returns None and shows usage.
            assert translated is None
            # Allow the call_from_thread update to land.
            await pilot.pause(0.3)
            # The usage message is captured in the command output.
            hint = app.query_one("#command-output", Static)
            assert "usage: /shell <installed-agent>" in cast(str, hint.content)

    asyncio.run(scenario())
    # No HTTP dispatch occurred for the bare /shell.
    assert client.posts == []


def test_shell_extra_args_fail_with_zero_dispatch() -> None:
    """Item 4b: /shell with extra args -> usage error, zero dispatch."""
    client = _FakeClient()
    app = _make_app(client)

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            translated = app._translate_command("/shell coder extra")
            assert translated is None
            assert client.posts == []

    asyncio.run(scenario())


def test_shell_unknown_agent_fails_with_zero_dispatch() -> None:
    """Item 4c: /shell with unknown agent -> error at resolution, zero dispatch."""
    client = _FakeClient()
    app = _make_app(client)

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            translated = app._translate_command("/shell nobody")
            assert translated is not None  # translation succeeds syntactically
            app.execute_command(translated)
            await pilot.pause(0.5)

    asyncio.run(scenario())
    assert client.posts == []


# ------------------------------------------------------------------
# Items 5-6: TUI layout stability                                    
# ------------------------------------------------------------------


def test_tui_input_box_stays_bottom_on_output_growth() -> None:
    """Item 5: input box Y-coordinate unchanged as command output grows 1->5 lines."""
    client = _FakeClient()
    app = _make_app(client)

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            y_before = app.query_one("#command-input").region.y
            for i in range(5):
                app._capture_command_output(f"line {i + 1}")
                await pilot.pause(0.05)
                y_after = app.query_one("#command-input").region.y
                assert y_after == y_before, (
                    f"input box moved: y_before={y_before} y_after={y_after} "
                    f"after appending line {i + 1}"
                )

    asyncio.run(scenario())


def test_tui_input_box_pinned_after_resize() -> None:
    """Item 6: after terminal resize, input box stays pinned to bottom;
    upper tab content scrolls independently."""
    from textual.widgets import TabbedContent

    client = _FakeClient()
    app = _make_app(client)

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            y_before = app.query_one("#command-input").region.y
            assert y_before == 24 - 3, f"expected y=21, got {y_before}"

            await pilot.resize_terminal(80, 40)
            await pilot.pause(0.2)
            y_after = app.query_one("#command-input").region.y
            assert y_after == 40 - 3, f"expected y=37 after resize, got {y_after}"

            tabs = app.query_one("#workspace-tabs", TabbedContent)
            assert tabs is not None
            assert tabs.region.height > 0

    asyncio.run(scenario())


# ------------------------------------------------------------------
# Item 7: language rendering                                         
# ------------------------------------------------------------------


def test_tui_zh_cn_and_en_render_identical_structure() -> None:
    """Item 7: each language boots its own Textual app and renders its
    localized component text.

    The check inspects real component text, not just the title: the header
    subtitle, every tab label, the command hint, the input placeholder, and
    the rendered pane bodies.
    """
    from textual.widgets import Input, Static, TabbedContent

    expectations: tuple[tuple[str, dict[str, object]], ...] = (
        (
            "en",
            {
                "title": "Research Developer Preview",
                "subtitle": "workspace: auto",
                "tabs": {
                    "collab": "Collab",
                    "agents": "Agents",
                    "tasks": "Tasks",
                    "approvals": "Approvals",
                    "system": "System",
                },
                "hint": "Commands:",
                "placeholder": "Type /help or a research command",
                "tasks_header": "Tasks",
                "system_header": "System",
            },
        ),
        (
            "zh-CN",
            {
                "title": "Research 开发者预览",
                "subtitle": "工作区: 自动",
                "tabs": {
                    "collab": "协作",
                    "agents": "Agent",
                    "tasks": "任务",
                    "approvals": "审批",
                    "system": "系统",
                },
                "hint": "命令：",
                "placeholder": "输入 /help 或 research 命令",
                "tasks_header": "任务",
                "system_header": "系统",
            },
        ),
    )
    for language, expected in expectations:
        client = _FakeClient()
        app = _make_app(client, language=language)

        async def scenario() -> None:
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(0.5)
                # Header title and the snapshot-derived subtitle.
                assert app.title == cast(str, expected["title"])
                assert app.sub_title.startswith(cast(str, expected["subtitle"]))
                # Every tab label is localized component text.
                tabs = app.query_one("#workspace-tabs", TabbedContent)
                for pane_id, label in cast(dict[str, str], expected["tabs"]).items():
                    assert tabs.get_tab(f"tab-{pane_id}").label == label
                # The command hint and input placeholder are localized.
                hint = app.query_one("#command-output", Static)
                assert cast(str, hint.content).startswith(cast(str, expected["hint"]))
                placeholder = app.query_one("#command-input", Input)
                assert placeholder.placeholder == cast(str, expected["placeholder"])
                # The rendered pane bodies carry the localized headers.
                tasks_view = app.query_one("#view-tasks", Static)
                assert cast(str, tasks_view.content).startswith(cast(str, expected["tasks_header"]))
                system_view = app.query_one("#view-system", Static)
                assert cast(str, system_view.content).startswith(cast(str, expected["system_header"]))

        asyncio.run(scenario())


# ------------------------------------------------------------------
# Item 9: daemon zombie / PID reuse protection                       
# ------------------------------------------------------------------


def test_daemon_zombie_not_live_owner() -> None:
    """Item 9: a real zombie must NOT be treated as a live owner.

    Fork a child that exits immediately.  Until the parent reaps it the
    child stays a zombie: its PID keeps a /proc entry in state Z.  The test
    proves that precondition first, so a False result cannot come from the
    PID simply disappearing; a live-process control proves ``is_live`` is
    not broken to always return False.  The child is reaped afterwards so
    no zombie leaks into the test process.
    """
    import os as _os
    import time as _time

    if not Path("/proc/self/stat").is_file():
        pytest.skip("zombie detection requires /proc")
    pid = _os.fork()
    if pid == 0:
        # Child: exit immediately, becoming a zombie until reaped.
        _os._exit(0)
    try:
        _time.sleep(0.05)
        # Precondition: the child is actually a zombie at this PID.  The
        # comm field is parenthesized and may contain spaces, so split at
        # the last ")" before reading the state field.
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        state = stat_line.rsplit(")", 1)[1].split()[0]
        assert state == "Z", f"child must be a zombie, got state {state!r}"
        # Control: the live test process still identifies as live.
        assert is_live(current_identity()) is True
        # An identity claiming the zombie PID must not be live.
        identity = {"pid": pid, "start_ticks": 0, "boot_id": "x"}
        assert is_live(identity) is False, "zombie must not be treated as a live owner"
    finally:
        _os.waitpid(pid, 0)


def test_pid_reuse_protection() -> None:
    """Item 9b: PID reuse must not be mistaken for the same owner.

    If a PID is reused by a different process (different start_ticks),
    the old identity must not be considered live, and the file can be
    taken over.
    """
    with tempfile.TemporaryDirectory() as d:
        state_root = Path(d)
        identity = claim(state_root, "sha256_test")
        assert is_live(identity) is True

        # Simulate PID reuse: same pid, different start_ticks -> not live
        reused = dict(identity)
        reused["start_ticks"] = int(cast(int, identity["start_ticks"])) + 1
        assert is_live(reused) is False, "PID reuse with different start_ticks must not be live"

        # Write the stale identity to disk so claim can take it over
        import json
        state_root.joinpath("daemon.identity.json").write_text(
            json.dumps(reused, sort_keys=True), encoding="utf-8"
        )
        identity2 = claim(state_root, "sha256_test")
        assert identity2["pid"] == os.getpid()
