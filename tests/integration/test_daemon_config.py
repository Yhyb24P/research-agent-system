import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from researchd.daemon.composition import DaemonConfig, compose_daemon
from researchd.daemon.cli import main


def _payload(tmp_path: Path) -> dict[str, object]:
    return {
        "database": str(tmp_path / "researchd.db"),
        "artifact_root": str(tmp_path / "artifacts"),
        "state_root": str(tmp_path / "state"),
        "repositories": {},
        "job_commands": {
            "typed-check": {"argv": ["/usr/bin/true", "--fixed-argument"]},
        },
        "host": "127.0.0.1",
        "port": 8788,
    }


def _config(payload: dict[str, object]) -> DaemonConfig:
    return DaemonConfig.model_validate_json(json.dumps(payload))


def test_config_rejects_shell_commands_relative_paths_and_non_loopback(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["job_commands"] = {"typed-check": {"argv": ["true && id"]}}
    with pytest.raises(ValidationError, match="absolute path"):
        _config(payload)

    payload = _payload(tmp_path)
    payload["database"] = "researchd.db"
    with pytest.raises(ValidationError, match="absolute"):
        _config(payload)

    payload = _payload(tmp_path)
    payload["host"] = "0.0.0.0"
    with pytest.raises(ValidationError, match="loopback"):
        _config(payload)


def test_config_rejects_unknown_fields_and_invalid_identifiers(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["legacy_database"] = "old.db"
    with pytest.raises(ValidationError, match="Extra inputs"):
        _config(payload)

    payload = _payload(tmp_path)
    payload["job_commands"] = {"bad job": {"argv": ["/usr/bin/true"]}}
    with pytest.raises(ValidationError, match="job type"):
        _config(payload)


def test_init_refuses_existing_database_instead_of_upgrading(tmp_path: Path) -> None:
    config_path = tmp_path / "researchd.json"
    config_path.write_text(json.dumps(_payload(tmp_path)))
    database = tmp_path / "researchd.db"
    database.write_bytes(b"not a current database")

    with pytest.raises(SystemExit, match="refusing to initialize existing database"):
        main(["--config", str(config_path), "init"])
    assert database.read_bytes() == b"not a current database"


def test_composition_rejects_missing_repository(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["repositories"] = {"source": str(tmp_path / "missing")}

    with pytest.raises(FileNotFoundError):
        compose_daemon(_config(payload))
