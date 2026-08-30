"""Projection-only Textual workspace; no authoritative UI state is stored."""

import json
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from researchd.client.transport import ResearchClient


class ResearchWorkspace(App[None]):
    BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]

    def __init__(self, client: ResearchClient) -> None:
        super().__init__()
        self.client = client
        self._agent_views: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            for title, pane_id in (("Collab", "collab"), ("Agents", "agents"), ("Tasks", "tasks"), ("Approvals", "approvals"), ("System", "system")):
                with TabPane(title, id=f"tab-{pane_id}"):
                    with VerticalScroll():
                        yield Static("Loading…", id=f"view-{pane_id}")
            # A console is only a projection/control client. It carries no
            # terminal ownership and may therefore be closed independently.
            try:
                agents = self.client.get("/api/agents")
            except Exception:
                agents = []
            for index, agent in enumerate(agents):
                agent_id = agent["agent_id"]
                pane_id = f"agent-{index}"
                self._agent_views[agent_id] = pane_id
                with TabPane(f"Agent: {agent['display_name']}", id=f"tab-{pane_id}"):
                    with VerticalScroll():
                        yield Static("Loading…", id=f"view-{pane_id}")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_projections()

    def action_refresh(self) -> None:
        self.refresh_projections()

    @work(thread=True, exclusive=True)
    def refresh_projections(self) -> None:
        runs = self.client.get("/api/runs")
        active_run = runs[0]["run_id"] if runs else None
        messages: Any = []
        if active_run is not None:
            messages = self.client.get(f"/api/runs/{active_run}/messages")["messages"]
        payloads = {"collab": messages, "agents": self.client.get("/api/agents"), "tasks": runs, "approvals": self.client.get("/api/approvals"), "system": self.client.health()}
        for agent_id, pane_id in self._agent_views.items():
            path = f"/api/agents/{agent_id}/console"
            if active_run is not None:
                path = f"{path}?run={active_run}"
            payloads[pane_id] = self.client.get(path)
        for name, payload in payloads.items():
            self.call_from_thread(self.query_one(f"#view-{name}", Static).update, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def run_tui(client: ResearchClient) -> None:
    ResearchWorkspace(client).run()


__all__ = ["ResearchWorkspace", "run_tui"]
