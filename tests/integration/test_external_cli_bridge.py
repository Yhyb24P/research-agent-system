"""PX08: bounded external CLI bridge acceptance matrix.

Covers the frozen acceptance items from 推进计划.md §B for
``researchd.collaboration.external_cli_bridge``:
1. empty argv, NUL argv items and non-positive frame limits are rejected;
   the fixed argv is executed via exec, never through a shell;
2. input and stdout/stderr are bounded by ``max_frame_bytes``; oversized
   input is rejected; frames keep their direction and carry no authority;
3. start is one-shot, send/frames before start are rejected, close is
   idempotent and leaves no child process behind;
4. the bridge transcript cannot produce or change ResearchRun, WorkOrder,
   Delegation, Invocation, Artifact, capability-grant, Verification or
   AuditEvent state.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.external_cli_bridge import CliBridgeFrame, ExternalCliBridge
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentInvocationRecord,
    ArtifactRecord,
    AuditEventRecord,
    DelegationRecord,
    ResearchRunRecord,
    VerificationResultRecord,
    WorkOrderRecord,
    WorkspaceGrantRecord,
)
from tests.integration.test_storage import migrate

AUTHORITY_MODELS = (
    ResearchRunRecord,
    WorkOrderRecord,
    DelegationRecord,
    AgentInvocationRecord,
    ArtifactRecord,
    WorkspaceGrantRecord,
    VerificationResultRecord,
    AuditEventRecord,
)


@pytest.mark.parametrize(("argv", "limit", "match"), [
    ((), 65_536, "nonempty"),
    (("",), 65_536, "nonempty"),
    (("echo", "hi\x00"), 65_536, "NUL-free"),
    (("echo", "hi"), 0, "positive"),
    (("echo", "hi"), -1, "positive"),
])


def test_bridge_rejects_invalid_configuration(
    argv: tuple[str, ...], limit: int, match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        ExternalCliBridge(argv, cwd="/tmp", max_frame_bytes=limit)


def test_bridge_runs_fixed_argv_without_shell_interpretation(tmp_path: Path) -> None:
    marker = "$(touch PWNED)"
    script = "import sys; sys.stdout.write('literal:' + sys.argv[1]); sys.stdout.flush()"

    async def run() -> list[CliBridgeFrame]:
        bridge = ExternalCliBridge(
            (sys.executable, "-c", script, marker),
            cwd=str(tmp_path), max_frame_bytes=65_536,
        )
        await bridge.start()
        frames = [frame async for frame in bridge.frames()]
        await bridge.close()
        return frames

    frames = asyncio.run(run())
    stdout = "".join(frame.data for frame in frames if frame.direction == "stdout")

    assert f"literal:{marker}" in stdout
    assert not (tmp_path / "PWNED").exists()


def test_frames_keep_direction_and_respect_the_frame_limit(tmp_path: Path) -> None:
    chunk = "A" * 100
    script = f"import sys; sys.stdout.write('{chunk}' * 5); sys.stderr.write('B' * 50)"

    async def run() -> list[CliBridgeFrame]:
        bridge = ExternalCliBridge(
            (sys.executable, "-c", script), cwd=str(tmp_path), max_frame_bytes=64,
        )
        await bridge.start()
        frames = [frame async for frame in bridge.frames()]
        await bridge.close()
        return frames

    frames = asyncio.run(run())
    stdout = "".join(frame.data for frame in frames if frame.direction == "stdout")
    stderr = "".join(frame.data for frame in frames if frame.direction == "stderr")

    assert stdout == "A" * 500
    assert stderr == "B" * 50
    assert frames
    assert all(frame.direction in {"stdout", "stderr"} for frame in frames)
    assert all(len(frame.data.encode()) <= 64 for frame in frames)


def test_input_over_the_frame_limit_is_rejected(tmp_path: Path) -> None:
    script = "import sys; sys.stdout.write(f'got:{len(sys.stdin.read())}')"

    async def run() -> str:
        bridge = ExternalCliBridge(
            (sys.executable, "-c", script), cwd=str(tmp_path), max_frame_bytes=16,
        )
        await bridge.start()
        await bridge.send("hello")
        with pytest.raises(ValueError, match="frame limit"):
            await bridge.send("x" * 17)
        assert bridge._process is not None and bridge._process.stdin is not None
        bridge._process.stdin.close()
        frames = [frame async for frame in bridge.frames()]
        await bridge.close()
        return "".join(frame.data for frame in frames if frame.direction == "stdout")

    assert asyncio.run(run()) == "got:5"


def test_start_once_guards_and_close_leaves_no_child(tmp_path: Path) -> None:
    async def run() -> int:
        bridge = ExternalCliBridge(("sleep", "30"), cwd=str(tmp_path))
        with pytest.raises(RuntimeError, match="not running"):
            await bridge.send("x")
        generator = bridge.frames()
        with pytest.raises(RuntimeError, match="not running"):
            await generator.__anext__()
        await bridge.start()
        assert bridge._process is not None
        pid = bridge._process.pid
        with pytest.raises(RuntimeError, match="already running"):
            await bridge.start()
        await bridge.close()
        await bridge.close()
        assert bridge._process is None
        return pid

    pid = asyncio.run(run())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_frame_model_is_transcript_only() -> None:
    assert set(CliBridgeFrame.model_fields) == {"direction", "data"}
    assert CliBridgeFrame.model_config.get("extra") == "forbid"

    with pytest.raises(ValidationError):
        CliBridgeFrame.model_validate({"direction": "stdout", "data": "x", "command": "rm -rf /"})
    with pytest.raises(ValidationError):
        CliBridgeFrame(direction="websocket", data="x")


def test_bridge_transcript_cannot_touch_authority_state(tmp_path: Path) -> None:
    database = tmp_path / "bridge.db"
    migrate(database)
    sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))

    def counts() -> list[int]:
        with sessions() as session:
            return [
                int(session.scalar(select(func.count()).select_from(model)))
                for model in AUTHORITY_MODELS
            ]

    before = counts()

    async def run() -> None:
        bridge = ExternalCliBridge(
            (sys.executable, "-c", "import sys; print('transcript only')"),
            cwd=str(tmp_path),
        )
        await bridge.start()
        await bridge.send("ping\n")
        _ = [frame async for frame in bridge.frames()]
        await bridge.close()

    asyncio.run(run())
    assert counts() == before
