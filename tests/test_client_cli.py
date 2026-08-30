"""PX02-01: research client package and console-script scaffold."""

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


def test_entrypoint_prints_help_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["research"])
    assert main([]) == 0
    assert entrypoint() == 0
    output = capsys.readouterr().out
    assert "usage: research" in output


def test_parser_has_no_subcommands_yet() -> None:
    parser = build_parser()
    assert parser.parse_args([]) is not None

    with pytest.raises(SystemExit):
        parser.parse_args(["not-a-command-yet"])
