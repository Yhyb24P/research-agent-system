"""Executable lifecycle for the loopback researchd process."""

import argparse
import json
import os
from pathlib import Path
from importlib.resources import files
import shutil
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from alembic import command
from alembic.config import Config

from researchd.api.web import serve_local_control
from researchd.collaboration.agent_definitions import AgentDefinition
from researchd.collaboration.install import AgentInstallService
from researchd.collaboration.registry import AgentRegistryService
from researchd.daemon.composition import DaemonConfig, compose_daemon
from researchd.daemon.security import (
    control_token_path,
    create_control_token,
    load_control_token,
)
from researchd.domain.ids import AgentId
from researchd.daemon.identity import claim as claim_daemon, release as release_daemon
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.db import create_sqlite_engine, session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="researchd")
    parser.add_argument("--config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate configuration without touching state")
    subparsers.add_parser("inspect", help="print a non-secret configuration projection")
    subparsers.add_parser("init", help="create a fresh database at the current schema")
    subparsers.add_parser("migrate", help="backup and explicitly upgrade an existing database")
    install = subparsers.add_parser("install-agent", help="install a trusted AgentDefinition locally")
    install.add_argument("definition", type=Path)
    remove = subparsers.add_parser("remove-agent", help="disable an installed Agent locally")
    remove.add_argument("agent_id")
    serve = subparsers.add_parser("serve", help="start the loopback control daemon")
    return parser


def _alembic_config(database: Path) -> Config:
    """Use packaged migration resources; never infer a source checkout root."""
    config = Config()
    config.set_main_option(
        "script_location",
        str(files("researchd.storage").joinpath("migrations")),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.resolve()}")
    return config


def _daemon_is_running(config: DaemonConfig) -> bool:
    """Health is deliberately public; any HTTP response means the port is owned."""
    host = f"[{config.host}]" if config.host == "::1" else config.host
    try:
        with urlopen(f"http://{host}:{config.port}/api/health", timeout=0.5):
            return True
    except HTTPError:
        return True
    except (URLError, TimeoutError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = DaemonConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
    except OSError as error:
        raise SystemExit(f"cannot read researchd config: {args.config}") from error
    database = config.database
    if args.command == "validate":
        print(json.dumps({"config_sha256": config.sha256(), "valid": True}, sort_keys=True))
        return 0
    if args.command == "inspect":
        print(json.dumps(config.inspection(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "init":
        if database.exists():
            raise SystemExit(f"refusing to initialize existing database: {database}")
        credential_path = control_token_path(config.state_root)
        if os.path.lexists(credential_path):
            raise SystemExit(f"refusing to replace existing control credential: {credential_path}")
        database.parent.mkdir(parents=True, exist_ok=True)
        command.upgrade(_alembic_config(database), "head")
        config.artifact_root.mkdir(parents=True, exist_ok=True)
        create_control_token(config.state_root)
        return 0

    if args.command == "migrate":
        if not database.exists():
            raise SystemExit(f"refusing to migrate missing database: {database}")
        if _daemon_is_running(config):
            raise SystemExit("refusing migration while researchd is running")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = database.with_name(f"{database.name}.{stamp}.pre-migrate.bak")
        shutil.copy2(database, backup)
        command.upgrade(_alembic_config(database), "head")
        print(json.dumps({"database": str(database), "backup": str(backup), "revision": "head"}, sort_keys=True))
        return 0

    if args.command in {"install-agent", "remove-agent"}:
        if _daemon_is_running(config):
            action = "AgentDefinition install" if args.command == "install-agent" else "Agent removal"
            raise SystemExit(f"refusing {action} while researchd is running")
        sessions = session_factory(create_sqlite_engine(database))
        registry = AgentRegistryService(sessions)
        installer = AgentInstallService(
            sessions,
            registry,
            RuntimeLaunchProfileService(sessions, registry),
        )
        if args.command == "remove-agent":
            try:
                removal = installer.disable(AgentId(args.agent_id))
            except ValueError as error:
                raise SystemExit(str(error)) from error
            print(json.dumps(removal.model_dump(mode="json"), sort_keys=True))
            return 0
        try:
            definition = AgentDefinition.model_validate_json(
                args.definition.read_text(encoding="utf-8"),
            )
        except OSError as error:
            raise SystemExit(f"cannot read AgentDefinition: {args.definition}") from error
        installation = installer.install(definition)
        print(json.dumps(installation.model_dump(mode="json"), sort_keys=True))
        return 0

    control_token = load_control_token(config.state_root)
    daemon_identity = claim_daemon(config.state_root, config.sha256())
    application = compose_daemon(config)
    application.daemon.start()
    server = serve_local_control(
        application.api,
        daemon=application.daemon,
        resolution=application.resolution,
        host=config.host,
        port=config.port,
        control_token=control_token,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        application.daemon.stop()
        release_daemon(config.state_root, daemon_identity)
    return 0


def entrypoint() -> int:
    return main()


__all__ = ["build_parser", "entrypoint", "main"]


if __name__ == "__main__":
    raise SystemExit(entrypoint())
