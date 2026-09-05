"""Developer Preview first-run setup and read-only doctor coverage."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from researchd.client.bootstrap import doctor_report, run_setup, write_new_config


def _git_project(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def test_setup_creates_owner_only_global_config_and_delegates_init(
    tmp_path: Path,
) -> None:
    project = _git_project(tmp_path / "project")
    home = tmp_path / "home"
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    result = run_setup(
        project=project,
        assume_yes=True,
        environ={},
        home=home,
        cwd=project,
        run_fn=fake_run,
        initialize_workspace_fn=lambda path: None,
        print_fn=lambda message: None,
    )

    assert result is not None
    assert result.config_path == home / ".config/research-agent-system/config.json"
    assert result.config_path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(result.config_path.read_text(encoding="utf-8"))
    assert payload["repositories"] == {"project": str(project)}
    assert payload["workspace_sources"]["workspace_local"]["root"] == str(project)
    assert [call[-1] for call in calls] == ["validate", "init"]


def test_setup_refuses_existing_state_without_replacing_it(tmp_path: Path) -> None:
    project = _git_project(tmp_path / "project")
    home = tmp_path / "home"
    database = home / ".local/share/research-agent-system/researchd.db"
    database.parent.mkdir(parents=True)
    database.write_text("existing", encoding="utf-8")

    result = run_setup(
        project=project,
        assume_yes=True,
        environ={},
        home=home,
        cwd=project,
        print_fn=lambda message: None,
    )

    assert result is None
    assert database.read_text(encoding="utf-8") == "existing"
    assert not (home / ".config/research-agent-system/config.json").exists()


def test_write_new_config_never_replaces_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    write_new_config(path, {"first": True})

    try:
        write_new_config(path, {"second": True})
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing trusted config was replaced")
    assert json.loads(path.read_text(encoding="utf-8")) == {"first": True}


def test_doctor_reports_missing_config_without_touching_state(tmp_path: Path) -> None:
    report, usable = doctor_report(None, environ={}, home=tmp_path)

    assert usable is False
    assert report["config_present"] is False
    assert report["guidance"] == "run `research setup`"
    assert list(tmp_path.iterdir()) == []


def test_doctor_accepts_initialized_owner_only_state(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    database = tmp_path / "researchd.db"
    artifacts = tmp_path / "artifacts"
    database.write_bytes(b"database marker")
    state.mkdir()
    token = state / "control.token"
    token.write_text("f" * 64, encoding="ascii")
    os.chmod(token, 0o600)
    write_new_config(config, {
        "database": str(database),
        "artifact_root": str(artifacts),
        "state_root": str(state),
        "repositories": {},
        "workspace_sources": {},
        "job_commands": {},
        "host": "127.0.0.1",
        "port": 0,
    })

    report, usable = doctor_report(config, environ={}, home=tmp_path)

    assert usable is True
    assert report["config_valid"] is True
    assert report["control_credential_owner_only"] is True
    assert report["daemon"]["reachable"] is False


def test_explicit_setup_isolates_state_inside_the_deployment_root(tmp_path: Path) -> None:
    project = _git_project(tmp_path / "project")
    home = tmp_path / "home"
    deployment = tmp_path / "deployment"

    result = run_setup(
        project=project,
        config_path=deployment / "researchd.json",
        assume_yes=True,
        environ={},
        home=home,
        cwd=project,
        initialize_workspace_fn=lambda path: None,
        print_fn=lambda message: None,
    )

    assert result is not None
    assert result.config_path == deployment / "researchd.json"
    payload = json.loads(result.config_path.read_text(encoding="utf-8"))
    assert payload["database"] == str(deployment / "data" / "researchd.db")
    assert payload["state_root"] == str(deployment / "state")
    assert (deployment / "data" / "researchd.db").is_file()
    assert (deployment / "state" / "control.token").is_file()
    # An isolated deployment profile must never fall back to the caller's
    # global XDG directories.
    assert not (home / ".local/share/research-agent-system").exists()
    assert not (home / ".local/state/research-agent-system").exists()


def test_explicit_setup_layout_matches_the_deployment_convention(tmp_path: Path) -> None:
    project = _git_project(tmp_path / "project")
    home = tmp_path / "home"
    deployment = tmp_path / "deployment"

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0)

    result = run_setup(
        project=project,
        config_path=deployment / "researchd.json",
        assume_yes=True,
        environ={},
        home=home,
        cwd=project,
        run_fn=fake_run,
        initialize_workspace_fn=lambda path: None,
        print_fn=lambda message: None,
    )

    assert result is not None
    payload = json.loads(result.config_path.read_text(encoding="utf-8"))
    # Deployment convention (DQ01 layout): researchd.json + data/ + artifacts/
    # + state/ as siblings under the deployment root.
    assert payload["artifact_root"] == str(deployment / "artifacts")


def test_default_setup_still_uses_trusted_xdg_paths(tmp_path: Path) -> None:
    project = _git_project(tmp_path / "project")
    home = tmp_path / "home"

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0)

    result = run_setup(
        project=project,
        assume_yes=True,
        environ={},
        home=home,
        cwd=project,
        run_fn=fake_run,
        initialize_workspace_fn=lambda path: None,
        print_fn=lambda message: None,
    )

    assert result is not None
    assert result.config_path == home / ".config/research-agent-system/config.json"
    payload = json.loads(result.config_path.read_text(encoding="utf-8"))
    assert payload["database"] == str(
        home / ".local/share/research-agent-system/researchd.db"
    )
    assert payload["artifact_root"] == str(
        home / ".local/share/research-agent-system/artifacts"
    )
    assert payload["state_root"] == str(home / ".local/state/research-agent-system")
