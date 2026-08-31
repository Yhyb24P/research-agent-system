"""PH07 product-candidate E2E driver (the acceptance client).

Drives a clean installed artifact through the full product workflow using
only the installed console scripts and the authenticated HTTP surface:

    bootstrap config/state -> workspace create -> trusted admin install
    AgentDefinitions -> agent start planner/coder/reviewer -> task create
    -> daemon driver advances the run -> HUMAN approval by approval_id
    (when required) -> executor artifacts/claims -> SYSTEM verification
    -> review -> terminal state -> daemon restart -> projection coherence.

Restrictions honored (PH07): no researchd imports, no SQLite, no manual
``orchestrator.run()` calls, no grant-row writes, no client-supplied Agent
argv.  The driver is standard library only.

Exit code 0 when the run reaches the expected terminal state (COMPLETED)
and every coherence check passes; 1 otherwise, with a JSON evidence
document printed to stdout.
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from definitions import build_definitions  # type: ignore[import-not-found]  # noqa: E402

EXPECTED_TERMINAL_STATE = "COMPLETED"
TERMINAL_STATES = {"COMPLETED", "FAILED", "CANCELLED"}
REQUIRED_EVENT_TYPES = {
    "RUN_CREATED",
    "PLAN_CREATED",
    "WORK_ORDER_CREATED",
    "WORK_ORDER_DISPATCHED",
    "EXECUTION_STARTED",
    "VERIFICATION_COMPLETED",
    "REVIEW_DECISION_RECORDED",
    "WORK_ORDER_ACCEPTED",
    "RUN_COMPLETED",
}


class Evidence:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {"phases": {}}

    def phase(self, name: str) -> dict[str, Any]:
        phase: dict[str, Any] | None = self.data["phases"].get(name)
        if phase is None:
            phase = {}
            self.data["phases"][name] = phase
        return phase

    def finish(self, result: str, **extra: Any) -> None:
        self.data["result"] = result
        self.data.update(extra)
        print(json.dumps(self.data, indent=2, sort_keys=True, default=str))


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"raw": body[:512].decode(errors="replace")}
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, {}


def run_console(
    argv: list[str], *, timeout: float = 120.0,
) -> tuple[int, str, str]:
    completed = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout,
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def poll(
    description: str,
    probe: Any,
    *,
    deadline_seconds: float,
    interval: float = 0.5,
) -> Any:
    deadline = time.monotonic() + deadline_seconds
    last: Any = None
    while True:
        last = probe()
        if last is not None:
            return last
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{description}: {last!r}")
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(prog="ph07-e2e")
    parser.add_argument("--root", type=Path, required=True, help="clean state root for this E2E")
    parser.add_argument("--commit", default="", help="candidate commit under test")
    parser.add_argument("--daemon-port", type=int, default=18788)
    parser.add_argument("--planner-port", type=int, default=19011)
    parser.add_argument("--coder-port", type=int, default=19013)
    parser.add_argument("--reviewer-port", type=int, default=19012)
    parser.add_argument("--run-timeout", type=float, default=180.0)
    args = parser.parse_args()

    evidence = Evidence()
    harness_dir = Path(__file__).resolve().parent
    venv_bin = Path(sys.executable).parent
    python = Path(sys.executable)
    coder_binary = venv_bin / "research-coder-agent"

    # ------------------------------------------------------------------
    # Phase 0: preflight (host prerequisites and installed surface)
    # ------------------------------------------------------------------
    preflight: dict[str, Any] = {
        "python": str(python),
        "workspace_host_dir_present": Path("/workspace").is_dir(),
        "bwrap_available": shutil.which("bwrap") is not None,
    }
    for script in ("researchd", "research", "researchctl", "research-coder-agent"):
        preflight[f"console_{script}"] = (venv_bin / script).is_file()
    evidence.phase("preflight").update(preflight)
    if not all(preflight[f"console_{name}"] for name in
               ("researchd", "research", "researchctl", "research-coder-agent")):
        evidence.finish("FAIL", failure="installed console scripts missing")
        return 1

    root = args.root.resolve()
    if root.exists():
        raise SystemExit(f"refusing to reuse a non-clean E2E root: {root}")
    state_root = root / "state"
    config_path = root / "config.json"
    config_path.parent.mkdir(parents=True)
    config = {
        "database": str(state_root / "researchd.db"),
        "artifact_root": str(root / "artifacts"),
        "state_root": str(state_root),
        "host": "127.0.0.1",
        "port": args.daemon_port,
        # PH07 exercises an explicitly operator-authorized local execution
        # capability. Empty configuration remains fail-closed in production.
        "workspace_capabilities": ["sandbox.shell"],
        "user_capabilities": ["sandbox.shell"],
    }
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    base_url = f"http://127.0.0.1:{args.daemon_port}"

    # ------------------------------------------------------------------
    # Phase 1: bootstrap via the daily client (delegates to researchd init)
    # ------------------------------------------------------------------
    code, out, err = run_console([str(venv_bin / "research"), "--config", str(config_path), "init"])
    evidence.phase("bootstrap").update({
        "command": "research --config <cfg> init",
        "exit_code": code,
        "stdout": out[-400:],
        "stderr": err[-400:],
    })
    if code != 0:
        evidence.finish("FAIL", failure="bootstrap init failed")
        return 1

    # ------------------------------------------------------------------
    # Phase 2: trusted admin install of the test AgentDefinitions
    # ------------------------------------------------------------------
    definitions = build_definitions(
        python=python,
        harness_dir=harness_dir,
        coder_binary=coder_binary,
        cwd=harness_dir,
        planner_port=args.planner_port,
        coder_port=args.coder_port,
        reviewer_port=args.reviewer_port,
    )
    install_receipts: dict[str, Any] = {}
    for role, definition in definitions.items():
        definition_path = root / f"{role}_definition.json"
        definition_path.write_text(json.dumps(definition, indent=2, sort_keys=True), encoding="utf-8")
        code, out, err = run_console([
            str(venv_bin / "researchd"), "--config", str(config_path),
            "install-agent", str(definition_path),
        ])
        receipt = json.loads(out) if out and code == 0 else {"exit_code": code, "stderr": err[-400:]}
        install_receipts[role] = receipt
        if code != 0:
            evidence.phase("install").update(install_receipts)
            evidence.finish("FAIL", failure=f"install-agent failed for {role}")
            return 1
    evidence.phase("install").update(install_receipts)

    # ------------------------------------------------------------------
    # Phase 3: daemon serve + managed agent starts
    # ------------------------------------------------------------------
    token = (state_root / "control.token").read_text(encoding="utf-8").strip()
    log_path = state_root / "daemon.log"
    daemon_proc: subprocess.Popen[bytes] | None = None
    daemon_proc = subprocess.Popen(
        [str(venv_bin / "researchd"), "--config", str(config_path), "serve"],
        stdout=log_path.open("ab"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    evidence.phase("daemon").update({"pid": daemon_proc.pid})

    def ready() -> dict[str, Any] | None:
        status, health = http_json("GET", f"{base_url}/api/health")
        if status == 200 and isinstance(health, dict) and health.get("ready") is True:
            return health
        return None

    try:
        health = poll("daemon READY", ready, deadline_seconds=30.0)
        evidence.phase("daemon").update({"ready": health})

        starts: dict[str, Any] = {}
        for role in ("planner", "coder", "reviewer"):
            agent_id = f"agent_ph07_{role}"
            status, envelope = http_json(
                "POST", f"{base_url}/api/agents/{agent_id}/start",
                {"command_id": f"cmd_ph07_start_{role}"}, token=token,
            )
            starts[role] = {"http_status": status, "envelope": envelope}
            if status != 202 or envelope.get("status") != "ACCEPTED":
                evidence.phase("agent_starts").update(starts)
                evidence.finish("FAIL", failure=f"agent start rejected for {role}")
                return 1
        evidence.phase("agent_starts").update(starts)

        def sessions_healthy() -> list[dict[str, Any]] | None:
            status, rows = http_json("GET", f"{base_url}/api/runtime-sessions", token=token)
            if status != 200 or not isinstance(rows, list):
                return None
            healthy = [row for row in rows if row.get("supervisor_state") == "HEALTHY"]
            return healthy if len(healthy) == 3 else None

        sessions = poll("3 HEALTHY runtime sessions", sessions_healthy, deadline_seconds=30.0)
        evidence.phase("runtime_sessions").update({"initial": sessions})

        # Operator readiness: every reference service answers health before
        # the task is created (no turn may hit an unbound port).
        for role, port in (("planner", args.planner_port),
                           ("coder", args.coder_port),
                           ("reviewer", args.reviewer_port)):
            status, _ = http_json("GET", f"http://127.0.0.1:{port}/health")
            if status != 200:
                evidence.finish("FAIL", failure=f"{role} service not healthy on port {port}")
                return 1

        # --------------------------------------------------------------
        # Phase 4: product workflow through authenticated HTTP
        # --------------------------------------------------------------
        status, workspace = http_json(
            "POST", f"{base_url}/api/workspaces",
            {"command_id": "cmd_ph07_workspace", "workspace_id": "ws_ph07", "name": "PH07 product E2E"},
            token=token,
        )
        evidence.phase("workspace").update({"http_status": status, "envelope": workspace})
        if status != 202:
            evidence.finish("FAIL", failure="workspace create rejected")
            return 1

        status, task = http_json(
            "POST", f"{base_url}/api/runs",
            {"command_id": "cmd_ph07_task", "workspace_id": "ws_ph07",
             "objective": "PH07 product E2E: planner, coder, verifier, reviewer",
             "run_id": "run_ph07_e2e"},
            token=token,
        )
        evidence.phase("task_create").update({"http_status": status, "envelope": task})
        if status != 202:
            evidence.finish("FAIL", failure="task create rejected")
            return 1

        approvals_used: list[str] = []

        def run_document() -> dict[str, Any]:
            status, document = http_json("GET", f"{base_url}/api/runs/run_ph07_e2e", token=token)
            return document if status == 200 else {"state": "UNREACHABLE"}

        final_doc: dict[str, Any] = {}
        deadline = time.monotonic() + args.run_timeout
        while True:
            document = run_document()
            state = document.get("state")
            if state in TERMINAL_STATES:
                final_doc = document
                break
            if document.get("pending_approval_ids"):
                approval_id = document["pending_approval_ids"][0]
                status, envelope = http_json(
                    "POST", f"{base_url}/api/approvals/{approval_id}/approve",
                    {"command_id": f"cmd_ph07_approve_{len(approvals_used)}"},
                    token=token,
                )
                approvals_used.append(approval_id)
                evidence.phase("approvals").setdefault("used", []).append({
                    "approval_id": approval_id, "http_status": status, "envelope": envelope,
                })
                if status != 202:
                    evidence.finish("FAIL", failure=f"approval {approval_id} rejected")
                    return 1
            if time.monotonic() >= deadline:
                evidence.finish("FAIL", failure="run did not reach a terminal state",
                                last_state=document.get("state"))
                return 1
            time.sleep(0.5)
        evidence.phase("run_terminal").update(final_doc)

        # --------------------------------------------------------------
        # Phase 5: projection coherence before restart
        # --------------------------------------------------------------
        status, timeline = http_json("GET", f"{base_url}/api/timeline/run_ph07_e2e", token=token)
        event_types = {item.get("event_type") for item in timeline if item.get("kind") == "event"}
        artifact_items = [item for item in timeline if item.get("kind") == "artifact"]
        claim_items = [item for item in timeline if item.get("kind") == "claim"]
        evidence.phase("timeline_pre_restart").update({
            "event_count": len([item for item in timeline if item.get("kind") == "event"]),
            "event_types": sorted(event_types),
            "artifact_count": len(artifact_items),
            "artifacts": artifact_items,
            "claim_count": len(claim_items),
            "claims": claim_items,
        })

        status, artifacts = http_json("GET", f"{base_url}/api/artifacts?run=run_ph07_e2e", token=token)
        evidence.phase("artifacts").update({"http_status": status, "rows": artifacts})

        # --------------------------------------------------------------
        # Phase 6: daemon restart and coherence after restart
        # --------------------------------------------------------------
        code, out, err = run_console([
            str(venv_bin / "research"), "--config", str(config_path), "daemon", "stop",
        ], timeout=60.0)
        evidence.phase("daemon_restart").update({"stop_exit_code": code, "stop_stderr": err[-300:]})
        if code != 0:
            evidence.finish("FAIL", failure="daemon stop failed")
            return 1

        daemon_proc = subprocess.Popen(
            [str(venv_bin / "researchd"), "--config", str(config_path), "serve"],
            stdout=log_path.open("ab"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        evidence.phase("daemon_restart").update({"second_pid": daemon_proc.pid})
        health = poll("daemon READY after restart", ready, deadline_seconds=30.0)
        evidence.phase("daemon_restart").update({"ready": health})

        sessions_after = poll(
            "3 HEALTHY sessions after restart", sessions_healthy, deadline_seconds=30.0,
        )
        evidence.phase("runtime_sessions").update({"after_restart": sessions_after})
        reattached = all(row.get("reattach_state") == "ATTACHED" for row in sessions_after)

        status, timeline_after = http_json("GET", f"{base_url}/api/timeline/run_ph07_e2e", token=token)
        events_after = [item for item in timeline_after if item.get("kind") == "event"]
        evidence.phase("timeline_post_restart").update({
            "event_count": len(events_after),
            "unchanged": len(events_after) == evidence.phase("timeline_pre_restart")["event_count"],
        })

        status, system_events = http_json("GET", f"{base_url}/api/system-events", token=token)
        offsets = [item.get("stream_offset") for item in system_events if isinstance(system_events, list)]
        monotonic = all(a is None or b is None or a < b for a, b in zip(offsets, offsets[1:]))
        evidence.phase("system_events").update({
            "count": len(offsets),
            "offsets_monotonic": monotonic,
        })

        status, health_doc = http_json("GET", f"{base_url}/api/health", token=token)
        driver_health = health_doc.get("orchestration_driver") if isinstance(health_doc, dict) else None
        evidence.phase("driver_health").update(driver_health or {})

        # --------------------------------------------------------------
        # Phase 7: verdict + teardown
        # --------------------------------------------------------------
        observed = final_doc.get("state")
        checks = {
            "terminal_state_expected": observed == EXPECTED_TERMINAL_STATE,
            "required_events_present": REQUIRED_EVENT_TYPES <= event_types,
            "artifact_present": len(artifact_items) >= 1,
            "claim_present": len(claim_items) >= 1,
            "timeline_unchanged_after_restart":
                evidence.phase("timeline_post_restart")["unchanged"],
            "sessions_reattached": reattached,
            "system_event_offsets_monotonic": monotonic,
        }
        evidence.data["checks"] = checks
        evidence.data["observed_terminal_state"] = observed
        evidence.data["expected_terminal_state"] = EXPECTED_TERMINAL_STATE
        evidence.data["candidate_commit"] = args.commit

        # Teardown: stop the supervised Agent sessions, then the daemon.
        for row in sessions_after:
            http_json(
                "POST",
                f"{base_url}/api/runtime-sessions/{row['runtime_session_id']}/stop",
                {"command_id": f"cmd_ph07_stop_{row['runtime_id']}",
                 "runtime_id": row["runtime_id"], "expected_version": row["version"]},
                token=token,
            )
        run_console([str(venv_bin / "research"), "--config", str(config_path), "daemon", "stop"],
                    timeout=60.0)
        evidence.data["teardown"] = "sessions stopped, daemon stopped"

        if observed == EXPECTED_TERMINAL_STATE and all(checks.values()):
            evidence.finish("PASS")
            return 0
        evidence.finish("FAIL", failure=(
            f"terminal state {observed!r} != expected {EXPECTED_TERMINAL_STATE!r} "
            f"or coherence checks failed: {[k for k, v in checks.items() if not v]}"
        ))
        return 1
    except TimeoutError as error:
        evidence.finish("FAIL", failure=f"deadline exceeded: {error}")
        return 1
    finally:
        if daemon_proc is not None and daemon_proc.poll() is None:
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
