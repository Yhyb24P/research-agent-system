from pathlib import Path

from scripts.dq01_filesystem_probe import probe
from scripts.dq01_preflight import collect, failures


def test_dq01_host_probes_emit_candidate_bound_pass_result(tmp_path: Path) -> None:
    preflight = collect(target=tmp_path)
    assert preflight["release_commit"]
    assert preflight["filesystem"]["target"] == str(tmp_path)
    assert failures(preflight) == []

    filesystem = probe(tmp_path, iterations=8)
    assert filesystem["release_commit"] == preflight["release_commit"]
    assert filesystem["target_root"] == str(tmp_path)
    assert filesystem["passed"] is True
    assert filesystem["failures"] == []
