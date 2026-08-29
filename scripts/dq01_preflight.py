#!/usr/bin/env python3
"""Run non-destructive host checks required before DQ01 sandbox testing."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def command_output(*argv: str) -> str | None:
    try:
        return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def source_commit() -> str | None:
    return command_output("git", "-C", str(ROOT), "rev-parse", "HEAD")


def collect(*, target: Path | None = None) -> dict[str, Any]:
    inspected = (target or Path.cwd()).resolve(strict=True)
    executable = shutil.which("bwrap")
    mode: str | None = None
    file_capabilities: str | None = None
    if executable:
        mode = stat.filemode(os.stat(executable).st_mode)
        file_capabilities = command_output("getcap", executable)
    userns_path = Path("/proc/sys/user/max_user_namespaces")
    userns_value = userns_path.read_text().strip() if userns_path.is_file() else None
    cgroup = command_output("stat", "-fc", "%T", "/sys/fs/cgroup")
    mount = command_output(
        "findmnt", "-n", "-T", str(inspected), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"
    )
    release = platform.release().lower()
    proc_version = Path("/proc/version").read_text(encoding="utf-8").lower()
    container = (
        "docker" if Path("/.dockerenv").exists()
        else "podman" if Path("/run/.containerenv").exists()
        else None
    )
    return {
        "release_commit": source_commit(),
        "host": {
            "os": platform.platform(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "git": command_output("git", "--version"),
            "topology": "wsl" if "microsoft" in release or "microsoft" in proc_version else "linux",
            "container": container,
        },
        "filesystem": {
            "target": str(inspected),
            "findmnt": mount,
            "stat_type": command_output("stat", "-fc", "%T", str(inspected)),
        },
        "bubblewrap": {
            "path": executable,
            "version": command_output(executable, "--version") if executable else None,
            "mode": mode,
            "file_capabilities": file_capabilities or "none-or-unavailable",
            "setuid": bool(mode and mode[3] == "s"),
        },
        "namespaces": {
            "max_user_namespaces": userns_value,
            "cgroup_filesystem": cgroup,
        },
    }


def failures(report: dict[str, Any]) -> list[str]:
    bubblewrap = report["bubblewrap"]
    namespaces = report["namespaces"]
    problems: list[str] = []
    if not bubblewrap["path"]:
        problems.append("bubblewrap executable is unavailable")
    if bubblewrap["setuid"]:
        problems.append("bubblewrap has setuid mode")
    if bubblewrap["file_capabilities"] not in {"none-or-unavailable", ""}:
        problems.append("bubblewrap has unexpected file capabilities")
    try:
        if int(namespaces["max_user_namespaces"] or "0") <= 0:
            problems.append("user namespaces are disabled")
    except ValueError:
        problems.append("user namespace limit is unreadable")
    if namespaces["cgroup_filesystem"] != "cgroup2fs":
        problems.append("cgroup v2 is not detected")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit non-zero when a required preflight check fails")
    parser.add_argument("--target", type=Path, help="deployment filesystem path to fingerprint")
    parser.add_argument("--output", type=Path, help="also write the JSON report to this path")
    args = parser.parse_args()
    report = collect(target=args.target)
    report["failures"] = failures(report)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(rendered, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 1 if args.strict and report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
