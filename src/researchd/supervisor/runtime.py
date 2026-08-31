"""Persist intent before side effects and reconcile every active session."""

from collections.abc import Iterable
import threading

from researchd.collaboration.registry import AgentRegistryService
from researchd.runtime_sessions.contracts import (
    CommandStatus,
    ExternalObservation,
    LaunchMode,
    ReattachState,
    RuntimeSession,
    RuntimeSessionAttachCommand,
    RuntimeSessionStartCommand,
    RuntimeSessionStopCommand,
    SupervisorState,
)
from researchd.runtime_sessions.service import RuntimeSessionService
from researchd.supervisor.drivers import (
    ManagedProcessDriver,
    RemoteHttpDriver,
    RuntimeDriver,
)


class RuntimeLaunchError(RuntimeError):
    """Raised after a failed side effect has been durably recorded."""


def _lease_owner(runtime_session: RuntimeSession) -> str:
    """Derive a stable daemon-owned lease identity from one session."""
    return f"runtime-session:{runtime_session.runtime_session_id}"


class RuntimeSupervisor:
    def __init__(
        self,
        service: RuntimeSessionService,
        drivers: Iterable[RuntimeDriver] | None = None,
        *,
        registry: AgentRegistryService | None = None,
        lease_seconds: int = 30,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        selected = tuple(drivers) if drivers is not None else (
            ManagedProcessDriver(),
            RemoteHttpDriver(),
        )
        self.service = service
        self.registry = registry
        self.lease_seconds = lease_seconds
        self.drivers = {driver.launch_mode: driver for driver in selected}
        if len(self.drivers) != len(selected):
            raise ValueError("only one runtime driver may own each launch mode")

    def start(self, command: RuntimeSessionStartCommand) -> RuntimeSession:
        intent = self.service.begin_start(command)
        if not intent.created:
            return intent.session
        return self._perform_start(command.command_id, intent.session, ReattachState.NOT_APPLICABLE)

    def attach(self, command: RuntimeSessionAttachCommand) -> RuntimeSession:
        intent = self.service.begin_attach(command)
        if not intent.created:
            return intent.session
        return self._perform_start(command.command_id, intent.session, ReattachState.ATTACHED)

    def _perform_start(
        self,
        command_id: str,
        runtime_session: RuntimeSession,
        reattach_state: ReattachState,
    ) -> RuntimeSession:
        lease_acquired = False
        try:
            self._acquire_local_lease(runtime_session)
            lease_acquired = True
            driver = self._driver(runtime_session.launch_mode)
            identity = driver.start(runtime_session.launch_spec)
        except Exception as error:
            if lease_acquired:
                self._release_local_lease(runtime_session)
            self.service.complete_command(
                command_id,
                expected_version=runtime_session.version,
                target=SupervisorState.LOST,
                exit_reason="launch_failed",
                reattach_state=ReattachState.FAILED,
                command_status=CommandStatus.FAILED,
                failure_reason=type(error).__name__[:128],
            )
            raise RuntimeLaunchError("runtime launch failed and was recorded") from error
        return self.service.complete_command(
            command_id,
            expected_version=runtime_session.version,
            target=SupervisorState.HEALTHY,
            external_identity=identity,
            reattach_state=reattach_state,
        )

    def stop(self, command: RuntimeSessionStopCommand) -> RuntimeSession:
        intent = self.service.begin_stop(command)
        if not intent.created:
            return intent.session
        runtime_session = intent.session
        identity = runtime_session.external_identity
        if identity is None:
            return self.service.complete_command(
                command.command_id,
                expected_version=runtime_session.version,
                target=SupervisorState.RECONCILIATION_REQUIRED,
                exit_reason="missing_external_identity",
                reattach_state=ReattachState.FAILED,
                command_status=CommandStatus.FAILED,
                failure_reason="missing_external_identity",
            )
        try:
            observation = self._driver(runtime_session.launch_mode).stop(identity)
        except Exception as error:
            self.service.complete_command(
                command.command_id,
                expected_version=runtime_session.version,
                target=SupervisorState.RECONCILIATION_REQUIRED,
                external_identity=identity,
                exit_reason="stop_failed",
                reattach_state=ReattachState.FAILED,
                command_status=CommandStatus.FAILED,
                failure_reason=type(error).__name__[:128],
            )
            raise RuntimeLaunchError("runtime stop failed and was recorded") from error
        if observation is ExternalObservation.ABSENT:
            completed = self.service.complete_command(
                command.command_id,
                expected_version=runtime_session.version,
                target=SupervisorState.STOPPED,
                external_identity=identity,
                exit_reason="requested_stop",
                reattach_state=ReattachState.DETACHED,
            )
            self._release_local_lease(runtime_session)
            return completed
        return self.service.complete_command(
            command.command_id,
            expected_version=runtime_session.version,
            target=SupervisorState.RECONCILIATION_REQUIRED,
            external_identity=identity,
            exit_reason="stop_not_confirmed",
            reattach_state=ReattachState.FAILED,
            command_status=CommandStatus.FAILED,
            failure_reason="stop_not_confirmed",
        )

    def reconcile_sessions(self) -> tuple[RuntimeSession, ...]:
        reconciled: list[RuntimeSession] = []
        for runtime_session in self.service.active():
            identity = runtime_session.external_identity
            if identity is None:
                reconciled.append(self._require_reconciliation(
                    runtime_session,
                    "missing_external_identity",
                ))
                continue
            driver = self.drivers.get(runtime_session.launch_mode)
            if driver is None:
                reconciled.append(self._require_reconciliation(
                    runtime_session,
                    "unsupported_launch_mode",
                ))
                continue
            observation = driver.observe(identity)
            if runtime_session.supervisor_state is SupervisorState.STOPPING:
                if observation is ExternalObservation.PRESENT:
                    observation = driver.stop(identity)
                target = (
                    SupervisorState.STOPPED
                    if observation is ExternalObservation.ABSENT
                    else SupervisorState.RECONCILIATION_REQUIRED
                )
                reconciled.append(self.service.reconcile(
                    str(runtime_session.runtime_session_id),
                    expected_version=runtime_session.version,
                    target=target,
                    external_identity=identity,
                    reattach_state=(
                        ReattachState.DETACHED
                        if target is SupervisorState.STOPPED
                        else ReattachState.FAILED
                    ),
                    exit_reason=(
                        "reconciled_stop"
                        if target is SupervisorState.STOPPED
                        else "stop_not_confirmed"
                    ),
                ))
            elif observation is ExternalObservation.PRESENT:
                healthy = self.service.reconcile(
                    str(runtime_session.runtime_session_id),
                    expected_version=runtime_session.version,
                    target=SupervisorState.HEALTHY,
                    external_identity=identity,
                    reattach_state=ReattachState.ATTACHED,
                )
                try:
                    self._acquire_local_lease(healthy)
                except Exception:
                    healthy = self._require_reconciliation(
                        healthy, "runtime_lease_unavailable",
                    )
                reconciled.append(healthy)
            elif observation is ExternalObservation.ABSENT:
                stopped = self.service.reconcile(
                    str(runtime_session.runtime_session_id),
                    expected_version=runtime_session.version,
                    target=SupervisorState.LOST,
                    external_identity=identity,
                    reattach_state=ReattachState.FAILED,
                    exit_reason="external_instance_absent",
                )
                self._release_local_lease(runtime_session)
                reconciled.append(stopped)
            else:
                reconciled.append(self._require_reconciliation(
                    runtime_session,
                    "external_identity_unverifiable",
                ))
        return tuple(reconciled)

    def _require_reconciliation(
        self,
        runtime_session: RuntimeSession,
        reason: str,
    ) -> RuntimeSession:
        if runtime_session.supervisor_state is SupervisorState.RECONCILIATION_REQUIRED:
            return runtime_session
        return self.service.reconcile(
            str(runtime_session.runtime_session_id),
            expected_version=runtime_session.version,
            target=SupervisorState.RECONCILIATION_REQUIRED,
            external_identity=runtime_session.external_identity,
            reattach_state=ReattachState.FAILED,
            exit_reason=reason,
        )

    def _driver(self, launch_mode: LaunchMode) -> RuntimeDriver:
        driver = self.drivers.get(launch_mode)
        if driver is None:
            raise ValueError(f"no runtime driver for {launch_mode.value}")
        return driver

    def renew_local_leases(self) -> int:
        """Renew leases for healthy supervised PROCESS sessions only."""
        renewed = 0
        for runtime_session in self.service.active():
            if runtime_session.supervisor_state is not SupervisorState.HEALTHY:
                continue
            if runtime_session.launch_mode is not LaunchMode.PROCESS:
                continue
            self._acquire_local_lease(runtime_session)
            renewed += 1
        return renewed

    def _acquire_local_lease(self, runtime_session: RuntimeSession) -> None:
        if self.registry is None or runtime_session.launch_mode is not LaunchMode.PROCESS:
            return
        self.registry.acquire_runtime(
            str(runtime_session.runtime_id),
            owner_id=_lease_owner(runtime_session),
            lease_seconds=self.lease_seconds,
        )

    def _release_local_lease(self, runtime_session: RuntimeSession) -> None:
        if self.registry is None or runtime_session.launch_mode is not LaunchMode.PROCESS:
            return
        self.registry.release_runtime_for_owner(
            str(runtime_session.runtime_id), owner_id=_lease_owner(runtime_session),
        )


class RuntimeLeaseHeartbeat:
    """Daemon lifecycle service that renews local supervised runtime leases."""

    def __init__(self, supervisor: RuntimeSupervisor, *, interval_seconds: float = 10.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.supervisor = supervisor
        self.interval_seconds = interval_seconds
        self._stopped = True
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_renewed = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped = False
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, name="researchd-runtime-lease-heartbeat", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.interval_seconds + 1)

    def health(self) -> dict[str, object]:
        return {
            "running": bool(self._thread is not None and self._thread.is_alive() and not self._stopped),
            "last_error": self._last_error,
            "last_renewed": self._last_renewed,
        }

    def _run(self) -> None:
        while not self._stopped:
            try:
                self._last_renewed = self.supervisor.renew_local_leases()
                self._last_error = None
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"
            self._wake.wait(timeout=self.interval_seconds)
