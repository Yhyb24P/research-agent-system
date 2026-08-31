"""Deployment-specific AgentDefinition documents for the PH07 E2E.

Pure standard library: the launch-profile digest is computed with the same
canonical rule the trusted install service enforces
(``sha256`` of ``{"launch_mode", "configuration"}`` sorted compact JSON),
so the documents validate without importing researchd.  Every launch spec
is an absolute, hash-bound argv — the daemon-owned launch catalog, never a
client-supplied command line.
"""

import hashlib
import json
from pathlib import Path
from typing import Any


def launch_digest(configuration: dict[str, Any]) -> str:
    payload = json.dumps(
        {"launch_mode": "PROCESS", "configuration": configuration},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _process_definition(
    *,
    agent_id: str,
    display_name: str,
    role: str,
    runtime_id: str,
    port: int,
    argv: list[str],
    cwd: str,
) -> dict[str, Any]:
    configuration = {"launch_spec": {"argv": argv, "cwd": cwd}}
    return {
        "definition_version": 1,
        "profile": {
            "agent_id": agent_id,
            "display_name": display_name,
            "roles": [role],
            "skills": [],
            "trust_zone": "LOCAL_PRIVATE",
            "constraints": [],
            "labels": {},
            "max_parallel_delegations": 1,
            "enabled": True,
            "profile_version": 1,
        },
        "runtimes": [{
            "runtime_id": runtime_id,
            "agent_id": agent_id,
            "adapter_kind": "PROCESS",
            "runtime_name": f"{display_name} process service",
            "endpoint_ref": f"http://127.0.0.1:{port}/invoke",
            "framework": "research-agent-json-v1",
            "model_provider": None,
            "model_name": None,
            "protocols": ["research-agent-json-v1"],
            "metadata": {"health_endpoint": f"http://127.0.0.1:{port}/health"},
        }],
        "launch_profiles": [{
            "runtime_id": runtime_id,
            "launch_mode": "PROCESS",
            "configuration": configuration,
            "spec_sha256": launch_digest(configuration),
            "enabled": True,
            "version": 1,
        }],
    }


def build_definitions(
    *,
    python: Path,
    harness_dir: Path,
    coder_binary: Path,
    cwd: Path,
    planner_port: int,
    coder_port: int,
    reviewer_port: int,
) -> dict[str, dict[str, Any]]:
    """Three installable AgentDefinitions: planner, coder, reviewer.

    The coder uses the installed ``research-coder-agent`` console script
    (the shipped pilot); planner and reviewer use the reference harness
    Agents, launched through the venv interpreter.
    """
    return {
        "planner": _process_definition(
            agent_id="agent_ph07_planner",
            display_name="PH07 Planner",
            role="planner",
            runtime_id="runtime_ph07_planner_process",
            port=planner_port,
            argv=[
                str(python),
                str(harness_dir / "planner_agent.py"),
                "--host", "127.0.0.1", "--port", str(planner_port),
            ],
            cwd=str(cwd),
        ),
        "coder": _process_definition(
            agent_id="agent_ph07_coder",
            display_name="PH07 Coder",
            role="executor",
            runtime_id="runtime_ph07_coder_process",
            port=coder_port,
            argv=[
                str(coder_binary),
                "--host", "127.0.0.1", "--port", str(coder_port),
            ],
            cwd=str(cwd),
        ),
        "reviewer": _process_definition(
            agent_id="agent_ph07_reviewer",
            display_name="PH07 Reviewer",
            role="reviewer",
            runtime_id="runtime_ph07_reviewer_process",
            port=reviewer_port,
            argv=[
                str(python),
                str(harness_dir / "reviewer_agent.py"),
                "--host", "127.0.0.1", "--port", str(reviewer_port),
            ],
            cwd=str(cwd),
        ),
    }


__all__ = ["build_definitions", "launch_digest"]
