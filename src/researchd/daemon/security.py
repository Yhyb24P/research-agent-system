"""Owner-only local credential for the researchd control channel."""

import os
from pathlib import Path
import secrets
import stat


CONTROL_TOKEN_FILENAME = "control.token"


class ControlCredentialError(RuntimeError):
    pass


def control_token_path(state_root: Path) -> Path:
    return state_root / CONTROL_TOKEN_FILENAME


def create_control_token(state_root: Path) -> str:
    """Create a new 256-bit token without ever replacing an existing credential."""
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = control_token_path(state_root)
    token = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise ControlCredentialError(f"control credential already exists: {path}") from error
    try:
        os.write(descriptor, f"{token}\n".encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return token


def load_control_token(state_root: Path) -> str:
    """Load a regular, current-user-owned 0600 credential and validate its shape."""
    path = control_token_path(state_root)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ControlCredentialError(f"cannot open control credential: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ControlCredentialError("control credential must be a regular file")
        if metadata.st_uid != os.geteuid():
            raise ControlCredentialError("control credential must be owned by the daemon user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ControlCredentialError("control credential permissions must be 0600")
        payload = os.read(descriptor, 256)
        if os.read(descriptor, 1):
            raise ControlCredentialError("control credential is too large")
    finally:
        os.close(descriptor)
    try:
        token = payload.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ControlCredentialError("control credential is not ASCII") from error
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise ControlCredentialError("control credential has an invalid format")
    return token


__all__ = [
    "CONTROL_TOKEN_FILENAME",
    "ControlCredentialError",
    "control_token_path",
    "create_control_token",
    "load_control_token",
]
