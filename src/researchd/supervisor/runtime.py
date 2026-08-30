"""Persist intent before side effects and reconcile every active session."""

from collections.abc import Iterable

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


class RuntimeSupervisor:
    def __init__(
        self,
        service: RuntimeSessionService,
        drivers: Iterable[RuntimeDriver] | None = None,
    ) -> None:
        selected = tuple(drivers) if drivers is not None else (
            ManagedProcessDriver(),
            RemoteHttpDriver(),
        )
        self.service = service
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
        try:
            driver = self._driver(runtime_session.launch_mode)
            identity = driver.start(runtime_session.launch_spec)
        except Exception as error:
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
            return self.service.complete_command(
                command.command_id,
                expected_version=runtime_session.version,
                target=SupervisorState.STOPPED,
                external_identity=identity,
                exit_reason="requested_stop",
                reattach_state=ReattachState.DETACHED,
            )
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
                reconciled.append(self.service.reconcile(
                    str(runtime_session.runtime_session_id),
                    expected_version=runtime_session.version,
                    target=SupervisorState.HEALTHY,
                    external_identity=identity,
                    reattach_state=ReattachState.ATTACHED,
                ))
            elif observation is ExternalObservation.ABSENT:
                reconciled.append(self.service.reconcile(
                    str(runtime_session.runtime_session_id),
                    expected_version=runtime_session.version,
                    target=SupervisorState.LOST,
                    external_identity=identity,
                    reattach_state=ReattachState.FAILED,
                    exit_reason="external_instance_absent",
                ))
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
