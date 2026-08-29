import os
import selectors
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Protocol

from researchd.domain.enums import NetworkMode
from researchd.executor.contracts import CommandResult, CommandSpec, SandboxMount, SandboxSpec


class SandboxUnavailable(RuntimeError):
    pass


class UnsupportedSandboxPolicy(ValueError):
    pass


class SandboxBackend(Protocol):
    def run(self, sandbox: SandboxSpec, command: CommandSpec) -> CommandResult: ...
    def cancel(self, execution_id: str) -> bool: ...


class BubblewrapBackend:
    """Linux sandbox with a minimal filesystem and isolated network/PID namespaces."""

    forbidden_environment_fragments = ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL")

    def __init__(self, executable: str = "bwrap") -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise SandboxUnavailable("bubblewrap executable not found")
        self.executable = resolved
        self._active: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def run(self, sandbox: SandboxSpec, command: CommandSpec) -> CommandResult:
        if sandbox.network is not NetworkMode.NONE:
            raise UnsupportedSandboxPolicy("BubblewrapBackend currently promises network=none only")
        argv = self._build_argv(sandbox, command)
        execution_id = command.execution_id
        started = time.monotonic()
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, close_fds=True,
        )
        with self._lock:
            self._active[execution_id] = process
        stdout, stderr, timed_out, output_exceeded = self._collect(process, command, started)
        with self._lock:
            self._active.pop(execution_id, None)
            cancelled = execution_id in self._cancelled
            self._cancelled.discard(execution_id)
        return CommandResult(
            execution_id=execution_id, exit_code=process.returncode,
            stdout=stdout, stderr=stderr, timed_out=timed_out, cancelled=cancelled,
            output_limit_exceeded=output_exceeded, duration_seconds=time.monotonic() - started,
        )

    def cancel(self, execution_id: str) -> bool:
        with self._lock:
            process = self._active.get(execution_id)
            if process is None:
                return False
            self._cancelled.add(execution_id)
        self._terminate_group(process, 0.2)
        return True

    def _build_argv(self, sandbox: SandboxSpec, command: CommandSpec) -> list[str]:
        workspace = Path(sandbox.workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise SandboxUnavailable("workspace is not a directory")
        cwd = PurePosixPath(command.cwd)
        if not cwd.is_absolute() or not cwd.is_relative_to(PurePosixPath("/workspace")):
            raise ValueError("sandbox cwd must be inside /workspace")
        argv = [
            "/usr/bin/prlimit",
            f"--cpu={command.limits.cpu_seconds}",
            f"--as={command.limits.memory_mb * 1024 * 1024}",
            f"--fsize={command.limits.file_size_mb * 1024 * 1024}",
            "--",
            self.executable,
            "--unshare-user", "--unshare-pid", "--unshare-net",
            "--die-with-parent", "--new-session", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/runtime/bin",
            "--setenv", "HOME", "/nonexistent",
            "--setenv", "TMPDIR", "/tmp",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
        ]
        if Path("/lib64").exists():
            argv += ["--ro-bind", "/lib64", "/lib64"]
        argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(workspace), "/workspace"]
        targets = {"/workspace", "/usr", "/lib", "/lib64", "/proc", "/dev", "/tmp"}
        mounts = list(sandbox.mounts)
        mounts.extend(self._interpreter_runtime_mounts(sandbox, command))
        for mount in mounts:
            source = Path(mount.source).resolve(strict=True)
            target = PurePosixPath(mount.target)
            if not target.is_absolute() or ".." in target.parts or str(target) in targets:
                raise ValueError("invalid or duplicate sandbox mount target")
            targets.add(str(target))
            argv += ["--ro-bind" if mount.read_only else "--bind", str(source), str(target)]
        for name, value in sorted(sandbox.environment.items()):
            if not name.isidentifier() or any(fragment in name.upper() for fragment in self.forbidden_environment_fragments):
                raise ValueError(f"environment variable is not allowlisted: {name}")
            argv += ["--setenv", name, value]
        argv += ["--chdir", str(cwd), "--", *command.argv]
        return argv

    @staticmethod
    def _interpreter_runtime_mounts(sandbox: SandboxSpec, command: CommandSpec) -> tuple[SandboxMount, ...]:
        """Expose a symlinked runtime interpreter's standard library.

        The venv is projected into ``/runtime``.  CI and many host venvs use
        a symlink for ``bin/python`` whose target lives outside that mount;
        without the target's installation root Python starts but cannot find
        its standard library.  Only the resolved interpreter distribution is
        mounted, read-only, and only when it is not already covered by the
        base filesystem mounts.
        """
        if not command.argv or not command.argv[0].startswith("/runtime/"):
            return ()
        runtime_mount = next((mount for mount in sandbox.mounts if mount.target == "/runtime"), None)
        if runtime_mount is None:
            return ()
        relative = PurePosixPath(command.argv[0]).relative_to(PurePosixPath("/runtime"))
        candidate = Path(runtime_mount.source) / Path(relative)
        if not candidate.is_symlink():
            return ()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return ()
        if not resolved.is_file():
            return ()
        root = next((parent for parent in (resolved.parent, *resolved.parents) if (parent / "lib").is_dir()), None)
        if root is None or root == Path("/") or root.is_relative_to(Path("/usr")):
            return ()
        return (SandboxMount(source=str(root), target=str(root), read_only=True),)

    def _collect(self, process: subprocess.Popen[bytes], command: CommandSpec, started: float) -> tuple[bytes, bytes, bool, bool]:
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        captured = 0
        timed_out = False
        exceeded = False
        terminated = False
        while selector.get_map():
            if not terminated and time.monotonic() - started >= command.limits.wall_seconds:
                timed_out = True
                terminated = True
                self._terminate_group(process, command.limits.terminate_grace_seconds)
            for key, _ in selector.select(timeout=0.05):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                remaining = max(0, command.limits.output_bytes - captured)
                if remaining:
                    chunks[key.data].append(data[:remaining])
                    captured += min(len(data), remaining)
                if len(data) > remaining and not terminated:
                    exceeded = True
                    terminated = True
                    self._terminate_group(process, command.limits.terminate_grace_seconds)
        process.wait()
        return b"".join(chunks["stdout"]), b"".join(chunks["stderr"]), timed_out, exceeded

    @staticmethod
    def _terminate_group(process: subprocess.Popen[bytes], grace: float) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
