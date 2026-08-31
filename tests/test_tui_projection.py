"""PH06: client-local focus model and TUI projection polish.

Covers the PH06 acceptance criteria:

- deterministic run selection (focused run wins, then newest non-terminal,
  then newest, then none) instead of the implicit oldest-first ``runs[0]``;
- registry-driven Agent pane reconciliation that never stops a RuntimeSession;
- resumable, duplicate-free stream cursors across reconnects;
- unchanged LOCAL_ONLY/SECRET redaction in rendered collaboration;
- projection-only TUI: no mutation ever leaves the authenticated typed
  command routes.
"""

import asyncio
from typing import Any, Iterator, cast

from textual.widgets import ContentSwitcher, Static, TabbedContent

from researchd.client.transport import ResearchClient, StreamFrame
from researchd.client.tui_app import (
    ResearchWorkspace,
    TuiProjectionState,
    _render_approvals,
    _render_collaboration,
    _render_system,
    _render_tasks,
)


def _run(run_id: str, state: str = "ACTIVE") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "state": state,
        "work_orders": [{"work_order_id": f"wo-{run_id}", "state": "PLANNED"}],
    }


def _agent(agent_id: str) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "display_name": agent_id.replace("_", " ").title(),
        "enabled": True,
        "runtimes": [
            {
                "runtime_id": f"rt-{agent_id}",
                "adapter_kind": "INTERNAL",
                "enabled": True,
            }
        ],
    }


class _FakeClient:
    """Structural stand-in for ``ResearchClient``; records every call."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.agents: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.handoffs: list[dict[str, Any]] = []
        self.frames: list[StreamFrame] = []
        self.health_payload: dict[str, Any] = {"state": "READY", "ready": True}
        self.get_calls: list[str] = []
        self.stream_calls: list[tuple[str, int | None]] = []
        self.command_calls: list[tuple[str, dict[str, Any] | None]] = []

    def health(self) -> dict[str, Any]:
        return dict(self.health_payload)

    def get(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        self.get_calls.append(path)
        if path == "/api/runs":
            return list(self.runs)
        if path == "/api/agents":
            return list(self.agents)
        if path == "/api/approvals":
            return list(self.approvals)
        if path.endswith("/messages"):
            return {"messages": list(self.messages)}
        if path == "/api/handoffs":
            return list(self.handoffs)
        if "/console" in path:
            agent_id = path.split("/")[3]
            agent = next(
                (item for item in self.agents if item["agent_id"] == agent_id),
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
        self.command_calls.append((path, payload))
        return {"status": "ACCEPTED"}

    def stream(
        self,
        path: str,
        *,
        after: int | None = None,
        follow: bool = False,
    ) -> Iterator[StreamFrame]:
        self.stream_calls.append((path, after))
        for frame in self.frames:
            if after is None or frame.offset is None or frame.offset > after:
                yield frame


def _snapshot(client: _FakeClient, *, active_run: str | None) -> dict[str, Any]:
    return {
        "runs": list(client.runs),
        "agents": list(client.agents),
        "active_run": active_run,
        "messages": list(client.messages),
        "handoffs": list(client.handoffs),
        "approvals": list(client.approvals),
        "health": client.health(),
        "agent_consoles": {
            str(agent["agent_id"]): {
                "agent": agent,
                "runtime_sessions": [],
                "invocations": [],
            }
            for agent in client.agents
        },
    }


def _has_pane(tabs: TabbedContent, pane_id: str) -> bool:
    switcher = tabs.get_child_by_type(ContentSwitcher)
    return any(child.id == pane_id for child in switcher.children)


# ---------------------------------------------------------------------------
# Run selection (acceptance: "run selection correct")
# ---------------------------------------------------------------------------


def test_select_run_keeps_focus_when_focused_run_still_present() -> None:
    state = TuiProjectionState()
    runs = [_run("run_old", "COMPLETED"), _run("run_new", "ACTIVE")]
    state.focused_run_id = "run_old"
    assert state.select_run(runs) == "run_old"


def test_select_run_prefers_newest_non_terminal_when_focus_is_lost() -> None:
    state = TuiProjectionState()
    runs = [_run("run_a", "COMPLETED"), _run("run_b", "ACTIVE"), _run("run_c", "FAILED")]
    assert state.select_run(runs) == "run_b"
    assert state.focused_run_id == "run_b"


def test_select_run_falls_back_to_newest_run_when_all_terminal() -> None:
    state = TuiProjectionState()
    runs = [_run("run_a", "COMPLETED"), _run("run_b", "CANCELLED")]
    assert state.select_run(runs) == "run_b"


def test_select_run_returns_none_when_no_runs_exist() -> None:
    state = TuiProjectionState(focused_run_id="run_gone")
    assert state.select_run([]) is None
    assert state.focused_run_id is None


def test_select_run_does_not_depend_on_implicit_first_position() -> None:
    # The API orders Runs oldest-first; the old TUI implicitly showed
    # ``runs[0]`` (the oldest). Selection must land on the newest
    # non-terminal run instead.
    state = TuiProjectionState()
    runs = [_run(f"run_{i}", "COMPLETED" if i < 4 else "ACTIVE") for i in range(5)]
    assert state.select_run(runs) == "run_4"


def test_cycle_run_walks_explicit_focus_and_wraps() -> None:
    state = TuiProjectionState()
    runs = [_run("run_a", "COMPLETED"), _run("run_b", "COMPLETED"), _run("run_c", "ACTIVE")]
    # Default focus lands on the newest non-terminal run (run_c); stepping
    # forward wraps to the head of the projection.
    assert state.cycle_run(runs, 1) == "run_a"
    assert state.cycle_run(runs, 1) == "run_b"
    assert state.cycle_run(runs, -1) == "run_a"
    assert state.cycle_run(runs, -1) == "run_c"


def test_cycle_run_clears_focus_when_no_runs_remain() -> None:
    state = TuiProjectionState(focused_run_id="run_gone")
    assert state.cycle_run([], 1) is None
    assert state.focused_run_id is None


# ---------------------------------------------------------------------------
# Stream cursors (acceptance: "reconnect does not duplicate or reorder
# authoritative events")
# ---------------------------------------------------------------------------


def test_accept_event_rejects_duplicates_and_out_of_order_offsets() -> None:
    state = TuiProjectionState()
    assert state.accept_event("run_a", 5)
    assert not state.accept_event("run_a", 5)
    assert not state.accept_event("run_a", 4)
    assert state.accept_event("run_a", 6)
    assert state.offset_for("run_a") == 6


def test_accept_event_ignores_frames_without_server_offset() -> None:
    state = TuiProjectionState()
    assert not state.accept_event("run_a", None)
    assert state.offset_for("run_a") is None


def test_reconnect_resumes_from_last_observed_offset_without_replay() -> None:
    state = TuiProjectionState()
    for offset in (1, 2, 3):
        assert state.accept_event("run_a", offset)
    # A reconnect replays the tail of the server buffer; only genuinely new
    # events are accepted and the resume cursor is the last observed offset.
    assert state.offset_for("run_a") == 3
    assert not state.accept_event("run_a", 2)
    assert not state.accept_event("run_a", 3)
    assert state.accept_event("run_a", 4)
    assert state.last_seen_stream_offset == 4


def test_last_seen_stream_offset_tracks_maximum_across_runs() -> None:
    state = TuiProjectionState()
    assert state.accept_event("run_a", 9)
    assert state.accept_event("run_b", 4)
    assert state.last_seen_stream_offset == 9


# ---------------------------------------------------------------------------
# Semantic rendering (acceptance: redaction policy unchanged)
# ---------------------------------------------------------------------------


def test_redacted_message_body_is_never_rendered() -> None:
    messages = [
        {"purpose": "DIRECTIVE", "sender_actor_id": "human", "body": "secret-value", "body_redacted": True},
        {"purpose": "MESSAGE", "sender_actor_id": "agent", "body": "visible", "body_redacted": False},
    ]
    rendered = _render_collaboration("run_a", messages, [])
    assert "[redacted]" in rendered
    assert "secret-value" not in rendered
    assert "visible" in rendered


def test_tasks_pane_marks_focused_run_with_work_order_cards() -> None:
    runs = [_run("run_a", "COMPLETED"), _run("run_b", "ACTIVE")]
    rendered = _render_tasks(runs, "run_b")
    assert "Focused run: run_b" in rendered
    assert "▶ run_b" in rendered
    assert "wo-run_b: PLANNED" in rendered


def test_approvals_pane_scopes_to_focused_run_and_documents_contract() -> None:
    approvals = [
        {"approval_id": "appr_1", "status": "PENDING", "work_order_id": "wo_b", "run_id": "run_b"},
        {"approval_id": "appr_2", "status": "APPROVED", "work_order_id": "wo_a", "run_id": "run_a"},
    ]
    rendered = _render_approvals(approvals, "run_b")
    assert "appr_1" in rendered
    assert "appr_2" not in rendered
    assert "approval approve <approval-id>" in rendered


def test_system_pane_reports_last_observed_stream_offset() -> None:
    rendered = _render_system({"state": "READY", "ready": True}, 42)
    assert "Last observed event offset: 42" in rendered


# ---------------------------------------------------------------------------
# App-level behaviour (acceptance: registry changes update Agent panes;
# closing a pane never stops a RuntimeSession; all mutations stay on the
# authenticated typed command routes)
# ---------------------------------------------------------------------------


def test_registry_changes_reconcile_agent_panes_without_stopping_runtimes() -> None:
    client = _FakeClient()
    client.agents = [_agent("agent_a")]

    async def scenario() -> None:
        app = ResearchWorkspace(cast(ResearchClient, client))
        async with app.run_test() as pilot:
            tabs = app.query_one("#workspace-tabs", TabbedContent)
            app._apply_snapshot(_snapshot(client, active_run=None))
            await pilot.pause(0.3)
            assert _has_pane(tabs, "tab-agent-agent_a")
            base_count = tabs.tab_count

            client.agents = [_agent("agent_a"), _agent("agent_b")]
            app._apply_snapshot(_snapshot(client, active_run=None))
            await pilot.pause(0.3)
            assert _has_pane(tabs, "tab-agent-agent_b")
            assert tabs.tab_count == base_count + 1

            # Removing an Agent from the registry drops its projection pane…
            client.agents = [_agent("agent_b")]
            app._apply_snapshot(_snapshot(client, active_run=None))
            await pilot.pause(0.3)
            assert not _has_pane(tabs, "tab-agent-agent_a")
            assert tabs.tab_count == base_count
            # …without ever issuing a stop/terminate command to the daemon.
            assert client.command_calls == []

    asyncio.run(scenario())


def test_focused_run_survives_refresh_even_when_newer_run_exists() -> None:
    client = _FakeClient()
    client.runs = [_run("run_old", "COMPLETED"), _run("run_new", "ACTIVE")]

    async def scenario() -> None:
        app = ResearchWorkspace(cast(ResearchClient, client))
        async with app.run_test() as pilot:
            await pilot.pause(0.6)  # on-mount refresh: focus lands on run_new
            assert app.state.focused_run_id == "run_new"
            app.state.focused_run_id = "run_old"
            app.refresh_projections()
            await pilot.pause(0.6)
            tasks = app.query_one("#view-tasks", Static)
            assert "Focused run: run_old" in str(tasks.content)

    asyncio.run(scenario())


def test_workspace_refresh_and_stream_issue_no_mutations() -> None:
    client = _FakeClient()
    client.runs = [_run("run_a", "ACTIVE")]
    client.frames = [
        StreamFrame(1, {"type": "EVENT"}),
        StreamFrame(2, {"type": "EVENT"}),
    ]

    async def scenario() -> None:
        app = ResearchWorkspace(cast(ResearchClient, client))
        async with app.run_test() as pilot:
            await pilot.pause(1.5)  # on-mount refresh + at least one stream poll

        # The TUI is a projection-only client: every mutation must travel the
        # authenticated typed command routes (shell/HTTP), never the TUI.
        assert client.command_calls == []

    asyncio.run(scenario())


def test_stream_poll_resumes_from_per_run_cursor() -> None:
    client = _FakeClient()
    client.runs = [_run("run_a", "ACTIVE")]
    client.frames = [StreamFrame(1, {"type": "EVENT"})]

    async def scenario() -> None:
        app = ResearchWorkspace(cast(ResearchClient, client))
        async with app.run_test() as pilot:
            await pilot.pause(1.2)
            cursors = [after for _, after in client.stream_calls]
            # The first poll has no cursor yet; after the frame is accepted
            # the next poll must resume strictly after offset 1.
            assert cursors[0] is None
            assert any(cursor == 1 for cursor in cursors[1:])

    asyncio.run(scenario())
