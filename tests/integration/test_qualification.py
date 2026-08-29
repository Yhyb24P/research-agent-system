from pathlib import Path
from runpy import run_path

_RC_TAG = run_path(str(Path(__file__).parents[2] / "scripts" / "dq06_evidence_check.py"))["_RC_TAG"]


def test_release_qualification_accepts_repository_rc_tag_series() -> None:
    assert _RC_TAG.fullmatch("v0.1.0-rc.102")
    assert _RC_TAG.fullmatch("v1.0.0-rc.1")
    assert not _RC_TAG.fullmatch("v0.1.0")
