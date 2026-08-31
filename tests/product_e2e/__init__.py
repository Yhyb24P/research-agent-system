"""PH07 product-candidate E2E harness: reference managed Agents and the
installed-artifact acceptance driver.

The reference Agents speak the managed turn protocol over loopback HTTP,
exactly like the shipped ``research-coder-agent`` pilot.  The driver
(``run_e2e.py``) is the acceptance client: it only uses the installed
console scripts and the authenticated HTTP surface.  It never imports
researchd services, opens SQLite, calls the orchestrator directly, writes
grant rows, or launches an Agent with client-supplied argv.
"""
