"""Persist one RuntimeSession intent, then exit without performing its side effect."""

import argparse
import os
from pathlib import Path

from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.ids import AgentRuntimeId, RuntimeSessionId
from researchd.runtime_sessions.contracts import (
    ProcessLaunchSpec,
    RuntimeSessionStartCommand,
    RuntimeSessionStopCommand,
)
from researchd.runtime_sessions.service import RuntimeSessionService
from researchd.storage.db import create_sqlite_engine, session_factory


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("database", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--expected-version", type=int)
    parser.add_argument("--session-id", default="runtime_session_crash_test")
    args = parser.parse_args()
    sessions = session_factory(create_sqlite_engine(args.database))
    service = RuntimeSessionService(sessions, AgentRegistryService(sessions))
    if args.action == "start":
        service.begin_start(RuntimeSessionStartCommand(
            command_id="command_crash_start",
            runtime_session_id=RuntimeSessionId(args.session_id),
            runtime_id=AgentRuntimeId("runtime_crash_test"),
            actor_type="SYSTEM",
            actor_id="crash-fixture",
            launch_spec=ProcessLaunchSpec(
                argv=("/usr/bin/sleep", "60"),
                cwd=str(args.workspace),
            ),
        ))
    else:
        if args.expected_version is None:
            raise ValueError("stop requires expected version")
        service.begin_stop(RuntimeSessionStopCommand(
            command_id="command_crash_stop",
            runtime_session_id=RuntimeSessionId(args.session_id),
            runtime_id=AgentRuntimeId("runtime_crash_test"),
            actor_type="SYSTEM",
            actor_id="crash-fixture",
            expected_version=args.expected_version,
        ))
    os._exit(73)


if __name__ == "__main__":
    main()
