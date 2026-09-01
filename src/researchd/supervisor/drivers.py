"""Bounded side-effect drivers used by RuntimeSupervisor."""

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin

import httpx

from researchd.runtime_sessions.contracts import (
    ExternalObservation,
    LaunchMode,
    ProcessLaunchSpec,
    RemoteHttpAttachSpec,
)


class RuntimeDriver(Protocol):
    launch_mode: LaunchMode

    def start(self, launch_spec: dict[str, object]) -> dict[str, object]: ...

    def observe(self, external_identity: dict[str, object]) -> ExternalObservation: ...

    def stop(self, external_identity: dict[str, object]) -> ExternalObservation: ...


class ManagedProcessDriver:
    """Launch without a shell and identify a process across daemon restarts."""

    launch_mode = LaunchMode.PROCESS

    def __init__(self, *, stop_timeout_seconds: float = 2.0) -> None:
        if stop_timeout_seconds < 0 or stop_timeout_seconds > 30:
            raise ValueError("stop timeout must be between 0 and 30 seconds")
        self.stop_timeout_seconds = stop_timeout_seconds
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    def start(self, launch_spec: dict[str, object]) -> dict[str, object]:
        spec = ProcessLaunchSpec.model_validate(launch_spec)
        cwd = Path(spec.cwd)
        executable = Path(spec.argv[0])
        if not cwd.is_dir():
            raise ValueError("process cwd is not a directory")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("process executable is unavailable")
        process = subprocess.Popen(
            list(spec.argv),
            cwd=cwd,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            shell=False,
        )
        try:
            identity = self._identity(process.pid)
            self._children[process.pid] = process
            return identity
        except Exception:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            raise

    @staticmethod
    def _environment() -> dict[str, str]:
        """Return the fixed non-secret process environment.

        HOME is required by managed CLIs to locate their owner-only profile
        configuration.  It grants no additional OS access and, unlike copying
        API-key variables, does not disclose credentials to every PROCESS
        runtime.
        """
        home = Path.home().resolve(strict=True)
        if not home.is_dir():
            raise ValueError("process home is not a directory")
        return {"HOME": str(home), "LANG": "C.UTF-8", "PATH": os.defpath}

    def observe(self, external_identity: dict[str, object]) -> ExternalObservation:
        try:
            pid = self._identity_pid(external_identity)
            child = self._children.get(pid)
            if child is not None and child.poll() is not None:
                self._children.pop(pid, None)
                return ExternalObservation.ABSENT
            current = self._identity(pid)
        except FileNotFoundError:
            return ExternalObservation.ABSENT
        except (OSError, TypeError, ValueError):
            return ExternalObservation.UNKNOWN
        return (
            ExternalObservation.PRESENT
            if current == external_identity
            else ExternalObservation.ABSENT
        )

    def stop(self, external_identity: dict[str, object]) -> ExternalObservation:
        observation = self.observe(external_identity)
        if observation is not ExternalObservation.PRESENT:
            return observation
        pid = self._identity_pid(external_identity)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return ExternalObservation.ABSENT
        child = self._children.get(pid)
        if child is not None:
            try:
                child.wait(timeout=self.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                pass
            else:
                self._children.pop(pid, None)
                return ExternalObservation.ABSENT
        deadline = time.monotonic() + self.stop_timeout_seconds
        while time.monotonic() < deadline:
            if self.observe(external_identity) is ExternalObservation.ABSENT:
                return ExternalObservation.ABSENT
            time.sleep(0.05)
        return self.observe(external_identity)

    @staticmethod
    def _identity_pid(external_identity: dict[str, object]) -> int:
        pid = external_identity.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("process identity has an invalid pid")
        return pid

    @staticmethod
    def _identity(pid: int) -> dict[str, object]:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = stat.rfind(")")
        if closing < 0:
            raise ValueError("process stat is malformed")
        fields = stat[closing + 2 :].split()
        if len(fields) <= 19:
            raise ValueError("process stat is incomplete")
        return {"pid": pid, "start_ticks": int(fields[19]), "boot_id": boot_id}


class RemoteHttpDriver:
    """Attach to a typed remote runtime without persisting credentials."""

    launch_mode = LaunchMode.REMOTE_HTTP

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=5.0, follow_redirects=False)

    def start(self, launch_spec: dict[str, object]) -> dict[str, object]:
        spec = RemoteHttpAttachSpec.model_validate(launch_spec)
        instance_id = self._health(spec.endpoint, spec.health_path)
        return {
            "endpoint": spec.endpoint,
            "health_path": spec.health_path,
            "instance_id": instance_id,
        }

    def observe(self, external_identity: dict[str, object]) -> ExternalObservation:
        endpoint = external_identity.get("endpoint")
        health_path = external_identity.get("health_path")
        instance_id = external_identity.get("instance_id")
        if not all(isinstance(value, str) and value for value in (endpoint, health_path, instance_id)):
            return ExternalObservation.UNKNOWN
        try:
            observed = self._health(str(endpoint), str(health_path))
        except (httpx.HTTPError, ValueError):
            return ExternalObservation.UNKNOWN
        return (
            ExternalObservation.PRESENT
            if observed == instance_id
            else ExternalObservation.ABSENT
        )

    def stop(self, external_identity: dict[str, object]) -> ExternalObservation:
        # REMOTE_HTTP lifecycle is attach/detach. Stopping the local session
        # never invents authority to terminate the remote Agent runtime.
        del external_identity
        return ExternalObservation.ABSENT

    def _health(self, endpoint: str, health_path: str) -> str:
        response = self.client.get(urljoin(endpoint.rstrip("/") + "/", health_path.lstrip("/")))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("remote health response must be an object")
        instance_id = payload.get("runtime_instance_id")
        if not isinstance(instance_id, str) or not instance_id or len(instance_id) > 256:
            raise ValueError("remote health response has no typed runtime identity")
        return instance_id
