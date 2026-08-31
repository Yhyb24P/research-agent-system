"""Daily ``research`` client: a thin loopback facade over the researchd daemon.

The client never reads the controller SQLite database directly; every
operation crosses the authenticated HTTP transport against the local
daemon. No TUI framework is a base dependency.
"""
