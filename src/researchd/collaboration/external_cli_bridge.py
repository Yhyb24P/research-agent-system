"""Bounded non-authoritative bridge for an installed external interactive CLI."""

import asyncio
from collections.abc import AsyncIterator

from pydantic import Field

from researchd.domain.base import DomainModel


class CliBridgeFrame(DomainModel):
    """Transcript data only; it is never a controller command or result."""

    direction: str = Field(pattern=r"^(stdin|stdout|stderr)$")
    data: str = Field(max_length=65_536)


class ExternalCliBridge:
    """Run a fixed argv without shell parsing or workflow-state authority."""

    def __init__(self, argv: tuple[str, ...], *, cwd: str, max_frame_bytes: int = 65_536) -> None:
        if not argv or any(not item or "\x00" in item for item in argv):
            raise ValueError("bridge argv must be nonempty and NUL-free")
        if max_frame_bytes <= 0:
            raise ValueError("bridge frame limit must be positive")
        self.argv, self.cwd, self.max_frame_bytes = argv, cwd, max_frame_bytes
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("bridge is already running")
        self._process = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

    async def send(self, data: str) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("bridge is not running")
        encoded = data.encode()
        if len(encoded) > self.max_frame_bytes:
            raise ValueError("bridge input exceeds frame limit")
        self._process.stdin.write(encoded)
        await self._process.stdin.drain()

    async def frames(self) -> AsyncIterator[CliBridgeFrame]:
        if self._process is None or self._process.stdout is None or self._process.stderr is None:
            raise RuntimeError("bridge is not running")
        readers = (("stdout", self._process.stdout), ("stderr", self._process.stderr))
        while self._process.returncode is None:
            pending = {
                asyncio.create_task(reader.read(self.max_frame_bytes)): direction
                for direction, reader in readers
            }
            done, unfinished = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED,
            )
            for task in unfinished:
                task.cancel()
            for task in done:
                data = task.result()
                if data:
                    yield CliBridgeFrame(
                        direction=pending[task], data=data.decode(errors="replace"),
                    )

    async def close(self) -> None:
        if self._process is None:
            return
        process, self._process = self._process, None
        if process.returncode is None:
            process.terminate()
        await process.wait()
