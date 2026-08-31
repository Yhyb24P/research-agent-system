import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from researchd.daemon.composition import DaemonConfig, compose_daemon
from researchd.daemon.cli import main
from researchd.daemon.security import ControlCredentialError


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


def test_init_creates_non_secret_owner_only_control_credential(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _payload(tmp_path)
    config_path = tmp_path / "researchd.json"
    config_path.write_text(json.dumps(payload))

    assert main(["--config", str(config_path), "init"]) == 0
    token_path = tmp_path / "state" / "control.token"
    token = token_path.read_text(encoding="ascii").strip()
    assert len(token) == 64
    assert token_path.stat().st_mode & 0o777 == 0o600

    assert main(["--config", str(config_path), "inspect"]) == 0
    inspected = capsys.readouterr().out
    assert token not in inspected
    assert token not in (tmp_path / "researchd.db").read_bytes().decode(
        "latin-1", errors="ignore"
    )


def test_serve_rejects_unsafe_control_credential_permissions(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    config_path = tmp_path / "researchd.json"
    config_path.write_text(json.dumps(payload))
    assert main(["--config", str(config_path), "init"]) == 0
    (tmp_path / "state" / "control.token").chmod(0o644)

    with pytest.raises(ControlCredentialError, match="permissions must be 0600"):
        main(["--config", str(config_path), "serve"])


def test_validate_and_inspect_do_not_touch_state_or_expose_arguments(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _payload(tmp_path)
    payload["job_commands"] = {
        "typed-check": {"argv": ["/usr/bin/printf", "sensitive-fixed-argument"]},
    }
    config_path = tmp_path / "researchd.json"
    config_path.write_text(json.dumps(payload))

    assert main(["--config", str(config_path), "validate"]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated == {"config_sha256": _config(payload).sha256(), "valid": True}
    assert not (tmp_path / "researchd.db").exists()
    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / "state").exists()

    assert main(["--config", str(config_path), "inspect"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["job_commands"] == {
        "typed-check": {"argument_count": 1, "executable": "/usr/bin/printf"},
    }
    assert "sensitive-fixed-argument" not in json.dumps(inspected)
    assert inspected["config_sha256"] == validated["config_sha256"]


def test_composition_rejects_missing_repository(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    payload["repositories"] = {"source": str(tmp_path / "missing")}

    with pytest.raises(FileNotFoundError):
        compose_daemon(_config(payload))
