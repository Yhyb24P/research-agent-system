"""Executable lifecycle for the loopback researchd process."""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

from researchd.api.web import serve_local_control
from researchd.daemon.composition import DaemonConfig, compose_daemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="researchd")
    parser.add_argument("--database", type=Path, default=Path("researchd.db"))
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--state-root", type=Path, default=Path(".researchd"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="create a fresh database at the current schema")
    serve = subparsers.add_parser("serve", help="start the loopback control daemon")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8788)
    return parser


def _alembic_config(database: Path) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.resolve()}")
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database: Path = args.database
    if args.command == "init":
        if database.exists():
            raise SystemExit(f"refusing to initialize existing database: {database}")
        database.parent.mkdir(parents=True, exist_ok=True)
        command.upgrade(_alembic_config(database), "head")
        args.artifact_root.mkdir(parents=True, exist_ok=True)
        return 0

    config = DaemonConfig(
        database=database,
        artifact_root=args.artifact_root,
        state_root=args.state_root,
        host=args.host,
        port=args.port,
    )
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
