"""Local control API and its loopback Web/TUI clients."""

from researchd.api.web import ControlCommandRouter, ControlResourceRouter, serve_local_control
from researchd.api.tui import render_tui

__all__ = ["ControlCommandRouter", "ControlResourceRouter", "serve_local_control", "render_tui"]
