"""Trusted launch catalog for existing AgentRuntime identities."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import AgentAdapterKind
from researchd.domain.ids import AgentRuntimeId
from researchd.runtime_sessions.contracts import (
    LaunchMode,
    ProcessLaunchConfiguration,
    ProcessLaunchSpec,
    RemoteHttpAttachSpec,
    RemoteHttpLaunchConfiguration,
    ResolvedProcessLaunch,
    ResolvedRemoteHttpLaunch,
    RuntimeLaunchProfile,
)
from researchd.storage.models import RuntimeLaunchProfileRecord


class RuntimeLaunchProfileService:
    """Resolve launch details only from daemon-owned persistent configuration."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        registry: AgentRegistryService,
    ) -> None:
        self.sessions = sessions
        self.registry = registry

    def register_process(
        self,
        runtime_id: str,
        launch_spec: ProcessLaunchSpec,
        *,
        enabled: bool = True,
    ) -> RuntimeLaunchProfile:
        runtime = self.registry.get_runtime(runtime_id)
        if runtime.adapter_kind is not AgentAdapterKind.PROCESS:
            raise ValueError("PROCESS profile requires a PROCESS AgentRuntime")
        configuration = ProcessLaunchConfiguration(launch_spec=launch_spec).model_dump(mode="json")
        return self._register(runtime_id, LaunchMode.PROCESS, configuration, enabled)

    def register_remote_http(
        self,
        runtime_id: str,
        configuration: RemoteHttpLaunchConfiguration,
        *,
        enabled: bool = True,
    ) -> RuntimeLaunchProfile:
        runtime = self.registry.get_runtime(runtime_id)
        if runtime.adapter_kind is not AgentAdapterKind.HTTP or runtime.endpoint_ref is None:
            raise ValueError("REMOTE_HTTP profile requires an HTTP AgentRuntime endpoint")
        return self._register(
            runtime_id,
            LaunchMode.REMOTE_HTTP,
            configuration.model_dump(mode="json"),
            enabled,
        )

    def resolve_process(self, runtime_id: str) -> ResolvedProcessLaunch:
        profile = self._require_enabled(runtime_id, LaunchMode.PROCESS)
        configuration = ProcessLaunchConfiguration.model_validate(profile.configuration)
        return ResolvedProcessLaunch(
            launch_spec=configuration.launch_spec,
            spec_sha256=profile.spec_sha256,
        )

    def resolve_remote_http(self, runtime_id: str) -> ResolvedRemoteHttpLaunch:
        runtime = self.registry.require_enabled_runtime(runtime_id)
        if runtime.endpoint_ref is None:
            raise ValueError("HTTP AgentRuntime has no registered endpoint")
        profile = self._require_enabled(runtime_id, LaunchMode.REMOTE_HTTP)
        configuration = RemoteHttpLaunchConfiguration.model_validate(profile.configuration)
        return ResolvedRemoteHttpLaunch(
            launch_spec=RemoteHttpAttachSpec(
                endpoint=runtime.endpoint_ref,
                health_path=configuration.health_path,
            ),
            spec_sha256=profile.spec_sha256,
        )

    def get(self, runtime_id: str) -> RuntimeLaunchProfile:
        with self.sessions() as session:
            row = session.get(RuntimeLaunchProfileRecord, runtime_id)
            if row is None:
                raise ValueError(f"runtime launch profile does not exist: {runtime_id}")
            return self._from_record(row)

    def _register(
        self,
        runtime_id: str,
        launch_mode: LaunchMode,
        configuration: dict[str, object],
        enabled: bool,
    ) -> RuntimeLaunchProfile:
        now = datetime.now(UTC)
        digest = self._digest(launch_mode, configuration)
        with self.sessions.begin() as session:
            if session.get(RuntimeLaunchProfileRecord, runtime_id) is not None:
                raise ValueError(f"runtime launch profile already exists: {runtime_id}")
            row = RuntimeLaunchProfileRecord(
                runtime_id=runtime_id,
                launch_mode=launch_mode.value,
                configuration_json=configuration,
                spec_sha256=digest,
                enabled=enabled,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._from_record(row)

    def _require_enabled(
        self,
        runtime_id: str,
        launch_mode: LaunchMode,
    ) -> RuntimeLaunchProfile:
        self.registry.require_enabled_runtime(runtime_id)
        profile = self.get(runtime_id)
        if not profile.enabled or profile.launch_mode is not launch_mode:
            raise ValueError(f"runtime launch profile is unavailable: {runtime_id}")
        if profile.spec_sha256 != self._digest(profile.launch_mode, profile.configuration):
            raise ValueError("runtime launch profile digest mismatch")
        return profile

    @staticmethod
    def _digest(launch_mode: LaunchMode, configuration: dict[str, object]) -> str:
        payload = json.dumps(
            {"launch_mode": launch_mode.value, "configuration": configuration},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _from_record(row: RuntimeLaunchProfileRecord) -> RuntimeLaunchProfile:
        return RuntimeLaunchProfile(
            runtime_id=AgentRuntimeId(row.runtime_id),
            launch_mode=LaunchMode(row.launch_mode),
            configuration=dict(row.configuration_json),
            spec_sha256=row.spec_sha256,
            enabled=row.enabled,
            version=row.version,
        )


__all__ = ["RuntimeLaunchProfileService"]
