import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_researchd_starts_as_independent_process(tmp_path: Path) -> None:
    database = tmp_path / "researchd.db"
    artifacts = tmp_path / "artifacts"
    state = tmp_path / "state"
    base = [
        sys.executable,
        "-m",
        "researchd.daemon.cli",
        "--database",
        str(database),
        "--artifact-root",
        str(artifacts),
        "--state-root",
        str(state),
    ]
    initialized = subprocess.run(
        [*base, "init"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert initialized.returncode == 0, initialized.stderr

    port = _free_port()
    process = subprocess.Popen(
        [*base, "serve", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        health: dict[str, object] | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                    health = json.load(response)
                    break
            except (URLError, TimeoutError, ConnectionError):
                time.sleep(0.05)
        assert process.poll() is None, process.stderr.read() if process.stderr else ""
        assert health is not None
        assert health["state"] == "READY"
        assert health["ready"] is True
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
