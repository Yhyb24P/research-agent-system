"""Trusted config discovery for the daily ``research`` client.

Precedence (first match wins):

1. explicit ``--config <path>``
2. ``RESEARCH_CONFIG`` environment variable
3. ``$XDG_CONFIG_HOME/research-agent-system/config.json``
4. ``~/.config/research-agent-system/config.json``

Repository-local configuration is never auto-discovered: executable paths
and capability settings from a checkout are only trusted when the user
explicitly names the file via ``--config`` or ``RESEARCH_CONFIG``.
"""

import os
from collections.abc import Mapping
from pathlib import Path

CONFIG_ENV_VAR = "RESEARCH_CONFIG"
CONFIG_DIR_NAME = "research-agent-system"
CONFIG_FILE_NAME = "config.json"


def default_config_dir(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Return the trusted global config directory (XDG or ~/.config)."""
    env = os.environ if environ is None else environ
    base_home = Path.home() if home is None else home
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / CONFIG_DIR_NAME
    return base_home / ".config" / CONFIG_DIR_NAME


def default_config_path(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Return the trusted global config file path (whether or not it exists)."""
    return default_config_dir(environ, home) / CONFIG_FILE_NAME


def default_state_root(home: Path | None = None) -> Path:
    """Default daemon state root for first-run setup."""
    base_home = Path.home() if home is None else home
    return base_home / ".local" / "state" / CONFIG_DIR_NAME


def default_data_root(home: Path | None = None) -> Path:
    """Default data root (database, artifacts) for first-run setup."""
    base_home = Path.home() if home is None else home
    return base_home / ".local" / "share" / CONFIG_DIR_NAME


def resolve_config_path(
    cli_arg: Path | None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path | None:
    """Resolve the effective config path, or None when nothing is configured.

    Explicit sources (``--config``, ``RESEARCH_CONFIG``) are returned even if
    the file does not exist so the caller can surface a precise error; the
    global fallback is only returned when the file actually exists.
    """
    if cli_arg is not None:
        return cli_arg
    env = os.environ if environ is None else environ
    from_env = env.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env)
    global_path = default_config_path(env, home)
    if global_path.is_file():
        return global_path
    return None


__all__ = [
    "CONFIG_DIR_NAME",
    "CONFIG_ENV_VAR",
    "CONFIG_FILE_NAME",
    "default_config_dir",
    "default_config_path",
    "default_data_root",
    "default_state_root",
    "resolve_config_path",
]
