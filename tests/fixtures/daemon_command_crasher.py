"""Exit after a generic daemon receipt is reserved but before its result."""

import asyncio
import os
import sys
from pathlib import Path

from researchd.daemon.command_service import DurableDaemonCommandService
from researchd.daemon.contracts import RunCancelCommand
from researchd.storage.db import create_sqlite_engine, session_factory


async def main(database: Path) -> None:
    def crash(command: object) -> object:
        del command
        os._exit(74)

    service = DurableDaemonCommandService(
        session_factory(create_sqlite_engine(database)),
        crash,
    )
    await service.execute(RunCancelCommand(
        command_id="command_generic_crash",
        actor_type="SYSTEM",
        actor_id="crash-fixture",
        run_id="run_unknown_outcome",
    ))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
