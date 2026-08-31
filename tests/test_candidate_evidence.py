"""CR01 re-verification (4baa2be): candidate_evidence.py fail-closed semantics.

Covers the exact-candidate evidence gate added by 4baa2be "ci: add exact
candidate installed-artifact gate":

- exact mode fails closed on candidate/checked-out commit mismatch, a
  missing candidate tag, a non-PASS product E2E, and release-manifest
  commit or tag mismatches;
- correct inputs yield a complete hash-bound summary whose
  ``qualification_claim`` never exceeds the mode's authority
  (PREFLIGHT_ONLY for preflight, SOFTWARE_CANDIDATE_GATE_ONLY for exact).

Post-hoc test: no source changes, no commits.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
TAG = "v1.0.0-rc.81"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "candidate_evidence_under_test", ROOT / "scripts" / "candidate_evidence.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(tmp_path: Path, *, e2e_result: str = "PASS", manifest_commit: str = COMMIT_A,
            manifest_tags: list[str] | None = None) -> dict[str, str]:
    wheel = tmp_path / "wheel.whl"
    wheel.write_bytes(b"wheel-bytes")
    sdist = tmp_path / "sdist.tar.gz"
    sdist.write_bytes(b"sdist-bytes")
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"bomFormat": "CycloneDX", "components": []}))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "source": {"commit": manifest_commit,
                   "tags": manifest_tags if manifest_tags is not None else [TAG]},
    }))
    e2e = tmp_path / "e2e.json"
    e2e.write_text(json.dumps({"result": e2e_result}))
    return {"wheel": str(wheel), "sdist": str(sdist), "sbom": str(sbom),
            "manifest": str(manifest), "e2e": str(e2e)}


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
         *, candidate_tag: str | None = TAG, candidate_commit: str = COMMIT_A,
         checked_out_commit: str = COMMIT_A, e2e_result: str = "PASS",
         manifest_commit: str = COMMIT_A, manifest_tags: list[str] | None = None,
         output: str | None = None) -> int:
    files = _inputs(tmp_path, e2e_result=e2e_result, manifest_commit=manifest_commit,
                    manifest_tags=manifest_tags)
    argv = [
        "candidate_evidence.py", "--mode", mode,
        "--candidate-commit", candidate_commit,
        "--checked-out-commit", checked_out_commit,
        "--project-version", "1.0.0rc81",
        "--wheel", files["wheel"], "--sdist", files["sdist"],
        "--manifest", files["manifest"], "--sbom", files["sbom"],
        "--e2e", files["e2e"],
        "--workflow-run-identity", "local/unit-test",
        "--output", output or str(tmp_path / "evidence.json"),
    ]
    if candidate_tag is not None:
        argv[3:3] = ["--candidate-tag", candidate_tag]
    monkeypatch.setattr(sys, "argv", argv)
    return _module().main()


def test_exact_commit_mismatch_fails_closed(monkeypatch, tmp_path) -> None:
    with pytest.raises(SystemExit, match="matching candidate/checked-out commits"):
        _run(monkeypatch, tmp_path, "exact", candidate_commit=COMMIT_A,
             checked_out_commit=COMMIT_B)
    assert not (tmp_path / "evidence.json").exists()


def test_exact_missing_tag_fails_closed(monkeypatch, tmp_path) -> None:
    with pytest.raises(SystemExit, match="requires a tag"):
        _run(monkeypatch, tmp_path, "exact", candidate_tag=None)
    assert not (tmp_path / "evidence.json").exists()


def test_exact_non_pass_e2e_fails_closed(monkeypatch, tmp_path) -> None:
    with pytest.raises(SystemExit, match="did not report PASS"):
        _run(monkeypatch, tmp_path, "exact", e2e_result="FAIL")
    assert not (tmp_path / "evidence.json").exists()


def test_exact_manifest_commit_mismatch_fails_closed(monkeypatch, tmp_path) -> None:
    with pytest.raises(SystemExit, match="manifest commit does not match checkout"):
        _run(monkeypatch, tmp_path, "exact", manifest_commit=COMMIT_B)
    assert not (tmp_path / "evidence.json").exists()


def test_exact_manifest_tag_mismatch_fails_closed(monkeypatch, tmp_path) -> None:
    with pytest.raises(SystemExit, match="does not record candidate tag"):
        _run(monkeypatch, tmp_path, "exact", manifest_tags=["v1.0.0-rc.80"])
    assert not (tmp_path / "evidence.json").exists()


def test_malformed_commit_fails_closed(monkeypatch, tmp_path) -> None:
    with pytest.raises(SystemExit, match="40-hex"):
        _run(monkeypatch, tmp_path, "preflight", candidate_commit="abc")


def test_preflight_correct_inputs_yield_hash_bound_summary(monkeypatch, tmp_path) -> None:
    files = _inputs(tmp_path)
    assert _run(monkeypatch, tmp_path, "preflight", candidate_tag=None) == 0
    summary = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "preflight"
    assert summary["candidate_tag"] is None
    assert summary["candidate_commit"] == COMMIT_A
    assert summary["checked_out_commit"] == COMMIT_A
    assert summary["project_version"] == "1.0.0rc81"
    assert summary["product_e2e_result"] == "PASS"
    assert summary["qualification_claim"] == "PREFLIGHT_ONLY"
    assert summary["workflow_run_identity"] == "local/unit-test"
    assert summary["wheel_sha256"] == _sha256(Path(files["wheel"]))
    assert summary["sdist_sha256"] == _sha256(Path(files["sdist"]))
    assert summary["release_manifest_sha256"] == _sha256(Path(files["manifest"]))
    assert summary["sbom_sha256"] == _sha256(Path(files["sbom"]))


def test_exact_correct_inputs_bind_tag_and_gate_claim(monkeypatch, tmp_path) -> None:
    assert _run(monkeypatch, tmp_path, "exact") == 0
    summary = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert summary["mode"] == "exact"
    assert summary["candidate_tag"] == TAG
    assert summary["qualification_claim"] == "SOFTWARE_CANDIDATE_GATE_ONLY"
