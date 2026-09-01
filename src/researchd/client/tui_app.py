"""Projection-only Textual workspace; no authoritative UI state is stored.

The workspace deliberately keeps focus and stream cursors in the client. It
does not infer an active Run from API ordering: a focused Run wins, otherwise
the newest non-terminal Run is selected, then the newest Run.
"""

from __future__ import annotations

import time
import shlex
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.worker import get_current_worker
from textual.widgets import Footer, Header, Input, Static, TabbedContent, TabPane

from researchd.client.shell import ShellSession
from researchd.client.transport import ResearchClient, TransportError


_TERMINAL_RUN_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


@dataclass
class TuiProjectionState:
    """Ephemeral focus and resumable cursor state for one TUI process."""

    focused_run_id: str | None = None
    focused_agent_id: str | None = None
    last_seen_stream_offset: int = 0
    _run_offsets: dict[str, int] = field(default_factory=dict)

    def select_run(self, runs: list[dict[str, Any]]) -> str | None:
        """Choose focus without relying on the oldest-first API ordering."""
        available = {str(run["run_id"]) for run in runs}
        if self.focused_run_id in available:
            return self.focused_run_id
        newest_non_terminal = [
            str(run["run_id"])
            for run in runs
            if run.get("state") not in _TERMINAL_RUN_STATES
        ]
        candidates = newest_non_terminal or [str(run["run_id"]) for run in runs]
        self.focused_run_id = candidates[-1] if candidates else None
        return self.focused_run_id

    def cycle_run(self, runs: list[dict[str, Any]], direction: int) -> str | None:
        """Move explicit focus through the current durable Run projection."""
        identifiers = [str(run["run_id"]) for run in runs]
        if not identifiers:
            self.focused_run_id = None
            return None
        current = self.select_run(runs)
        assert current is not None
        self.focused_run_id = identifiers[(identifiers.index(current) + direction) % len(identifiers)]
        return self.focused_run_id

    def accept_event(self, run_id: str, offset: int | None) -> bool:
        """Accept each server-assigned offset once, including after reconnect."""
        if offset is None:
            return False
        previous = self._run_offsets.get(run_id, 0)
        if offset <= previous:
            return False
        self._run_offsets[run_id] = offset
        self.last_seen_stream_offset = max(self.last_seen_stream_offset, offset)
        return True

    def offset_for(self, run_id: str) -> int | None:
        return self._run_offsets.get(run_id)


class ResearchWorkspace(App[None]):
    """A disposable projection/control client for the local daemon."""

    TITLE = "Research Developer Preview"
    SUB_TITLE = "Agent collaboration workspace"

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("[", "previous_run", "Previous run"),
        ("]", "next_run", "Next run"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        client: ResearchClient,
        *,
        config_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.config_path = config_path
        self.state = TuiProjectionState()
        self._runs: list[dict[str, Any]] = []
        self._agent_views: dict[str, str] = {}
        self._command_lines: list[str] = []
        self._shell = ShellSession(client, print_fn=self._capture_command_output)

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="workspace-tabs"):
            for title, pane_id in (
                ("Collab", "collab"),
                ("Agents", "agents"),
                ("Tasks", "tasks"),
                ("Approvals", "approvals"),
                ("System", "system"),
            ):
                with TabPane(title, id=f"tab-{pane_id}"):
                    with VerticalScroll():
                        yield Static("Loading…", id=f"view-{pane_id}")
        yield Static(
            "Commands: /task <objective> · /msg @agent <text> · /attach <file> · /approve <id> · /help",
            id="command-output",
        )
        yield Input(placeholder="Type /help or a research command", id="command-input")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_projections()
        self.follow_selected_run()

    def action_refresh(self) -> None:
        self.refresh_projections()

    def action_previous_run(self) -> None:
        self.state.cycle_run(self._runs, -1)
        self.refresh_projections()

    def action_next_run(self) -> None:
        self.state.cycle_run(self._runs, 1)
        self.refresh_projections()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.clear()
        if not line:
            return
        if line == "/help":
            self._capture_command_output(
                "/task <objective> | /msg @agent <text> | /attach <file> | /approve <id> | "
                "/agent use <id> | /workspace use <id> | /run list"
            )
            return
        if line.startswith(("/agent add ", "/agent refresh ", "/agent remove ")):
            self.manage_agent(line)
            return
        translated = self._translate_command(line)
        if translated is None:
            return
        if translated == "quit":
            self.exit()
            return
        if translated.startswith("events watch"):
            self._capture_command_output("Use the live projections instead of events watch in the TUI.")
            return
        self._capture_command_output(f"> {line}")
        self.execute_command(translated)

    def _translate_command(self, line: str) -> str | None:
        if not line.startswith("/"):
            return line
        body = line[1:].strip()
        if body.startswith("task "):
            return f"task create {shlex.quote(body[5:].strip())}"
        if body.startswith("approve "):
            return f"approval approve {shlex.quote(body[8:].strip())}"
        if body.startswith("msg "):
            run_id = self.state.focused_run_id
            if run_id is None:
                self._capture_command_output("No focused run; create a task first.")
                return None
            try:
                tokens = shlex.split(body)
            except ValueError as error:
                self._capture_command_output(f"parse error: {error}")
                return None
            if len(tokens) < 3 or not tokens[1].startswith("@"):
                self._capture_command_output("usage: /msg @agent <text>")
                return None
            recipient = tokens[1][1:]
            message = " ".join(tokens[2:])
            return (
                f"msg {shlex.quote(run_id)} {shlex.quote(message)} "
                f"--to {shlex.quote(recipient)}"
            )
        return body

    @work(thread=True, group="command")
    def execute_command(self, line: str) -> None:
        keep_open = self._shell.execute(line)
        if not keep_open:
            self.call_from_thread(self.exit)
            return
        self.call_from_thread(self.refresh_projections)

    @work(thread=True, exclusive=True, group="agent-management")
    def manage_agent(self, line: str) -> None:
        if self.config_path is None:
            self._capture_command_output("Agent catalog management needs a trusted config path.")
            return
        try:
            tokens = shlex.split(line[1:])
        except ValueError as error:
            self._capture_command_output(f"parse error: {error}")
            return
        if len(tokens) < 3 or tokens[0] != "agent":
            self._capture_command_output("usage: /agent add|refresh|remove <role>")
            return
        action, role = tokens[1], tokens[2]
        if role not in {"planner", "coder", "reviewer"}:
            self._capture_command_output("Agent role must be planner, coder, or reviewer.")
            return
        from typing import cast

        from researchd.client.agent_management import (
            PreviewRole,
            add_aweswitch_agent,
            default_profile_ref,
            remove_agent,
        )

        preview_role = cast(PreviewRole, role)
        output = self._capture_command_output
        if action == "remove":
            if len(tokens) != 3:
                output("usage: /agent remove <role>")
                return
            remove_agent(self.config_path, preview_role, print_fn=output)
        else:
            profile = None
            if len(tokens) == 5 and tokens[3] == "--profile":
                profile = tokens[4]
            elif len(tokens) != 3:
                output(f"usage: /agent {action} <role> [--profile aweswitch:<profile>]")
                return
            selected = profile or default_profile_ref()
            if selected is None:
                output("Select a supported profile with --profile aweswitch:<profile>.")
                return
            add_aweswitch_agent(
                self.config_path, preview_role, selected, print_fn=output,
            )
        self.call_from_thread(self.refresh_projections)

    def _capture_command_output(self, message: str) -> None:
        def update() -> None:
            self._command_lines.append(message)
            del self._command_lines[:-5]
            self.query_one("#command-output", Static).update("\n".join(self._command_lines))

        try:
            self.call_from_thread(update)
        except RuntimeError:
            update()

    @work(thread=True, exclusive=True, group="projection")
    def refresh_projections(self) -> None:
        """Fetch one coherent presentation snapshot through the control API."""
        try:
            runs = self.client.get("/api/runs")
            agents = self.client.get("/api/agents")
            active_run = self.state.select_run(runs)
            messages: list[dict[str, Any]] = []
            handoffs: list[dict[str, Any]] = []
            if active_run is not None:
                messages = self.client.get(f"/api/runs/{active_run}/messages")["messages"]
                handoffs = self.client.get("/api/handoffs", params={"run": active_run})
            snapshot: dict[str, Any] = {
                "runs": runs,
                "agents": agents,
                "active_run": active_run,
                "messages": messages,
                "handoffs": handoffs,
                "approvals": self.client.get("/api/approvals"),
                "health": self.client.health(),
            }
            snapshot["agent_consoles"] = {
                str(agent["agent_id"]): self.client.get(
                    f"/api/agents/{agent['agent_id']}/console",
                    params={"run": active_run} if active_run else None,
                )
                for agent in agents
            }
        except TransportError as error:
            self.call_from_thread(self._show_error, str(error))
            return
        self.call_from_thread(self._apply_snapshot, snapshot)

    @work(thread=True, exclusive=True, group="run-stream")
    def follow_selected_run(self) -> None:
        """Poll the existing SSE replay endpoint with its durable cursor."""
        worker = get_current_worker()
        while not worker.is_cancelled:
            run_id = self.state.focused_run_id
            if run_id is None:
                time.sleep(0.25)
                continue
            changed = False
            try:
                for frame in self.client.stream(
                    f"/api/runs/{run_id}/stream",
                    after=self.state.offset_for(run_id),
                ):
                    if worker.is_cancelled:
                        return
                    changed = self.state.accept_event(run_id, frame.offset) or changed
            except TransportError as error:
                self.call_from_thread(self._show_error, f"event refresh failed: {error}")
            if changed:
                self.call_from_thread(self.refresh_projections)
            time.sleep(0.5)

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._runs = list(snapshot["runs"])
        self._shell.state.current_run = snapshot["active_run"]
        self.sub_title = (
            f"workspace: {self._shell.state.current_workspace or 'auto'} · "
            f"run: {snapshot['active_run'] or 'none'} · "
            f"daemon: {snapshot['health'].get('state', 'unknown')}"
        )
        self._reconcile_agent_panes(list(snapshot["agents"]))
        active_run = snapshot["active_run"]
        focused = active_run or "none"
        payloads: dict[str, str] = {
            "collab": _render_collaboration(focused, snapshot["messages"], snapshot["handoffs"]),
            "agents": _render_agents(snapshot["agents"]),
            "tasks": _render_tasks(self._runs, active_run),
            "approvals": _render_approvals(snapshot["approvals"], active_run),
            "system": _render_system(snapshot["health"], self.state.last_seen_stream_offset),
        }
        for agent_id, console in snapshot["agent_consoles"].items():
            pane_id = self._agent_views.get(agent_id)
            if pane_id is not None:
                payloads[pane_id] = _render_agent_console(console, active_run)
        # Dynamic panes mount after ``add_pane`` completes. Deferring rendering
        # one refresh cycle means a newly registered Agent receives this same
        # snapshot instead of waiting for an unrelated later event.
        self.call_after_refresh(self._render_payloads, payloads)

    def _render_payloads(self, payloads: dict[str, str]) -> None:
        for pane_id, content in payloads.items():
            try:
                self.query_one(f"#view-{pane_id}", Static).update(content)
            except Exception:
                # A pane removed during reconciliation is no longer a target.
                continue

    def _reconcile_agent_panes(self, agents: list[dict[str, Any]]) -> None:
        """Add/remove only client projection panes as the Registry changes."""
        tabs = self.query_one("#workspace-tabs", TabbedContent)
        desired = {str(agent["agent_id"]): agent for agent in agents}
        for agent_id, pane_id in tuple(self._agent_views.items()):
            if agent_id not in desired:
                tabs.remove_pane(f"tab-{pane_id}")
                del self._agent_views[agent_id]
        for agent_id, agent in desired.items():
            if agent_id in self._agent_views:
                continue
            pane_id = f"agent-{agent_id}"
            self._agent_views[agent_id] = pane_id
            tabs.add_pane(TabPane(
                f"Agent: {agent['display_name']}",
                VerticalScroll(Static("Loading…", id=f"view-{pane_id}")),
                id=f"tab-{pane_id}",
            ))

    def _show_error(self, message: str) -> None:
        self.query_one("#view-system", Static).update(f"Control-plane projection error\n{message}")


def _render_tasks(runs: list[dict[str, Any]], active_run: str | None) -> str:
    if not runs:
        return "Tasks\nNo durable ResearchRuns. Use /task <objective> to begin."
    lines = ["Tasks", f"Focused run: {active_run or 'none'}"]
    for run in runs:
        marker = "▶" if run["run_id"] == active_run else " "
        orders = ", ".join(f"{item['work_order_id']}: {item['state']}" for item in run["work_orders"]) or "no work orders"
        lines.extend((f"{marker} {run['run_id']}  {run['state']}", f"  {orders}"))
    return "\n".join(lines)


def _render_collaboration(
    active_run: str,
    messages: list[dict[str, Any]],
    handoffs: list[dict[str, Any]],
) -> str:
    lines = ["Collaboration", f"Focused run: {active_run}", "Messages:"]
    if active_run != "none":
        lines.extend((
            "Detached views:",
            f"  research console collab --run {active_run}",
            f"  research console system",
        ))
    lines.extend(
        f"- {item['purpose']} from {item['sender_actor_id']}: {item['body'] if not item['body_redacted'] else '[redacted]'}"
        for item in messages
    )
    if not messages:
        lines.append("- none")
    lines.append("Handoffs:")
    lines.extend(
        f"- {item['proposal_id']}  {item['status']}  {item['requested_mode']}"
        for item in handoffs
    )
    if not handoffs:
        lines.append("- none")
    return "\n".join(lines)


def _render_agents(agents: list[dict[str, Any]]) -> str:
    lines = ["Agents"]
    for agent in agents:
        runtimes = ", ".join(
            f"{runtime['runtime_id']} ({runtime['adapter_kind']}, {'enabled' if runtime['enabled'] else 'disabled'})"
            for runtime in agent["runtimes"]
        ) or "no runtimes"
        lines.extend((
            f"- {agent['display_name']} [{agent['agent_id']}] {'enabled' if agent['enabled'] else 'disabled'}",
            f"  {runtimes}",
        ))
    return (
        "\n".join(lines)
        if len(lines) > 1
        else "Agents\nNo installed Agents. Use /agent add coder to begin."
    )


def _render_agent_console(console: dict[str, Any], active_run: str | None) -> str:
    agent = console["agent"]
    lines = [f"Agent console: {agent['display_name']}", f"Focused run: {active_run or 'all'}"]
    command = f"research console agent {agent['agent_id']}"
    if active_run is not None:
        command += f" --run {active_run}"
    lines.append(f"Detached: {command}")
    lines.append("Runtime sessions:")
    lines.extend(
        f"- {item['runtime_id']}: {item['supervisor_state']}"
        for item in console["runtime_sessions"]
    )
    lines.append("Invocations:")
    lines.extend(
        f"- {item['invocation_id']}: {item['purpose']} / {item['status']}"
        for item in console["invocations"]
    )
    return "\n".join(lines)


def _render_approvals(approvals: list[dict[str, Any]], active_run: str | None) -> str:
    scoped = [item for item in approvals if active_run is None or item["run_id"] == active_run]
    lines = ["Approvals", "Use: approval approve <approval-id>"]
    lines.extend(
        f"- {item['approval_id']}  {item['status']}  WorkOrder: {item['work_order_id']}"
        for item in scoped
    )
    return "\n".join(lines) if scoped else "\n".join(lines + ["- none"])


def _render_system(health: dict[str, Any], last_offset: int) -> str:
    return "\n".join((
        "System",
        f"Daemon: {health.get('state', 'unknown')}",
        f"Ready: {health.get('ready', False)}",
        f"Last observed event offset: {last_offset}",
    ))


def run_tui(client: ResearchClient, *, config_path: Path | None = None) -> None:
    ResearchWorkspace(client, config_path=config_path).run()


__all__ = ["ResearchWorkspace", "TuiProjectionState", "run_tui"]
