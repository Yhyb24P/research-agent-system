"""AC43 installed-artifact failure-matrix E2E driver.

Drives a clean installed artifact through a failure-and-recovery workflow
using only the installed console scripts and the authenticated HTTP surface:

    bootstrap -> install two executors (A fails, B succeeds) -> task create
    -> executor A fails deterministically -> HUMAN retries with executor B
    -> Run reaches COMPLETED -> daemon restart -> projection coherence.

Restrictions honored: no researchd imports, no SQLite, no direct
Orchestrator/Handoff service calls, no client-supplied Agent argv.
Deterministic Agent services only.

Exit code 0 when the run reaches COMPLETED and every coherence check passes;
1 otherwise, with a JSON evidence document printed to stdout.
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
    parser = argparse.ArgumentParser(prog="failure-e2e")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--daemon-port", type=int, default=18789)
    parser.add_argument("--planner-port", type=int, default=19021)
    parser.add_argument("--coder-a-port", type=int, default=19023)
    parser.add_argument("--coder-b-port", type=int, default=19024)
    parser.add_argument("--reviewer-port", type=int, default=19022)
    parser.add_argument("--run-timeout", type=float, default=180.0)
    args = parser.parse_args()

    evidence = Evidence()
    harness_dir = Path(__file__).resolve().parent
    venv_bin = Path(sys.executable).parent
    python = Path(sys.executable)

    # ------------------------------------------------------------------
    # Phase 0: preflight
    # ------------------------------------------------------------------
    preflight: dict[str, Any] = {
        "python": str(python),
    }
    for script in ("researchd", "research", "researchctl"):
        preflight[f"console_{script}"] = (venv_bin / script).is_file()
    evidence.phase("preflight").update(preflight)
    if not all(preflight[f"console_{name}"] for name in
               ("researchd", "research", "researchctl")):
        evidence.finish("FAIL", failure="installed console scripts missing")
        return 1

    root = args.root.resolve()
    if root.exists():
        raise SystemExit(f"refusing to reuse a non-clean E2E root: {root}")
    state_root = root / "state"
    config_path = root / "config.json"
    config_path.parent.mkdir(parents=True)

    git_source = root / "git-source"
    git_source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(git_source)], check=True)
    (git_source / "README.md").write_text("# Failure E2E source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_source), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(git_source), "-c", "user.email=e2e@failure.local",
         "-c", "user.name=failure-e2e", "commit", "-q", "-m", "seed"],
        check=True,
    )

    config = {
        "database": str(state_root / "researchd.db"),
        "artifact_root": str(root / "artifacts"),
        "state_root": str(state_root),
        "host": "127.0.0.1",
        "port": args.daemon_port,
        "workspace_capabilities": ["sandbox.shell"],
        "user_capabilities": ["sandbox.shell"],
        "workspace_sources": {"ws_failure": {"root": str(git_source)}},
    }
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    base_url = f"http://127.0.0.1:{args.daemon_port}"

    # ------------------------------------------------------------------
    # Phase 1: bootstrap
    # ------------------------------------------------------------------
    code, out, err = run_console(
        [str(venv_bin / "research"), "--config", str(config_path), "init"]
    )
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
    # Phase 2: install deterministic agents
    # ------------------------------------------------------------------
    # Build agent definitions: planner, reviewer, and two coders (A fails, B succeeds).
    # Coder A uses failing_executor.py; Coder B uses success_executor.py.
    definitions = build_definitions(
        python=python,
        harness_dir=harness_dir,
        coder_binary=venv_bin / "research-coder-agent",
        cwd=harness_dir,
        planner_port=args.planner_port,
        coder_port=args.coder_a_port,
        reviewer_port=args.reviewer_port,
    )
    # Add two deterministic executors with distinct Agent IDs and ports
    from definitions import _process_definition
    definitions["coder_a"] = _process_definition(
        agent_id="agent_ph07_coder_a",
        display_name="PH07 Coder A (failing)",
        role="executor",
        runtime_id="runtime_ph07_coder_a_process",
        port=args.coder_a_port,
        argv=[str(python), str(harness_dir / "failing_executor.py"),
              "--host", "127.0.0.1", "--port", str(args.coder_a_port)],
        cwd=str(harness_dir),
    )
    definitions["coder_b"] = _process_definition(
        agent_id="agent_ph07_coder_b",
        display_name="PH07 Coder B (success)",
        role="executor",
        runtime_id="runtime_ph07_coder_b_process",
        port=args.coder_b_port,
        argv=[str(python), str(harness_dir / "success_executor.py"),
              "--host", "127.0.0.1", "--port", str(args.coder_b_port)],
        cwd=str(harness_dir),
    )

    # Remove the original "coder" (replaced by coder_a and coder_b)
    definitions.pop("coder", None)

    install_receipts: dict[str, Any] = {}
    for role, definition in definitions.items():
        definition_path = root / f"{role}_definition.json"
        definition_path.write_text(
            json.dumps(definition, indent=2, sort_keys=True), encoding="utf-8"
        )
        code, out, err = run_console([
            str(venv_bin / "researchd"), "--config", str(config_path),
            "install-agent", str(definition_path),
        ])
        receipt = json.loads(out) if out and code == 0 else {
            "exit_code": code, "stderr": err[-400:],
        }
        install_receipts[role] = receipt
        if code != 0:
            evidence.phase("install").update(install_receipts)
            evidence.finish("FAIL", failure=f"install-agent failed for {role}")
            return 1
    evidence.phase("install").update(install_receipts)

    # ------------------------------------------------------------------
    # Phase 3: daemon serve + agent starts
    # ------------------------------------------------------------------
    token = (state_root / "control.token").read_text(encoding="utf-8").strip()
    log_path = state_root / "daemon.log"
    daemon_proc: subprocess.Popen[bytes] | None = None
    daemon_proc = subprocess.Popen(
        [str(venv_bin / "researchd"), "--config", str(config_path), "serve"],
        stdout=log_path.open("ab"), stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
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

        # Start planner, coder A, coder B, reviewer
        for role, port in (
            ("planner", args.planner_port),
            ("coder_a", args.coder_a_port),
            ("coder_b", args.coder_b_port),
            ("reviewer", args.reviewer_port),
        ):
            agent_id = f"agent_ph07_{role}"
            status, envelope = http_json(
                "POST", f"{base_url}/api/agents/{agent_id}/start",
                {"command_id": f"cmd_fail_start_{role}"}, token=token,
            )
            if status != 202 or envelope.get("status") != "ACCEPTED":
                evidence.phase("agent_starts").update({role: envelope})
                evidence.finish("FAIL", failure=f"agent start rejected for {role}")
                return 1

        # ------------------------------------------------------------------
        # Phase 4: create workspace and task
        # ------------------------------------------------------------------
        status, workspace = http_json(
            "POST", f"{base_url}/api/workspaces",
            {"command_id": "cmd_fail_ws", "workspace_id": "ws_failure",
             "name": "Failure E2E"},
            token=token,
        )
        if status != 202:
            evidence.finish("FAIL", failure="workspace create rejected")
            return 1

        status, task = http_json(
            "POST", f"{base_url}/api/runs",
            {"command_id": "cmd_fail_task", "workspace_id": "ws_failure",
             "objective": "Failure E2E: executor A fails, retry with B",
             "run_id": "run_failure_e2e"},
            token=token,
        )
        if status != 202:
            evidence.finish("FAIL", failure="task create rejected")
            return 1

        # ------------------------------------------------------------------
        # Phase 5: poll for failure, then retry with executor B
        # ------------------------------------------------------------------
        deadline = time.monotonic() + args.run_timeout
        retried = False
        final_doc: dict[str, Any] = {}

        while True:
            status, document = http_json(
                "GET", f"{base_url}/api/runs/run_failure_e2e", token=token
            )
            state = document.get("state") if isinstance(document, dict) else None

            if state in TERMINAL_STATES:
                final_doc = document
                break

            # Check for WAITING_EXTERNAL (failure state)
            if state == "WAITING_EXTERNAL" and not retried:
                # Find the failed work order and retry with executor B
                status, wo_list = http_json(
                    "GET", f"{base_url}/api/runs/run_failure_e2e", token=token
                )
                work_orders = wo_list.get("work_orders", []) if isinstance(wo_list, dict) else []
                for wo in work_orders:
                    if wo.get("state") in ("EXECUTION_FAILED", "FAILED"):
                        # Retry with target agent B
                        status, retry_envelope = http_json(
                            "POST",
                            f"{base_url}/api/work-orders/{wo['work_order_id']}/retry",
                            {"command_id": "cmd_fail_retry",
                             "target_agent_id": "agent_ph07_coder_b"},
                            token=token,
                        )
                        evidence.phase("retry").update({
                            "work_order_id": wo["work_order_id"],
                            "http_status": status,
                            "envelope": retry_envelope,
                        })
                        retried = True
                        break

            if time.monotonic() >= deadline:
                evidence.finish("FAIL", failure="run did not reach terminal state",
                                last_state=state)
                return 1
            time.sleep(0.5)

        evidence.phase("run_terminal").update(final_doc)

        # ------------------------------------------------------------------
        # Phase 6: projection coherence
        # ------------------------------------------------------------------
        status, timeline = http_json(
            "GET", f"{base_url}/api/timeline/run_failure_e2e", token=token
        )
        event_types = {
            item.get("event_type")
            for item in timeline if isinstance(item, dict) and item.get("kind") == "event"
        }
        evidence.phase("timeline").update({
            "event_count": len([
                item for item in timeline
                if isinstance(item, dict) and item.get("kind") == "event"
            ]),
            "event_types": sorted(t for t in event_types if t is not None),
        })
        invocations = [
            item for item in timeline
            if isinstance(item, dict)
            and item.get("kind") == "invocation"
            and item.get("purpose") == "EXECUTE"
        ]
        attempts = [
            item for item in timeline
            if isinstance(item, dict) and item.get("kind") == "attempt"
        ]
        delegations = [
            item for item in timeline
            if isinstance(item, dict)
            and item.get("kind") == "delegation"
            and item.get("purpose") == "EXECUTE"
        ]
        grants = [
            item for item in timeline
            if isinstance(item, dict) and item.get("kind") == "workspace_grant"
        ]
        evidence.phase("authority_closure").update({
            "execute_invocations": invocations,
            "attempts": attempts,
            "execute_delegations": delegations,
            "workspace_grant_ids": [item.get("workspace_grant_id") for item in grants],
        })

        # ------------------------------------------------------------------
        # Phase 7: daemon restart and coherence
        # ------------------------------------------------------------------
        code, out, err = run_console(
            [str(venv_bin / "research"), "--config", str(config_path),
             "daemon", "stop"],
            timeout=60.0,
        )
        evidence.phase("daemon_restart").update({
            "stop_exit_code": code, "stop_stderr": err[-300:],
        })

        daemon_proc = subprocess.Popen(
            [str(venv_bin / "researchd"), "--config", str(config_path), "serve"],
            stdout=log_path.open("ab"), stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        evidence.phase("daemon_restart").update({"second_pid": daemon_proc.pid})

        health = poll("daemon READY after restart", ready, deadline_seconds=30.0)
        evidence.phase("daemon_restart").update({"ready": health})

        status, timeline_after = http_json(
            "GET", f"{base_url}/api/timeline/run_failure_e2e", token=token
        )
        events_after = [
            item for item in timeline_after
            if isinstance(item, dict) and item.get("kind") == "event"
        ]
        evidence.phase("timeline_post_restart").update({
            "event_count": len(events_after),
            "projection_unchanged": timeline_after == timeline,
        })

        # ------------------------------------------------------------------
        # Phase 8: verdict + teardown
        # ------------------------------------------------------------------
        observed = final_doc.get("state")
        checks = {
            "terminal_state_expected": observed == EXPECTED_TERMINAL_STATE,
            "retried_with_target_agent": retried,
            "failed_agent_a_then_succeeded_agent_b": (
                len(invocations) == 2
                and invocations[0].get("agent_id") == "agent_ph07_coder_a"
                and invocations[0].get("status") == "FAILED"
                and isinstance(invocations[0].get("failure_category"), str)
                and isinstance(invocations[0].get("reason_code"), str)
                and invocations[1].get("agent_id") == "agent_ph07_coder_b"
                and invocations[1].get("status") == "SUCCEEDED"
            ),
            "attempt_authority_closed": (
                len(attempts) == 2
                and [item.get("state") for item in attempts] == ["FAILED", "SUCCEEDED"]
            ),
            "delegation_authority_closed": (
                len(delegations) == 2
                and [item.get("state") for item in delegations] == ["FAILED", "COMPLETED"]
            ),
            "retry_uses_distinct_workspace_grant": (
                len(grants) == 2
                and len({item.get("workspace_grant_id") for item in grants}) == 2
            ),
            "required_audit_events_present": {
                "AGENT_EXECUTION_WAITING",
                "ATTEMPT_RETRY_REQUESTED",
                "AGENT_EXECUTION_RESUMED",
                "RUN_COMPLETED",
            }.issubset(event_types),
            "restart_projection_unchanged": timeline_after == timeline,
        }
        evidence.data["checks"] = checks
        evidence.data["observed_terminal_state"] = observed
        evidence.data["expected_terminal_state"] = EXPECTED_TERMINAL_STATE
        evidence.data["candidate_commit"] = args.commit

        run_console(
            [str(venv_bin / "research"), "--config", str(config_path),
             "daemon", "stop"],
            timeout=60.0,
        )
        evidence.data["teardown"] = "daemon stopped"

        if observed == EXPECTED_TERMINAL_STATE and all(checks.values()):
            evidence.finish("PASS")
            return 0
        evidence.finish("FAIL", failure=(
            f"terminal state {observed!r} != expected {EXPECTED_TERMINAL_STATE!r} "
            f"or checks failed: {[k for k, v in checks.items() if not v]}"
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
