"""Authenticated loopback transport for the daily ``research`` client.

The client never opens the controller SQLite database; every operation
crosses the daemon's HTTP surface. Mutations carry a generated or
caller-supplied command identity so retries stay idempotent, and SSE
streams resume from the last observed stream offset.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from researchd.daemon.security import load_control_token


def new_command_id() -> str:
    """Unique command identity, safe for the 128-character command field."""
    return f"cmd_{uuid4().hex}"


def load_owner_token(state_root: Path) -> str:
    """Load the owner-only control credential created by ``researchd init``."""
    return load_control_token(state_root)


class TransportError(RuntimeError):
    """The daemon answered with an error status."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        super().__init__(f"daemon responded with HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


class AuthenticationRequired(TransportError):
    """The presented credential was missing or rejected."""


class CommandNotAccepted(TransportError):
    """A 409 without a REJECTED envelope: not-ready gate or identity conflict."""


class CommandRejected(TransportError):
    """The daemon durably rejected the command (REJECTED envelope)."""


class InvalidCommand(TransportError):
    """The daemon refused the request body (422)."""


@dataclass(frozen=True)
class StreamFrame:
    """One parsed SSE frame; ``offset`` is the resumable stream position."""

    offset: int | None
    data: dict[str, Any]


class ResearchClient:
    """Thin facade over the researchd control HTTP API."""

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=10.0)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "ResearchClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def health(self) -> dict[str, Any]:
        response = self._http.get(self._url("/api/health"))
        if response.status_code >= 400:
            raise TransportError(response.status_code, _json_or_empty(response))
        return cast_payload(response.json())

    def get(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        response = self._http.get(
            self._url(path), params=params, headers=self._headers()
        )
        if response.status_code == 401:
            raise AuthenticationRequired(401, _json_or_empty(response))
        if response.status_code >= 400:
            raise TransportError(response.status_code, _json_or_empty(response))
        return response.json()

    def post_command(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        body = dict(payload or {})
        body["command_id"] = command_id or new_command_id()
        response = self._http.post(
            self._url(path), json=body, headers=self._headers()
        )
        envelope = _json_or_empty(response)
        if response.status_code == 401:
            raise AuthenticationRequired(401, envelope)
        if response.status_code == 409:
            if envelope.get("status") == "REJECTED":
                raise CommandRejected(409, envelope)
            raise CommandNotAccepted(409, envelope)
        if response.status_code == 422:
            raise InvalidCommand(422, envelope)
        if response.status_code != 202:
            raise TransportError(response.status_code, envelope)
        return envelope

    def stream(
        self,
        path: str,
        *,
        after: int | None = None,
        follow: bool = False,
    ) -> Iterator[StreamFrame]:
        """Yield parsed SSE frames; resume by passing the last frame offset.

        Frames carry the server-assigned stream offset in their ``id:``
        line, which is the same cursor the ``after`` query parameter and
        the ``Last-Event-ID`` header accept.
        """
        params: dict[str, str] = {}
        if after is not None:
            params["after"] = str(after)
        if follow:
            params["follow"] = "1"
        with self._http.stream(
            "GET",
            self._url(path),
            params=params or None,
            headers=self._headers(),
        ) as response:
            if response.status_code == 401:
                response.read()
                raise AuthenticationRequired(401, {})
            if response.status_code != 200:
                response.read()
                raise TransportError(response.status_code, {})
            offset: int | None = None
            data: str | None = None
            for line in response.iter_lines():
                if line.startswith(":"):
                    continue
                if line.startswith("id:"):
                    offset = int(line[len("id:"):].strip())
                    continue
                if line.startswith("data:"):
                    data = line[len("data:"):].strip()
                    continue
                if not line:
                    if data is not None:
                        yield StreamFrame(offset, json.loads(data))
                        offset, data = None, None
            if data is not None:
                yield StreamFrame(offset, json.loads(data))


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        return cast_payload(response.json())
    except (json.JSONDecodeError, ValueError):
        return {"raw": response.text[:512]}


def cast_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TransportError(0, {"unexpected payload": repr(payload)[:128]})
    return payload


__all__ = [
    "AuthenticationRequired",
    "CommandNotAccepted",
    "CommandRejected",
    "InvalidCommand",
    "ResearchClient",
    "StreamFrame",
    "TransportError",
    "load_owner_token",
    "new_command_id",
]
