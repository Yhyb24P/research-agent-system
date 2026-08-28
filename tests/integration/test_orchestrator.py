import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from researchd.agents.cloud_lead import CloudLeadAdapter
from researchd.context.builder import ContextBuilder
from researchd.context.redaction import DeterministicRedactor
from researchd.domain.enums import Capability, DataClassification, VerificationOverall
from researchd.domain.criteria import acceptance_fingerprint
from researchd.domain.verification import CriterionEvaluation, VerificationResult
from researchd.domain.ids import VerificationId
from researchd.executor.contracts import ExecutorResult
from researchd.models.cloud import CloudCallBudget, CloudModelRequest, CloudModelResponse, CloudPricing, CloudUsage
from researchd.orchestrator.engine import OrchestrationLimits, ResearchOrchestrator
from researchd.cli.main import build_parser
from researchd.policy.approval import ApprovalService
from researchd.policy.engine import BudgetLimits, DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AttemptRecord, AuditEventRecord, ResearchRunRecord, VerificationResultRecord, WorkOrderRecord, WorkspaceRecord,
)
from researchd.verifier.contracts import VerificationInputs


ROOT = Path(__file__).parents[2]


def migrate(path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    command.check(config)


class FakeCloud:
    provider_name = "fake-cloud"
    model_name = "fake-model"

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[CloudModelRequest] = []

    async def complete(self, request: CloudModelRequest) -> CloudModelResponse:
        self.requests.append(request)
        response_text = self.responses.pop(0)
        if "ignored-by-test" in response_text:
            response_text = response_text.replace("ignored-by-test", json.loads(request.context_json)["work_order_id"])
        if "ver_placeholder" in response_text:
            response_text = response_text.replace("ver_placeholder", json.loads(request.context_json)["verification"]["verification_id"])
        return CloudModelResponse(
            text=response_text, usage=CloudUsage(prompt_tokens=4, completion_tokens=3, total_tokens=7),
            provider_request_id=f"fake-{len(self.requests)}",
        )


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.cancelled: list[str] = []

    async def execute(self, work_order: Any, attempt: Any) -> ExecutorResult:
        self.calls += 1
        return ExecutorResult(
            attempt_id=attempt.attempt_id, status="execution_complete", capability_results=(),
            reported_claims=("NaN fix applied",), errors=(),
        )

    async def cancel(self, attempt_id: str) -> None:
        self.cancelled.append(attempt_id)


class FlakyExecutor(FakeExecutor):
    async def execute(self, work_order: Any, attempt: Any) -> ExecutorResult:
        self.calls += 1
        return ExecutorResult(
            attempt_id=attempt.attempt_id,
            status="failed" if self.calls == 1 else "execution_complete",
            capability_results=(), reported_claims=(), errors=("injected failure",) if self.calls == 1 else (),
        )


class FakeVerifier:
    def __init__(self, sessions: Any, *, passed: bool = True) -> None:
        self.sessions = sessions
        self.passed = passed

    def verify(self, work_order: Any, attempt: Any, result: ExecutorResult) -> VerificationResult:
        now = datetime.now(UTC)
        overall = VerificationOverall.PASS if self.passed else VerificationOverall.FAIL
        verification_id = VerificationId(f"ver_{attempt.attempt_id}")
        fingerprint = acceptance_fingerprint(work_order.contract.get("acceptance", []))
        with self.sessions.begin() as session:
            session.add(VerificationResultRecord(
                verification_id=verification_id, attempt_id=attempt.attempt_id,
                work_order_id=work_order.work_order_id, overall=overall.value, criteria_json=[],
                acceptance_sha256=fingerprint, verifier_version="fake-verifier",
                valid=True, classification=DataClassification.PUBLIC.value, created_at=now,
            ))
        return VerificationResult(
            verification_id=verification_id, attempt_id=attempt.attempt_id, overall=overall,
            criteria=(), acceptance_sha256=fingerprint, verifier_version="fake-verifier",
            valid=True, classification=DataClassification.PUBLIC,
        )


def _proposal() -> str:
    return json.dumps({
        "proposal_id": "plan_nan_001", "hypotheses": [],
        "proposed_work_orders": [{
            "proposal_id": "fix_nan_001", "objective": "fix the reproducible NaN smoke failure",
            "inputs": [], "requested_capabilities": [],
            "constraints": {"network": "none", "writable_paths": []},
            "budget": {"max_wall_seconds": 60}, "acceptance": [], "expected_outputs": [],
            "data_policy": {"default_classification": "LOCAL_ONLY"}, "evidence_refs": [],
        }], "risks": [], "required_evidence": [],
    })


def _proposal_with_capability(capability: str) -> str:
    return json.dumps({
        "proposal_id": "plan_cap_001", "hypotheses": [],
        "proposed_work_orders": [{
            "proposal_id": "cap_wo_001", "objective": "perform approved operation",
            "inputs": [], "requested_capabilities": [capability],
            "constraints": {"network": "full", "writable_paths": []},
            "budget": {"max_wall_seconds": 60}, "acceptance": [], "expected_outputs": [],
            "data_policy": {"default_classification": "PROJECT_PRIVATE"}, "evidence_refs": [],
        }], "risks": [], "required_evidence": [],
    })


def _review(decision: str = "ACCEPT") -> str:
    return json.dumps({
        "decision": decision, "work_order_id": "ignored-by-test", "evidence_refs": ["ver_placeholder"],
        "deficiencies": [], "rationale": "deterministic checks are green",
        "requested_next_objective": None, "requested_evidence": [],
    })


def make_orchestrator(tmp_path: Path, *, cloud_responses: list[str], passed: bool = True, max_iterations: int = 8) -> tuple[Any, ResearchOrchestrator, FakeExecutor, FakeCloud]:
    db = tmp_path / "orchestrator.db"
    migrate(db)
    sessions = session_factory(create_sqlite_engine(db))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(workspace_id="ws_e2e", name="e2e", version=1, created_at=now, updated_at=now))
    from researchd.artifacts.store import ContentAddressedArtifactStore
    builder = ContextBuilder(sessions, ContentAddressedArtifactStore(tmp_path / "artifacts"), DeterministicRedactor())
    model = FakeCloud(cloud_responses)
    cloud = CloudLeadAdapter(
        model, sessions, builder,
        budget=CloudCallBudget(max_requests=3, max_input_bytes=100_000, max_response_bytes=100_000, max_output_tokens=512, max_total_tokens=2_000),
        pricing=CloudPricing(prompt_usd_per_million=Decimal("0"), completion_usd_per_million=Decimal("0")),
    )
    executor = FakeExecutor()
    policy = RecordingPolicyEngine(DeterministicPolicyEngine(), sessions)
    orchestrator = ResearchOrchestrator(
        sessions, cloud, policy, executor, FakeVerifier(sessions, passed=passed),
        workspace_capabilities=frozenset(), user_capabilities=frozenset(),
        maximum_budget=BudgetLimits(100, 100, 0, 100, 100),
        limits=OrchestrationLimits(max_iterations=max_iterations, max_cloud_calls=8),
    )
    return sessions, orchestrator, executor, model


def test_full_fake_nan_loop_reaches_completed_with_trace(tmp_path: Path) -> None:
    sessions, orchestrator, executor, model = make_orchestrator(tmp_path, cloud_responses=[_proposal(), _review()])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="reproduce and fix NaN")
    snapshot = asyncio.run(orchestrator.run(run_id, max_steps=30))
    assert snapshot.state.value == "COMPLETED"
    assert executor.calls == 1 and len(model.requests) == 2
    with sessions() as session:
        events = session.scalars(select(AuditEventRecord).where(AuditEventRecord.run_id == run_id)).all()
        names = {event.event_type for event in events}
        assert {"RUN_CREATED", "PLAN_CREATED", "WORK_ORDER_DISPATCHED", "VERIFICATION_COMPLETED", "REVIEW_DECISION_RECORDED", "WORK_ORDER_ACCEPTED", "RUN_COMPLETED"} <= names


def test_verification_failure_creates_a_new_revision_work_order(tmp_path: Path) -> None:
    sessions, orchestrator, executor, model = make_orchestrator(tmp_path, cloud_responses=[_proposal()], passed=False)
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="reproduce NaN")
    for _ in range(12):
        asyncio.run(orchestrator.advance(run_id))
        snapshot = orchestrator.snapshot(run_id)
        if snapshot.state.value == "FAILED":
            break
    with sessions() as session:
        orders = session.query(WorkOrderRecord).order_by(WorkOrderRecord.created_at).all()
        assert len(orders) >= 2
        assert orders[1].parent_work_order_id == orders[0].work_order_id
        assert orders[0].state == "REVISION_REQUIRED"


def test_human_required_pauses_and_explicit_abort_resolves(tmp_path: Path) -> None:
    sessions, orchestrator, executor, model = make_orchestrator(tmp_path, cloud_responses=[_proposal(), _review("HUMAN_REQUIRED")])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="reproduce NaN")
    snapshot = asyncio.run(orchestrator.run(run_id, max_steps=30))
    assert snapshot.state.value == "WAITING_HUMAN"
    work_order_id = snapshot.work_orders[0][0]
    status = orchestrator.resolve_human(work_order_id, action="abort")
    assert status is False
    assert orchestrator.snapshot(run_id).state.value == "FAILED"


def test_approval_request_pauses_then_resumes_policy(tmp_path: Path) -> None:
    db_sessions, _, _, _ = make_orchestrator(tmp_path, cloud_responses=[_proposal_with_capability("network.external")])
    # Rebuild the controller with an approval service and the same durable DB.
    from researchd.artifacts.store import ContentAddressedArtifactStore
    builder = ContextBuilder(db_sessions, ContentAddressedArtifactStore(tmp_path / "artifacts2"), DeterministicRedactor())
    model = FakeCloud([_proposal_with_capability("network.external")])
    cloud = CloudLeadAdapter(model, db_sessions, builder,
        budget=CloudCallBudget(max_requests=3, max_input_bytes=100_000, max_response_bytes=100_000, max_output_tokens=512, max_total_tokens=2_000),
        pricing=CloudPricing(prompt_usd_per_million=Decimal("0"), completion_usd_per_million=Decimal("0")))
    approvals = ApprovalService(db_sessions)
    orchestrator = ResearchOrchestrator(
        db_sessions, cloud, RecordingPolicyEngine(DeterministicPolicyEngine(), db_sessions), FakeExecutor(), FakeVerifier(db_sessions),
        approvals=approvals, workspace_capabilities=frozenset({Capability.NETWORK_EXTERNAL}),
        user_capabilities=frozenset({Capability.NETWORK_EXTERNAL}), maximum_budget=BudgetLimits(100, 100, 0, 100, 100),
    )
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="approved external step")
    snapshot = asyncio.run(orchestrator.run(run_id, max_steps=10))
    assert snapshot.state.value == "WAITING_HUMAN" and snapshot.pending_approval_ids
    order_id = snapshot.work_orders[0][0]
    with db_sessions() as session:
        order = session.get(WorkOrderRecord, order_id)
        assert order is not None and order.approval_id is not None
        approval_id = order.approval_id
    grant = approvals.approve(approval_id, granted_by="test-human")
    asyncio.run(orchestrator.approve(order_id, grant.grant_id))
    asyncio.run(orchestrator.advance(run_id))
    assert orchestrator.snapshot(run_id).work_orders[0][1].value == "READY"


def test_restart_after_execution_result_reconciles_into_verification(tmp_path: Path) -> None:
    sessions, orchestrator, executor, model = make_orchestrator(tmp_path, cloud_responses=[_proposal(), _review()])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="restart-safe")
    for _ in range(5):
        asyncio.run(orchestrator.advance(run_id))
    with sessions() as session:
        order = session.query(WorkOrderRecord).one()
        attempt = session.query(AttemptRecord).one()
    result = asyncio.run(executor.execute(order, attempt))
    orchestrator._store_execution_result(attempt.attempt_id, result)
    orchestrator.recover(run_id)
    snapshot = asyncio.run(orchestrator.run(run_id, max_steps=20))
    assert snapshot.state.value == "COMPLETED"
    with sessions() as session:
        assert session.scalar(select(AuditEventRecord.event_type).where(AuditEventRecord.event_type == "RECOVERY_EXECUTION_RECONCILED")) == "RECOVERY_EXECUTION_RECONCILED"


def test_cancel_before_execution_prevents_dispatch(tmp_path: Path) -> None:
    sessions, orchestrator, executor, model = make_orchestrator(tmp_path, cloud_responses=[_proposal()])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="cancel me")
    for _ in range(3):
        asyncio.run(orchestrator.advance(run_id))
    snapshot = asyncio.run(orchestrator.cancel(run_id))
    assert snapshot.state.value == "CANCELLED" and executor.calls == 0


def test_max_iteration_budget_fails_without_unbounded_revisions(tmp_path: Path) -> None:
    sessions, orchestrator, executor, model = make_orchestrator(tmp_path, cloud_responses=[_proposal()], passed=False, max_iterations=1)
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="bounded")
    snapshot = asyncio.run(orchestrator.run(run_id, max_steps=30))
    assert snapshot.state.value == "FAILED"
    with sessions() as session:
        run = session.get(ResearchRunRecord, run_id)
        assert run is not None and run.iterations_used == 1


def test_cli_parser_exposes_only_local_status_controls() -> None:
    assert build_parser().parse_args(["status", "run_demo"]).command == "status"
    assert build_parser().parse_args(["events", "run_demo"]).command == "events"
    assert build_parser().parse_args(["cancel", "run_demo"]).command == "cancel"
    assert build_parser().parse_args(["agent", "list"]).agent_command == "list"
    assert build_parser().parse_args(["agent", "inspect", "agent_demo"]).agent_command == "inspect"
    assert build_parser().parse_args(["delegation", "list", "--run", "run_demo"]).delegation_command == "list"
    assert build_parser().parse_args(["run", "status", "run_demo"]).run_command == "status"
    assert build_parser().parse_args(["events", "watch", "run_demo"]).first == "watch"
    assert build_parser().parse_args(["events", "run_demo", "--after", "evt_1"]).after_event_id == "evt_1"


def test_retry_unchanged_work_order_creates_new_attempt(tmp_path: Path) -> None:
    sessions, orchestrator, _, model = make_orchestrator(tmp_path, cloud_responses=[_proposal(), _review()])
    flaky = FlakyExecutor()
    orchestrator.executor = flaky
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="retry")
    for _ in range(5):
        asyncio.run(orchestrator.advance(run_id))
    with sessions() as session:
        order = session.query(WorkOrderRecord).one()
        assert order.state == "EXECUTION_FAILED"
        first_attempt = session.query(AttemptRecord).one().attempt_id
    second_attempt = orchestrator.retry_attempt(order.work_order_id)
    with sessions() as session:
        assert session.query(AttemptRecord).count() == 2
        assert second_attempt != first_attempt
        assert session.query(WorkOrderRecord).one().contract["objective"] == "fix the reproducible NaN smoke failure"
    snapshot = asyncio.run(orchestrator.run(run_id, max_steps=30))
    assert snapshot.state.value == "COMPLETED" and flaky.calls == 2
