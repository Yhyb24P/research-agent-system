"""Non-secret strong identity for the local daemon process."""

import json
import os
from pathlib import Path


def identity_path(state_root: Path) -> Path:
    return state_root / "daemon.identity.json"


def current_identity(pid: int | None = None) -> dict[str, object]:
    process_id = os.getpid() if pid is None else pid
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    fields = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii").split()
    # /proc/<pid>/stat field 22 (index 21 after the pid/comm split) is the
    # start time in clock ticks since boot: a stable generation marker.  The
    # adjacent field 20 is the live thread count, which must never stand in
    # for it: a thread-pool change would make a live daemon look stale and
    # authorize a second owner.
    return {"pid": process_id, "start_ticks": int(fields[21]), "boot_id": boot_id}


def is_live(identity: dict[str, object]) -> bool:
    pid = identity.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        return current_identity(pid) == {
            "pid": pid,
            "start_ticks": identity.get("start_ticks"),
            "boot_id": identity.get("boot_id"),
        }
    except OSError:
        return False


def claim(state_root: Path, config_sha256: str) -> dict[str, object]:
    state_root.mkdir(parents=True, exist_ok=True)
    path = identity_path(state_root)
    payload = {**current_identity(), "config_sha256": config_sha256}
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RuntimeError("daemon identity is unreadable; refusing takeover") from None
        if isinstance(existing, dict) and is_live(existing):
            raise RuntimeError("researchd daemon identity is already live")
        path.unlink()
        return claim(state_root, config_sha256)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    return payload


def release(state_root: Path, identity: dict[str, object]) -> None:
    path = identity_path(state_root)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if existing == identity:
        path.unlink(missing_ok=True)


__all__ = ["claim", "current_identity", "identity_path", "is_live", "release"]
