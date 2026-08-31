"""PH04: strong daemon identity and fail-closed stop semantics.

The identity file is the only thing that authorizes signaling the daemon:
a live identity refuses takeover, a stale one is reclaimed, and a PID reuse
with a different generation is never treated as the same process.
"""

import json
import os
import threading
from pathlib import Path
from typing import cast

import pytest

from researchd.client.lifecycle import stop_daemon
from researchd.daemon import identity


def test_claim_writes_strong_identity_and_refuses_live_takeover(tmp_path: Path) -> None:
    claimed = identity.claim(tmp_path, "sha_config")
    assert claimed["pid"] == os.getpid()
    assert isinstance(claimed["start_ticks"], int)
    assert isinstance(claimed["boot_id"], str) and claimed["boot_id"]
    assert claimed["config_sha256"] == "sha_config"
    assert identity.identity_path(tmp_path).exists()
    assert identity.is_live(claimed) is True

    with pytest.raises(RuntimeError, match="already live"):
        identity.claim(tmp_path, "sha_config")

    identity.release(tmp_path, claimed)
    assert not identity.identity_path(tmp_path).exists()


def test_stale_identity_is_reclaimed(tmp_path: Path) -> None:
    pid_max = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="ascii").strip())
    stale = {
        "pid": pid_max + 1,
        "start_ticks": 0,
        "boot_id": identity.current_identity()["boot_id"],
        "config_sha256": "old",
    }
    assert identity.is_live(stale) is False

    path = identity.identity_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stale), encoding="utf-8")
    claimed = identity.claim(tmp_path, "new")
    assert claimed["pid"] == os.getpid()
    assert claimed["config_sha256"] == "new"


def test_pid_reuse_with_a_different_generation_is_not_live() -> None:
    live = identity.current_identity()
    forged = {**live, "start_ticks": cast(int, live["start_ticks"]) + 1}
    assert identity.is_live(forged) is False


def test_corrupt_identity_refuses_takeover(tmp_path: Path) -> None:
    path = identity.identity_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        identity.claim(tmp_path, "sha")


def test_racing_claims_yield_a_single_owner(tmp_path: Path) -> None:
    results: list[dict[str, object] | RuntimeError] = []
    lock = threading.Lock()

    def attempt() -> None:
        try:
            with lock:
                results.append(identity.claim(tmp_path, "sha_race"))
        except RuntimeError as error:
            with lock:
                results.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    winners = [result for result in results if isinstance(result, dict)]
    assert len(winners) == 1
    assert all(result["pid"] == os.getpid() for result in winners)


def test_release_removes_only_the_matching_identity(tmp_path: Path) -> None:
    claimed = identity.claim(tmp_path, "sha_a")
    identity.release(tmp_path, {**claimed, "config_sha256": "sha_b"})
    assert identity.identity_path(tmp_path).exists()
    identity.release(tmp_path, claimed)
    assert not identity.identity_path(tmp_path).exists()


def _config_file(tmp_path: Path) -> Path:
    config = tmp_path / "researchd.json"
    config.write_text(
        json.dumps({
            "database": str(tmp_path / "researchd.db"),
            "artifact_root": str(tmp_path / "artifacts"),
            "state_root": str(tmp_path / "state"),
            "repositories": {},
            "job_commands": {},
            "host": "127.0.0.1",
            "port": 8788,
        }),
        encoding="utf-8",
    )
    return config


def test_stop_daemon_fails_closed_without_a_live_identity(tmp_path: Path) -> None:
    """Without a live strong identity no signal may go out; stop returns 1."""
    config = _config_file(tmp_path)
    state_root = tmp_path / "state"

    # No identity file at all.
    assert stop_daemon(config) == 1

    # A stale identity (dead PID) never authorizes a signal.
    state_root.mkdir(parents=True, exist_ok=True)
    pid_max = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="ascii").strip())
    stale = {
        "pid": pid_max + 1,
        "start_ticks": 0,
        "boot_id": identity.current_identity()["boot_id"],
        "config_sha256": "sha",
    }
    identity.identity_path(state_root).write_text(json.dumps(stale), encoding="utf-8")
    assert stop_daemon(config) == 1

    # A corrupt identity file fails closed as well.
    identity.identity_path(state_root).write_text("{corrupt", encoding="utf-8")
    assert stop_daemon(config) == 1
