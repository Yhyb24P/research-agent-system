import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.storage.db import create_sqlite_engine, session_factory


ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start(base: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [*base, "serve"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_health(process: subprocess.Popen[str], port: int) -> dict[str, object]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                return cast(dict[str, object], json.load(response))
        except HTTPError as error:
            if error.code == 503:
                return cast(dict[str, object], json.load(error))
            raise
        except (URLError, TimeoutError, ConnectionError):
            time.sleep(0.05)
    failure_output = process.stderr.read() if process.stderr else ""
    raise AssertionError(f"researchd failed before READY: {failure_output}")


def _post(port: int, path: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        assert response.status == 202
        return cast(dict[str, object], json.load(response))


def _terminate(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _register_process_runtime(database: Path) -> None:
    registry = AgentRegistryService(session_factory(create_sqlite_engine(database)))
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_restart_test"),
        display_name="Restart test Agent",
        roles=("executor",),
        skills=("runtime.test",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_crash_test"),
        agent_id=AgentId("agent_restart_test"),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Restart test process",
    ))
def test_researchd_starts_as_independent_process(tmp_path: Path) -> None:
    database = tmp_path / "researchd.db"
    artifacts = tmp_path / "artifacts"
    state = tmp_path / "state"
    config = tmp_path / "researchd.json"
    config.write_text(json.dumps({
        "database": str(database),
        "artifact_root": str(artifacts),
        "state_root": str(state),
        "repositories": {},
        "job_commands": {},
        "host": "127.0.0.1",
        "port": _free_port(),
    }))
    base = [
        sys.executable,
        "-m",
        "researchd.daemon.cli",
        "--config",
        str(config),
    ]
    initialized = subprocess.run(
        [*base, "init"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert initialized.returncode == 0, initialized.stderr

    port = int(json.loads(config.read_text())["port"])
    process = _start(base)
    try:
        health = _wait_for_health(process, port)
        assert health["state"] == "READY"
        assert health["ready"] is True
    finally:
        _terminate(process)


def test_researchd_restart_reattaches_live_runtime_and_preserves_audit_order(
    tmp_path: Path,
) -> None:
    database = tmp_path / "researchd.db"
    port = _free_port()
    config = tmp_path / "researchd.json"
    config.write_text(json.dumps({
        "database": str(database),
        "artifact_root": str(tmp_path / "artifacts"),
        "state_root": str(tmp_path / "state"),
        "repositories": {},
        "job_commands": {},
        "host": "127.0.0.1",
        "port": port,
    }))
    base = [sys.executable, "-m", "researchd.daemon.cli", "--config", str(config)]
    initialized = subprocess.run(
        [*base, "init"], check=False, capture_output=True, text=True, timeout=30
    )
    assert initialized.returncode == 0, initialized.stderr

    _register_process_runtime(database)

    first = _start(base)
    second: subprocess.Popen[str] | None = None
    runtime_pid: int | None = None
    try:
        _wait_for_health(first, port)
        started = _post(port, "/api/runtime-sessions/start", {
            "command_id": "command_restart_start",
            "runtime_session_id": "runtime_session_restart_test",
            "runtime_id": "runtime_crash_test",
            "actor_type": "SYSTEM",
            "actor_id": "restart-test",
            "launch_spec": {"argv": ["/usr/bin/sleep", "60"], "cwd": str(tmp_path)},
        })
        started_resource = cast(dict[str, object], started["resource"])
        assert started["status"] == "ACCEPTED"
        assert started_resource["supervisor_state"] == "HEALTHY"
        identity = cast(dict[str, object], started_resource["external_identity"])
        runtime_pid = int(cast(int, identity["pid"]))

        with urlopen(f"http://127.0.0.1:{port}/api/system-events?after=0") as response:
            before = json.load(response)["events"]
        before_offsets = [item["stream_offset"] for item in before]
        _terminate(first)

        second = _start(base)
        health = _wait_for_health(second, port)
        startup = cast(dict[str, object], health["startup"])
        phases = cast(list[dict[str, object]], startup["phases"])
        runtime_phase = phases[4]
        assert runtime_phase["phase"] == "RUNTIME_RECONCILIATION"
        assert runtime_phase["affected_count"] == 1
        with urlopen(f"http://127.0.0.1:{port}/api/runtime-sessions") as response:
            session = json.load(response)[0]
        assert session["supervisor_state"] == "HEALTHY"
        assert session["reattach_state"] == "ATTACHED"

        with urlopen(f"http://127.0.0.1:{port}/api/system-events?after=0") as response:
            after = json.load(response)["events"]
        after_offsets = [item["stream_offset"] for item in after]
        assert after_offsets == list(range(1, len(after_offsets) + 1))
        assert after_offsets[:len(before_offsets)] == before_offsets

        stopped = _post(port, "/api/runtime-sessions/runtime_session_restart_test/stop", {
            "command_id": "command_restart_stop",
            "runtime_id": "runtime_crash_test",
            "actor_type": "SYSTEM",
            "actor_id": "restart-test",
            "expected_version": session["version"],
        })
        stopped_resource = cast(dict[str, object], stopped["resource"])
        assert stopped["status"] == "ACCEPTED"
        assert stopped_resource["supervisor_state"] == "STOPPED"
        runtime_pid = None
    finally:
        if first.poll() is None:
            _terminate(first)
        if second is not None and second.poll() is None:
            _terminate(second)
        if runtime_pid is not None:
            import os
            import signal

            try:
                os.kill(runtime_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_start_intent_crash_never_relaunches_uncertain_process(tmp_path: Path) -> None:
    database = tmp_path / "researchd.db"
    port = _free_port()
    config = tmp_path / "researchd.json"
    config.write_text(json.dumps({
        "database": str(database),
        "artifact_root": str(tmp_path / "artifacts"),
        "state_root": str(tmp_path / "state"),
        "repositories": {}, "job_commands": {},
        "host": "127.0.0.1", "port": port,
    }))
    base = [sys.executable, "-m", "researchd.daemon.cli", "--config", str(config)]
    assert subprocess.run([*base, "init"], check=False).returncode == 0
    _register_process_runtime(database)
    crashed = subprocess.run([
        sys.executable,
        str(ROOT / "tests/fixtures/runtime_intent_crasher.py"),
        "start", str(database), str(tmp_path),
    ], check=False)
    assert crashed.returncode == 73

    daemon = _start(base)
    try:
        health = _wait_for_health(daemon, port)
        startup = cast(dict[str, object], health["startup"])
        phases = cast(list[dict[str, object]], startup["phases"])
        assert health["state"] == "FAILED"
        assert health["ready"] is False
        assert phases[4]["status"] == "FAIL"
        assert phases[4]["error_type"] == "RuntimeError"
        with urlopen(f"http://127.0.0.1:{port}/api/runtime-sessions") as response:
            session = json.load(response)[0]
        assert session["supervisor_state"] == "RECONCILIATION_REQUIRED"
        assert session["external_identity"] is None
        assert session["exit_reason"] == "missing_external_identity"
        with pytest.raises(HTTPError) as rejected:
            _post(port, "/api/runtime-sessions/start", {
                "command_id": "command_must_be_rejected",
                "runtime_session_id": "runtime_session_rejected",
                "runtime_id": "runtime_crash_test",
                "actor_type": "SYSTEM", "actor_id": "crash-test",
                "launch_spec": {"argv": ["/usr/bin/true"], "cwd": str(tmp_path)},
            })
        assert rejected.value.code == 409
    finally:
        _terminate(daemon)


def test_stop_intent_crash_is_finished_by_restart_reconciliation(tmp_path: Path) -> None:
    database = tmp_path / "researchd.db"
    port = _free_port()
    config = tmp_path / "researchd.json"
    config.write_text(json.dumps({
        "database": str(database),
        "artifact_root": str(tmp_path / "artifacts"),
        "state_root": str(tmp_path / "state"),
        "repositories": {}, "job_commands": {},
        "host": "127.0.0.1", "port": port,
    }))
    base = [sys.executable, "-m", "researchd.daemon.cli", "--config", str(config)]
    assert subprocess.run([*base, "init"], check=False).returncode == 0
    _register_process_runtime(database)
    first = _start(base)
    second: subprocess.Popen[str] | None = None
    runtime_pid: int | None = None
    try:
        _wait_for_health(first, port)
        started = _post(port, "/api/runtime-sessions/start", {
            "command_id": "command_normal_start",
            "runtime_session_id": "runtime_session_crash_test",
            "runtime_id": "runtime_crash_test",
            "actor_type": "SYSTEM", "actor_id": "crash-test",
            "launch_spec": {"argv": ["/usr/bin/sleep", "60"], "cwd": str(tmp_path)},
        })
        started_resource = cast(dict[str, object], started["resource"])
        assert started["status"] == "ACCEPTED"
        identity = cast(dict[str, object], started_resource["external_identity"])
        runtime_pid = int(cast(int, identity["pid"]))
        _terminate(first)
        crashed = subprocess.run([
            sys.executable,
            str(ROOT / "tests/fixtures/runtime_intent_crasher.py"),
            "stop", str(database), str(tmp_path),
            "--expected-version", str(started_resource["version"]),
        ], check=False)
        assert crashed.returncode == 73

        second = _start(base)
        health = _wait_for_health(second, port)
        startup = cast(dict[str, object], health["startup"])
        phases = cast(list[dict[str, object]], startup["phases"])
        assert phases[4]["affected_count"] == 1
        with urlopen(f"http://127.0.0.1:{port}/api/runtime-sessions") as response:
            session = json.load(response)[0]
        assert session["supervisor_state"] == "STOPPED"
        assert session["exit_reason"] == "reconciled_stop"
        runtime_pid = None
    finally:
        if first.poll() is None:
            _terminate(first)
        if second is not None and second.poll() is None:
            _terminate(second)
        if runtime_pid is not None:
            import os
            import signal
            try:
                os.kill(runtime_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_generic_command_crash_blocks_ready_without_replaying(tmp_path: Path) -> None:
    database = tmp_path / "researchd.db"
    port = _free_port()
    config = tmp_path / "researchd.json"
    config.write_text(json.dumps({
        "database": str(database),
        "artifact_root": str(tmp_path / "artifacts"),
        "state_root": str(tmp_path / "state"),
        "repositories": {}, "job_commands": {},
        "host": "127.0.0.1", "port": port,
    }))
    base = [sys.executable, "-m", "researchd.daemon.cli", "--config", str(config)]
    assert subprocess.run([*base, "init"], check=False).returncode == 0
    crashed = subprocess.run([
        sys.executable,
        str(ROOT / "tests/fixtures/daemon_command_crasher.py"),
        str(database),
    ], check=False)
    assert crashed.returncode == 74

    daemon = _start(base)
    try:
        health = _wait_for_health(daemon, port)
        assert health["state"] == "FAILED"
        assert health["ready"] is False
        startup = cast(dict[str, object], health["startup"])
        phases = cast(list[dict[str, object]], startup["phases"])
        assert phases[7]["phase"] == "AUDIT_STREAM_HEALTH"
        assert phases[7]["status"] == "FAIL"
        with urlopen(
            f"http://127.0.0.1:{port}/api/daemon-commands?status=ACCEPTED"
        ) as response:
            commands = json.load(response)
        assert len(commands) == 1
        assert commands[0]["command_id"] == "command_generic_crash"
        assert commands[0]["status"] == "ACCEPTED"
        with pytest.raises(HTTPError) as rejected:
            _post(port, "/api/runs/run_another/cancel", {
                "command_id": "command_after_crash",
                "actor_type": "SYSTEM",
                "actor_id": "crash-test",
            })
        assert rejected.value.code == 409
    finally:
        _terminate(daemon)
