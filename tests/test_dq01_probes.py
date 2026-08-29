import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]


def _run(script: str, *arguments: str) -> dict[str, object]:
    completed = subprocess.run(
        (sys.executable, str(ROOT / "scripts" / script), *arguments),
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def test_dq01_host_probes_emit_candidate_bound_pass_result(tmp_path: Path) -> None:
    preflight = _run("dq01_preflight.py", "--strict", "--target", str(tmp_path))
    assert preflight["release_commit"]
    filesystem_fingerprint = preflight["filesystem"]
    assert isinstance(filesystem_fingerprint, dict)
    assert filesystem_fingerprint["target"] == str(tmp_path)
    assert preflight["failures"] == []

    filesystem = _run(
        "dq01_filesystem_probe.py", "--root", str(tmp_path), "--iterations", "8"
    )
    assert filesystem["release_commit"] == preflight["release_commit"]
    assert filesystem["target_root"] == str(tmp_path)
    assert filesystem["passed"] is True
    assert filesystem["failures"] == []
