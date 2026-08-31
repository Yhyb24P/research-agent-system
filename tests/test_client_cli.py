"""PX02-01/PX02-03: research client package and lifecycle CLI."""

import sys
import tomllib
from pathlib import Path

import pytest

from researchd.client.cli import build_parser, entrypoint, main


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

    def fake_interactive(config: Path) -> int:
        calls.append(("interactive", str(config)))
        return 0

    monkeypatch.setattr("researchd.client.cli.run_init", fake_init)
    monkeypatch.setattr("researchd.client.cli.run_status", fake_status)
    monkeypatch.setattr("researchd.client.cli.interactive_entry", fake_interactive)

    assert main(["--config", "cfg.json", "init"]) == 0
    assert main(["--config", "cfg.json", "status"]) == 0
    assert main(["--config", "cfg.json"]) == 0
    assert calls == [
        ("init", "cfg.json"),
        ("status", "cfg.json"),
        ("interactive", "cfg.json"),
    ]


def test_missing_config_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["init"])


def test_unknown_subcommand_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["--config", "cfg.json", "frobnicate"])


def test_entrypoint_requires_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["research"])
    with pytest.raises(SystemExit):
        entrypoint()
