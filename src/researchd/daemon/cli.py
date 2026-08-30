"""Executable lifecycle for the loopback researchd process."""

import argparse
import json
from pathlib import Path

from alembic import command
from alembic.config import Config

from researchd.api.web import serve_local_control
from researchd.daemon.composition import DaemonConfig, compose_daemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="researchd")
    parser.add_argument("--config", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate configuration without touching state")
    subparsers.add_parser("inspect", help="print a non-secret configuration projection")
    subparsers.add_parser("init", help="create a fresh database at the current schema")
    serve = subparsers.add_parser("serve", help="start the loopback control daemon")
    return parser


def _alembic_config(database: Path) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.resolve()}")
    return config


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
        database.parent.mkdir(parents=True, exist_ok=True)
        command.upgrade(_alembic_config(database), "head")
        config.artifact_root.mkdir(parents=True, exist_ok=True)
        config.state_root.mkdir(parents=True, exist_ok=True)
        return 0

    application = compose_daemon(config)
    report = application.daemon.start()
    if not report.ready:
        raise SystemExit("researchd startup barrier failed")
    server = serve_local_control(
        application.api,
        daemon=application.daemon,
        host=config.host,
        port=config.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        application.daemon.stop()
    return 0


def entrypoint() -> int:
    return main()


__all__ = ["build_parser", "entrypoint", "main"]


if __name__ == "__main__":
    raise SystemExit(entrypoint())
