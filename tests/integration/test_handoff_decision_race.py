"""AC07 A14–A16 adversarial handoff decision race and 9872fd1
decision_pending recovery across a real composed-daemon restart.

Layer 2 (integration / control plane).  No provider dependency.

All A14–A17 evidence crosses the authenticated loopback HTTP boundary
(``ControlCommandRouter.post``) and the composed-daemon lifecycle
(``compose_daemon`` / ``daemon.stop`` / ``daemon.start``).  No direct
``HandoffResolutionService`` calls are used for acceptance evidence.
"""

import asyncio
import http.client
import json
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.web import ControlCommandRouter
from researchd.collaboration.handoff import HandoffResolutionService
from researchd.daemon.composition import DaemonConfig, DaemonApplication, compose_daemon
from researchd.domain.enums import AgentAdapterKind, HandoffStatus
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentRuntimeRecord,
    AttemptRecord,
    AuditEventRecord,
    DelegationRecord,
    HandoffProposalRecord,
    RuntimeLaunchProfileRecord,
    RuntimeSessionRecord,
    WorkOrderRecord,
)
from tests.integration.test_handoff_safety import Fixture


def _rebind_runtimes(
    sessions: sessionmaker[Session],
) -> None:
    """Switch the Fixture's INTERNAL runtimes to PROCESS and create the
    supervised-session records the composed daemon's AgentSelector requires.

    Must be called **after** the composed daemon has started, so the
    startup barrier's RUNTIME_RECONCILIATION phase does not see active
    sessions it cannot observe.
    """
    now = datetime.now(UTC)
    launch_spec = {"argv": ["/usr/bin/true"], "cwd": "/tmp"}
    with sessions.begin() as session:
        for row in session.scalars(select(AgentRuntimeRecord)).all():
            row.adapter_kind = AgentAdapterKind.PROCESS.value
            spec_sha = "a" * 64
            session.add(RuntimeLaunchProfileRecord(
                runtime_id=row.runtime_id,
                launch_mode="PROCESS",
                configuration_json={"launch_spec": launch_spec},
                spec_sha256=spec_sha,
                enabled=True,
                version=1, created_at=now, updated_at=now,
            ))
            session.add(RuntimeSessionRecord(
                runtime_session_id=f"rs_{row.runtime_id}",
                runtime_id=row.runtime_id,
                launch_mode="PROCESS",
                supervisor_state="HEALTHY",
                launch_spec_json=launch_spec,
                launch_profile_sha256=spec_sha,
                external_identity_json={"pid": 1, "boot_id": "test"},
                reattach_state="NOT_APPLICABLE",
                started_at=now, last_health_at=now,
                version=1, created_at=now, updated_at=now,
            ))


def _setup(
    tmp_path: Path,
) -> tuple[DaemonApplication, str, sessionmaker[Session]]:
    """Seed the database via the Fixture, start a composed daemon, and
    rebind the runtimes so the AgentSelector can resolve them.

    Returns ``(application, proposal_id, sessions)``.
    """
    fixture = Fixture(tmp_path)
    proposal_id = fixture.proposal()
    fixture.terminalize_source()
    fixture.sessions.kw["bind"].dispose()

    from researchd.workspace.contracts import WorkspaceSource, WorkspaceTransportKind

    ws_root = tmp_path / "ws_root"
    ws_root.mkdir()

    config = DaemonConfig(
        database=tmp_path / "handoff.db",
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
        workspace_sources={
            "ws_h": WorkspaceSource(
                root=ws_root,
                transport_kind=WorkspaceTransportKind.ARCHIVE,
            ),
        },
    )
    application = compose_daemon(config)
    assert application.daemon.start().ready

    # Rebind runtimes **after** the daemon is ready so the startup
    # barrier's RUNTIME_RECONCILIATION phase does not see them.
    _rebind_runtimes(application.api.sessions)
    return application, proposal_id, application.api.sessions


def _router(application: DaemonApplication) -> ControlCommandRouter:
    return ControlCommandRouter(
        application.api,
        application.daemon,
        human_actor_id="local-control-client",
    )


def _handoff_decision(
    router: ControlCommandRouter,
    proposal_id: str,
    command_id: str,
    decision: str,
    reason: str,
    target_agent_id: str | None = None,
) -> dict[str, Any]:
    """POST a handoff decision through the authenticated HTTP boundary."""
    payload: dict[str, Any] = {
        "command_id": command_id,
        "decision": decision,
        "reason": reason,
    }
    if target_agent_id is not None:
        payload["target_agent_id"] = target_agent_id
    status, body = asyncio.run(router.post(
        f"/api/handoffs/{proposal_id}/decision",
        payload,
    ))
    assert status in (202, 409), f"unexpected HTTP status {status}"
    return body


# ------------------------------------------------------------------
# A14–A16: concurrent accept-vs-reject race via authenticated HTTP
# ------------------------------------------------------------------


def test_a14_a16_accept_vs_reject_concurrent_http_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race two authenticated HUMAN decisions for the same PROPOSED Handoff
    through the REAL authenticated loopback HTTP boundary, and prove that
    two DISTINCT server handler threads enter the Handoff arbitration at
    the same time.

    Server-side concurrency is proven at the arbitration entry: the real
    ``application.handoffs.accept`` / ``reject`` entry points (the same
    service instance the dispatcher invokes) are wrapped with a
    ``threading.Barrier(2)`` that must be passed BEFORE the original
    decision runs.  Both barrier parties are present only when two
    handler threads are simultaneously inside the entry; if the server
    degrades to serial processing the barrier is never satisfied, the
    wait times out, and the test fails.  The recorded handler thread ids
    must be two DISTINCT threads.

    Client-side request windows are kept for diagnostics only: overlap
    there shows both requests were in flight, which is necessary but not
    sufficient for server-side concurrency (a serial server still
    overlaps client windows), so they are not asserted as the proof.

    Each racer owns an independent HTTP connection (independent session)
    against the daemon's own ``ThreadingHTTPServer`` control server.  An
    ``asyncio.gather`` of the in-process router cannot provide this
    proof at all, because the routing core executes synchronously and
    the two coroutines would simply run back-to-back.

    The database-level CAS in ``HandoffResolutionService`` guarantees
    exactly one durable decision wins regardless of which racer arrives
    first.  The loser creates no Attempt, Delegation, workspace grant,
    child WorkOrder, or runnable Run transition; the invariants below are
    therefore written race-safe, branching on which decision won.
    """
    application, proposal_id, sessions = _setup(tmp_path)

    from researchd.api.web import serve_local_control
    from researchd.collaboration.handoff import HandoffProposal

    # --- Server-side concurrency instrumentation ----------------------
    # The dispatcher resolves the same HandoffResolutionService instance
    # (composition wires one service object into both the application and
    # the dispatcher), so wrapping its entry methods instruments the real
    # arbitration path used by the HTTP handlers.
    original_accept = application.handoffs.accept
    original_reject = application.handoffs.reject
    entry_barrier = threading.Barrier(2)
    handler_thread_ids: list[int] = []
    ids_guard = threading.Lock()

    def record_and_wait() -> None:
        with ids_guard:
            handler_thread_ids.append(threading.get_ident())
        # Both handler threads must be inside the entry simultaneously
        # before either decision may proceed; a serial server leaves the
        # barrier unsatisfied and this wait times out (the test fails).
        entry_barrier.wait(timeout=30)

    def instrumented_accept(
        proposal_id_: str,
        *,
        actor_type: str,
        actor_id: str,
        reason: str,
        target_agent_id: str | None = None,
    ) -> HandoffProposal:
        record_and_wait()
        return original_accept(
            proposal_id_,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            target_agent_id=target_agent_id,
        )

    def instrumented_reject(
        proposal_id_: str,
        *,
        actor_type: str,
        actor_id: str,
        reason: str,
    ) -> HandoffProposal:
        record_and_wait()
        return original_reject(
            proposal_id_,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )

    monkeypatch.setattr(application.handoffs, "accept", instrumented_accept)
    monkeypatch.setattr(application.handoffs, "reject", instrumented_reject)

    # --- Real authenticated loopback HTTP boundary ---------------------
    control_token = secrets.token_hex(16)
    server = serve_local_control(
        application.api,
        daemon=application.daemon,
        host="127.0.0.1",
        port=0,
        control_token=control_token,
    )
    # Bound explicitly to 127.0.0.1; only the ephemeral port is unknown.
    host = "127.0.0.1"
    port = int(server.server_address[1])
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    racers: tuple[tuple[str, str, str, str | None], ...] = (
        ("cmd_race_accept", "accept", "take over", "agent_b"),
        ("cmd_race_reject", "reject", "no longer needed", None),
    )
    release_barrier = threading.Barrier(len(racers))
    results: list[tuple[int, dict[str, Any]] | None] = [None] * len(racers)
    windows: list[tuple[float, float]] = [(0.0, 0.0)] * len(racers)  # diagnostics only
    errors: list[BaseException | None] = [None] * len(racers)

    def racer(index: int, command_id: str, decision: str, reason: str, target: str | None) -> None:
        payload: dict[str, Any] = {
            "command_id": command_id,
            "decision": decision,
            "reason": reason,
        }
        if target is not None:
            payload["target_agent_id"] = target
        # Each racer owns its own connection: an independent HTTP session.
        connection = http.client.HTTPConnection(host, port, timeout=60)
        try:
            connection.connect()
            release_barrier.wait()
            start = time.monotonic()
            connection.request(
                "POST",
                f"/api/handoffs/{proposal_id}/decision",
                body=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {control_token}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            body = json.loads(response.read())
            end = time.monotonic()
            results[index] = (response.status, body)
            windows[index] = (start, end)
        except BaseException as error:  # noqa: BLE001 - reported per racer
            errors[index] = error
        finally:
            connection.close()

    threads = [
        threading.Thread(target=racer, args=(index, *args), name=f"handoff-racer-{index}")
        for index, args in enumerate(racers)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert all(not thread.is_alive() for thread in threads), "a racer did not finish"
        assert all(error is None for error in errors), errors
        pairs = [pair for pair in results if pair is not None]
        assert len(pairs) == len(racers)

        # Server-side concurrency proof: two DISTINCT handler threads were
        # simultaneously inside the handoff entry (the barrier only
        # releases once both parties are present, before the original
        # accept/reject runs).
        assert len(handler_thread_ids) == 2, (
            "both decisions must reach the handoff entry"
        )
        assert len(set(handler_thread_ids)) == 2, (
            "two DISTINCT server handler threads must enter the arbitration: "
            f"{handler_thread_ids}"
        )

        statuses = {body.get("status") for _, body in pairs}
        assert statuses == {"ACCEPTED", "REJECTED"}, (
            f"expected exactly one ACCEPTED and one REJECTED, got {statuses}"
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=10)

    with sessions() as session:
        attempts = session.scalars(select(AttemptRecord).where(
            AttemptRecord.attempt_id.like("att_handoff_%"),
        )).all()
        delegations = session.scalars(select(DelegationRecord).where(
            DelegationRecord.delegation_id.like("del_handoff_%"),
        )).all()
        child_work_orders = session.scalars(select(WorkOrderRecord).where(
            WorkOrderRecord.parent_work_order_id == "wo_exec",
        )).all()
        proposal = session.get(HandoffProposalRecord, proposal_id)
        assert proposal is not None
        # No torn state in either outcome: at most one of each durable
        # side effect, and no child WorkOrder in either outcome.
        assert len(attempts) <= 1
        assert len(delegations) <= 1
        assert child_work_orders == []
        if proposal.status == HandoffStatus.ACCEPTED.value:
            # The accept side won: its side effects are complete and the
            # reject side left no trace.
            assert len(attempts) == 1
            assert len(delegations) == 1
            assert session.scalar(select(AuditEventRecord.event_type).where(
                AuditEventRecord.event_type == "HANDOFF_REJECTED",
                AuditEventRecord.entity_id == proposal_id,
            )) is None
        else:
            # The reject side won: no accept side effects may exist.
            assert proposal.status == HandoffStatus.REJECTED.value
            assert attempts == []
            assert delegations == []
            rejected = session.scalars(select(AuditEventRecord.event_type).where(
                AuditEventRecord.event_type == "HANDOFF_REJECTED",
                AuditEventRecord.entity_id == proposal_id,
            )).all()
            assert len(rejected) == 1


# ------------------------------------------------------------------
# A14–A16: interruption after controller effect, convergence via HTTP
# ------------------------------------------------------------------


def test_a14_a16_interruption_after_controller_effect_converges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject interruption after the accept-side controller effect but before
    the proposal decision is recorded.  Recovery must converge to ACCEPTED
    through the authenticated HTTP boundary and must never permit a later
    REJECTED proposal to coexist with the created authority."""
    application, proposal_id, sessions = _setup(tmp_path)
    router = _router(application)

    handoffs: HandoffResolutionService = application.handoffs
    original_decide = handoffs._decide

    def crash_before_decision(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated crash before Handoff decision commit")

    monkeypatch.setattr(handoffs, "_decide", crash_before_decision)

    # Fault injection: crash the controller effect path directly (not a
    # product action).  The reservation is already committed; the decision
    # commit is lost.
    with pytest.raises(RuntimeError, match="decision commit"):
        handoffs.accept(
            proposal_id,
            actor_type="HUMAN",
            actor_id="local-control-client",
            reason="accept and continue",
            target_agent_id="agent_b",
        )

    monkeypatch.setattr(handoffs, "_decide", original_decide)

    # Reject must fail-closed via the authenticated HTTP boundary.
    reject_body = _handoff_decision(
        router, proposal_id,
        command_id="cmd_interrupt_reject",
        decision="reject", reason="reject after crash",
    )
    assert reject_body.get("status") == "REJECTED"

    # Accept replay converges to ACCEPTED via the authenticated HTTP boundary.
    accept_body = _handoff_decision(
        router, proposal_id,
        command_id="cmd_interrupt_accept_replay",
        decision="accept", reason="resume after crash",
        target_agent_id="agent_b",
    )
    assert accept_body.get("status") == "ACCEPTED"

    with sessions() as session:
        attempts = session.scalars(select(AttemptRecord).where(
            AttemptRecord.attempt_id.like("att_handoff_%"),
        )).all()
        assert len(attempts) == 1

        rejected = session.scalar(select(HandoffProposalRecord).where(
            HandoffProposalRecord.proposal_id == proposal_id,
            HandoffProposalRecord.status == "REJECTED",
        ))
        assert rejected is None


# ------------------------------------------------------------------
# 9872fd1: decision_pending survives a real composed-daemon restart
# ------------------------------------------------------------------


def test_9872fd1_decision_pending_survives_daemon_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that decision_pending survives a real composed-daemon restart,
    reject remains fail-closed, accept replay converges to ACCEPTED,
    and the reservation audit is exactly one.

    The restart is performed by stopping the composed daemon, re-composing
    a new ``DaemonApplication`` on the same database, and starting it.
    All post-restart queries go through the public projection API and the
    authenticated HTTP boundary.
    """
    application, proposal_id, sessions = _setup(tmp_path)
    router = _router(application)

    handoffs: HandoffResolutionService = application.handoffs
    controller = application.api.orchestrator
    assert controller is not None
    original_retry = controller.retry_attempt

    def crash_retry(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("simulated crash after reservation, before controller effect")

    monkeypatch.setattr(controller, "retry_attempt", crash_retry)

    # Fault injection: crash the controller effect directly (not a product
    # action).  The reservation is already committed; the controller effect
    # and decision commit are lost.
    with pytest.raises(RuntimeError, match="simulated crash"):
        handoffs.accept(
            proposal_id,
            actor_type="HUMAN",
            actor_id="local-control-client",
            reason="accept and continue",
            target_agent_id="agent_b",
        )

    monkeypatch.setattr(controller, "retry_attempt", original_retry)

    # Verify decision_pending in the projection before the restart.
    handoffs_projection = application.api.handoffs("run_h")
    pending = [h for h in handoffs_projection if h["proposal_id"] == proposal_id]
    assert len(pending) == 1
    assert pending[0]["status"] == "PROPOSED"
    assert pending[0]["decision_pending"] is True

    with sessions() as session:
        reserved = session.scalars(select(AuditEventRecord).where(
            AuditEventRecord.event_type == "HANDOFF_ACCEPT_RESERVED",
            AuditEventRecord.entity_id == proposal_id,
        )).all()
        assert len(reserved) == 1

    # --- Real composed-daemon restart ---
    # Stop the supervised sessions so the new daemon's startup barrier
    # does not try to reconcile them.
    with sessions.begin() as session:
        for row in session.scalars(select(RuntimeSessionRecord)).all():
            row.supervisor_state = "STOPPED"
            row.stopped_at = datetime.now(UTC)
            row.exit_reason = "test_restart"
            row.version += 1

    application.daemon.stop()
    from researchd.workspace.contracts import WorkspaceSource, WorkspaceTransportKind

    restart_config = DaemonConfig(
        database=tmp_path / "handoff.db",
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
        workspace_sources={
            "ws_h": WorkspaceSource(
                root=tmp_path / "ws_root",
                transport_kind=WorkspaceTransportKind.ARCHIVE,
            ),
        },
    )
    new_application = compose_daemon(restart_config)
    assert new_application.daemon.start().ready

    # Re-enable the supervised sessions so the AgentSelector can resolve
    # the target Agent for the accept replay.
    with new_application.api.sessions.begin() as session:
        for row in session.scalars(select(RuntimeSessionRecord)).all():
            row.supervisor_state = "HEALTHY"
            row.stopped_at = None
            row.exit_reason = None
            row.last_health_at = datetime.now(UTC)
            row.version += 1

    new_router = _router(new_application)
    new_sessions = new_application.api.sessions

    # decision_pending survives the restart.
    handoffs_after_restart = new_application.api.handoffs("run_h")
    pending_after = [
        h for h in handoffs_after_restart if h["proposal_id"] == proposal_id
    ]
    assert len(pending_after) == 1
    assert pending_after[0]["status"] == "PROPOSED"
    assert pending_after[0]["decision_pending"] is True

    # Reject remains fail-closed after the restart (via authenticated HTTP).
    reject_body = _handoff_decision(
        new_router, proposal_id,
        command_id="cmd_restart_reject",
        decision="reject", reason="reject after restart",
    )
    assert reject_body.get("status") == "REJECTED"

    # Accept replay converges to ACCEPTED (via authenticated HTTP).
    accept_body = _handoff_decision(
        new_router, proposal_id,
        command_id="cmd_restart_accept_replay",
        decision="accept", reason="resume after restart",
        target_agent_id="agent_b",
    )
    assert accept_body.get("status") == "ACCEPTED"

    with new_sessions() as session:
        reserved = session.scalars(select(AuditEventRecord).where(
            AuditEventRecord.event_type == "HANDOFF_ACCEPT_RESERVED",
            AuditEventRecord.entity_id == proposal_id,
        )).all()
        assert len(reserved) == 1

        attempts = session.scalars(select(AttemptRecord).where(
            AttemptRecord.attempt_id.like("att_handoff_%"),
        )).all()
        assert len(attempts) == 1

        proposal = session.get(HandoffProposalRecord, proposal_id)
        assert proposal is not None
        assert proposal.status == HandoffStatus.ACCEPTED.value
