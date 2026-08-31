"""PH05: the installed artifact ships its own migration chain.

``researchd init`` and ``researchd migrate`` must run the migrations packaged
inside the distribution, never a source checkout's alembic.ini.
"""

import json
from importlib.resources import files
from pathlib import Path

import pytest

import researchd.daemon.cli as daemon_cli
from researchd.daemon.cli import _alembic_config


def _cli_config(tmp_path: Path, database: Path) -> Path:
    config = tmp_path / "researchd.json"
    config.write_text(
        json.dumps({
            "database": str(database),
            "artifact_root": str(tmp_path / "artifacts"),
            "state_root": str(tmp_path / "state"),
            "repositories": {},
            "job_commands": {},
            "host": "127.0.0.1",
            "port": 8788,
        }),
        encoding="utf-8",
    )
    return config


def test_researchd_migrate_refuses_a_missing_database(tmp_path: Path) -> None:
    config = _cli_config(tmp_path, tmp_path / "absent.db")
    with pytest.raises(SystemExit, match="refusing to migrate missing database"):
        daemon_cli.main(["--config", str(config), "migrate"])


def test_researchd_migrate_refuses_a_live_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "present.db"
    database.touch()
    config = _cli_config(tmp_path, database)
    monkeypatch.setattr(daemon_cli, "_daemon_is_running", lambda config: True)
    with pytest.raises(SystemExit, match="refusing migration while researchd is running"):
        daemon_cli.main(["--config", str(config), "migrate"])


def test_migration_resources_are_packaged_with_the_distribution() -> None:
    migrations = files("researchd.storage").joinpath("migrations")
    assert migrations.joinpath("env.py").is_file()
    assert migrations.joinpath("script.py.mako").is_file()
    versions = sorted(
        entry.name
        for entry in migrations.joinpath("versions").iterdir()
        if entry.name.endswith(".py") and entry.name != "__init__.py"
    )
    assert versions[0].startswith("0001")
    assert versions[-1].startswith("0025")
    assert len(versions) == 25


def test_researchd_cli_uses_packaged_migrations_not_the_checkout() -> None:
    config = _alembic_config(Path("/tmp/ph05/researchd.db"))
    location = config.get_main_option("script_location")
    assert location == str(files("researchd.storage").joinpath("migrations"))
    assert "alembic.ini" not in location
    assert config.get_main_option("sqlalchemy.url") == "sqlite:////tmp/ph05/researchd.db"
