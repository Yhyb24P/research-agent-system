"""Detached internal process that makes local Job status durable across restarts."""

import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

child: subprocess.Popen[bytes] | None = None


def atomic_status(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    os.replace(temporary, path)


def forward_signal(signum: int, frame: object) -> None:
    del frame
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass


def main() -> int:
    global child
    spec_path = Path(sys.argv[1])
    spec = json.loads(spec_path.read_text())
    status_path = Path(spec["status_path"])
    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)
    child = subprocess.Popen(
        spec["argv"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        env={"PATH": "/usr/bin", "HOME": "/nonexistent", "TMPDIR": "/tmp"},
    )
    atomic_status(status_path, {"state": "RUNNING", "runner_pid": os.getpid(), "child_pid": child.pid, "updated_at": datetime.now(UTC).isoformat()})
    returncode = child.wait()
    atomic_status(status_path, {"state": "SUCCEEDED" if returncode == 0 else "FAILED", "runner_pid": os.getpid(), "child_pid": child.pid, "returncode": returncode, "updated_at": datetime.now(UTC).isoformat()})
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
