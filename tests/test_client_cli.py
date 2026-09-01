"""PX02-01/PX02-03: research client package and lifecycle CLI."""

import sys
import tomllib
from pathlib import Path

import pytest

from researchd.client.cli import build_parser, entrypoint, main
from researchd.client.config_discovery import (
    default_config_path,
    resolve_config_path,
)


def test_console_script_is_declared_for_the_client_package() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle)

    assert project["project"]["scripts"]["research"] == "researchd.client.cli:entrypoint"


def test_main_dispatches_lifecycle_subcommands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_init(config: Path) -> int:
        calls.append(("init", str(config)))
        return 0

    def fake_status(config: Path) -> int:
        calls.append(("status", str(config)))
        return 0

    def fake_default(config: Path) -> int:
        calls.append(("default", str(config)))
        return 0

    monkeypatch.setattr("researchd.client.cli.run_init", fake_init)
    monkeypatch.setattr("researchd.client.cli.run_status", fake_status)
    monkeypatch.setattr("researchd.client.cli.default_entry", fake_default)

    assert main(["--config", "cfg.json", "init"]) == 0
    assert main(["--config", "cfg.json", "status"]) == 0
    assert main(["--config", "cfg.json"]) == 0
    assert calls == [
        ("init", "cfg.json"),
        ("status", "cfg.json"),
        ("default", "cfg.json"),
    ]


def test_config_discovery_precedence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    global_config = xdg / "research-agent-system" / "config.json"
    global_config.parent.mkdir(parents=True)
    global_config.write_text("{}", encoding="utf-8")

    environ = {
        "RESEARCH_CONFIG": str(tmp_path / "environment.json"),
        "XDG_CONFIG_HOME": str(xdg),
    }
    explicit = tmp_path / "explicit.json"

    assert resolve_config_path(explicit, environ, home) == explicit
    assert resolve_config_path(None, environ, home) == tmp_path / "environment.json"
    assert resolve_config_path(None, {"XDG_CONFIG_HOME": str(xdg)}, home) == global_config
    assert default_config_path({}, home) == home / ".config/research-agent-system/config.json"


def test_missing_config_prints_first_run_guidance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("researchd.client.cli.resolve_config_path", lambda value: None)

    assert main(["init"]) == 2
    assert "research setup" in capsys.readouterr().out


def test_missing_config_resolution_returns_none(tmp_path: Path) -> None:
    assert resolve_config_path(None, {}, tmp_path) is None


def test_unknown_subcommand_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["--config", "cfg.json", "frobnicate"])


def test_entrypoint_guides_first_run_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["research"])
    monkeypatch.setattr("researchd.client.cli.resolve_config_path", lambda value: None)
    monkeypatch.setattr("researchd.client.cli.run_setup", lambda: None)

    assert entrypoint() == 2


def test_setup_and_doctor_do_not_require_an_existing_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []

    def fake_setup(**kwargs: object) -> object:
        calls.append(("setup", kwargs))
        return object()

    def fake_doctor(config: Path | None) -> int:
        calls.append(("doctor", config))
        return 0

    monkeypatch.setattr("researchd.client.cli.run_setup", fake_setup)
    monkeypatch.setattr("researchd.client.cli.run_doctor", fake_doctor)

    assert main(["setup", "--project", str(tmp_path), "--yes", "--port", "9888"]) == 0
    assert main(["doctor"]) == 0
    assert calls[0][0] == "setup"
    assert calls[1] == ("doctor", None)
