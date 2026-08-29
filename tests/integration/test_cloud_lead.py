import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.agents.cloud_lead import CloudLeadAdapter
from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.context.builder import CloudContextSelection, ContextBuilder, EgressDenied
from researchd.context.redaction import DeterministicRedactor
from researchd.context.serializer import serialize_cloud_bundle
from researchd.domain.criteria import MetricCriterion, normalized_acceptance
from researchd.domain.enums import AttemptState, Capability, DataClassification, PolicyOutcome, ResearchRunState, WorkOrderState
from researchd.domain.verification import VerificationResult
from researchd.models.cloud import (
    CloudCallBudget,
    CloudBudgetExceeded,
    CloudModelRequest,
    CloudModelResponse,
    CloudPricing,
    CloudProviderConfiguration,
    CloudProviderUnavailable,
    CloudSchemaInvalid,
    CloudUsage,
)
from researchd.models.openai_compatible import OpenAICompatibleCloudModel
from researchd.policy.engine import BudgetLimits, DeterministicPolicyEngine, PolicyRequest
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AgentInteractionRecord, ArtifactRecord, AttemptRecord, AuditEventRecord, CloudInteractionGovernanceRecord, ResearchRunRecord, WorkspaceRecord, WorkOrderRecord
from researchd.storage.transitions import TransactionalTransitionService, TransitionPreconditionFailed
from researchd.verifier.contracts import VerificationInputs
from researchd.verifier.engine import VerifierEngine
from researchd.verifier.producers import TrustedObservationProducers

ROOT = Path(__file__).parents[2]
SECRET = "TASK05-SECRET-MUST-NOT-REACH-CLOUD-b9a7"


def migrate(path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    command.check(config)


class CloudFixture:
    def __init__(self, tmp_path: Path) -> None:
        database = tmp_path / "cloud.db"
        migrate(database)
        self.sessions = session_factory(create_sqlite_engine(database))
        self.store = ContentAddressedArtifactStore(tmp_path / "artifacts")
        self.artifacts = ArtifactService(self.store, self.sessions)
        self.builder = ContextBuilder(
            self.sessions, self.store,
            DeterministicRedactor(secret_literals=[SECRET], filesystem_prefixes=["/host/private"]),
        )
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(WorkspaceRecord(workspace_id="ws_cloud", name="cloud", version=1, created_at=now, updated_at=now))
            session.flush()
            session.add(ResearchRunRecord(run_id="run_cloud", workspace_id="ws_cloud", objective=f"review safe evidence; never expose {SECRET}", state=ResearchRunState.ACTIVE.value, version=1, created_at=now, updated_at=now))
            session.flush()
            session.add(WorkOrderRecord(
                work_order_id="wo_cloud", run_id="run_cloud", parent_work_order_id=None,
                objective="review metric from /host/private", state=WorkOrderState.VERIFYING.value,
                idempotency_key="cloud-idempotency-0001", contract={"acceptance": []},
                version=1, created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(AttemptRecord(
                attempt_id="att_cloud", work_order_id="wo_cloud", state=AttemptState.VERIFYING.value,
                terminal_at=None, version=1, created_at=now, updated_at=now,
            ))

    def verification(self, *, value: float, threshold: float) -> tuple[ArtifactRecord, VerificationResult]:
        artifact = self.artifacts.register(
            json.dumps({"score": value}).encode(), mime_type="application/json",
            artifact_type="metrics", classification=DataClassification.CLOUD_SAFE,
            producer_type="tool", producer_id="safe-metric-extractor", attempt_id="att_cloud",
        )
        criterion = MetricCriterion(criterion_id="c_score", type="metric", metric="score", operator=">=", threshold=threshold)
        with self.sessions.begin() as session:
            order = session.get(WorkOrderRecord, "wo_cloud")
            assert order is not None
            order.contract = {"acceptance": normalized_acceptance((criterion,))}
        result = VerifierEngine(self.sessions, TrustedObservationProducers(self.store)).verify(
            work_order_id="wo_cloud", attempt_id="att_cloud", criteria=(criterion,),
            inputs=VerificationInputs(metric_artifacts={"score": artifact.artifact_id}),
        )
        return artifact, result


@pytest.fixture
def fixture(tmp_path: Path) -> CloudFixture:
    return CloudFixture(tmp_path)


class QueueCloudModel:
    provider_name = "fake-cloud"
    model_name = "fake-structured-v1"

    def __init__(self, responses: list[str | Exception], usage: CloudUsage | None = None) -> None:
        self.responses = responses
        self.requests: list[CloudModelRequest] = []
        self.usage = usage or CloudUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    async def complete(self, request: CloudModelRequest) -> CloudModelResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return CloudModelResponse(text=response, usage=self.usage, provider_request_id=f"fake-{len(self.requests)}")


def plan_json() -> str:
    return json.dumps({"proposal_id": "plan-proposal-1", "hypotheses": [], "proposed_work_orders": [], "risks": [], "required_evidence": []})


def provider_configuration(
    model: QueueCloudModel | OpenAICompatibleCloudModel, *, requests: int = 3,
    retry_backoff: float = 0.25,
) -> CloudProviderConfiguration:
    endpoint = getattr(model, "base_url", "https://fake-cloud.example")
    timeout = float(getattr(model, "timeout_seconds", 60))
    return CloudProviderConfiguration(
        configuration_id="dq03-test-provider-v1", provider=model.provider_name,
        endpoint=endpoint, account_id="test-account", project_id="test-project",
        region="test-region", model=model.model_name, sdk_client="httpx-0.28.1",
        request_timeout_seconds=timeout, retry_max_requests=requests,
        retry_backoff_seconds=retry_backoff, retry_max_backoff_seconds=8,
        retention_policy="store=false", training_opt_out=True, privacy_mode="test-isolated",
        structured_output_mode="json-schema-strict",
        token_accounting_source="provider-usage", cost_accounting_source="configured-pricing",
    )


def adapter(
    model: QueueCloudModel | OpenAICompatibleCloudModel, fixture: CloudFixture, *,
    requests: int = 3, retry_backoff: float = 0.25, max_cost_usd: Decimal = Decimal("10"),
) -> CloudLeadAdapter:
    return CloudLeadAdapter(
        model, fixture.sessions, fixture.builder,
        configuration=provider_configuration(model, requests=requests, retry_backoff=retry_backoff),
        budget=CloudCallBudget(max_requests=requests, max_input_bytes=200_000, max_response_bytes=50_000, max_output_tokens=1000, max_total_tokens=10_000, max_cost_usd=max_cost_usd),
        pricing=CloudPricing(prompt_usd_per_million=Decimal("1.5"), completion_usd_per_million=Decimal("6")),
        retry_backoff_seconds=retry_backoff,
    )


def test_cloud_mock_receives_only_safe_redacted_authoritative_bundle(fixture: CloudFixture) -> None:
    local = fixture.artifacts.register(
        f"raw {SECRET}".encode(), mime_type="text/plain", artifact_type="raw",
        classification=DataClassification.LOCAL_ONLY, producer_type="tool", producer_id="fixture", attempt_id="att_cloud",
    )
    safe, verification = fixture.verification(value=2, threshold=1)
    bundle = fixture.builder.build(
        run_id="run_cloud", work_order_id="wo_cloud", artifact_ids=[safe.artifact_id],
        verification_id=str(verification.verification_id),
    )
    model = QueueCloudModel([plan_json()])
    selection = CloudContextSelection(
        run_id="run_cloud", work_order_id="wo_cloud", artifact_ids=(safe.artifact_id,),
        verification_id=str(verification.verification_id),
    )
    result = asyncio.run(adapter(model, fixture).propose_plan(selection))
    assert result.output.proposal_id == "plan-proposal-1"
    sent = model.requests[0].context_json.encode()
    assert SECRET.encode() not in sent and b"/host/private" not in sent
    assert b"score" in sent and str(verification.verification_id).encode() in sent
    assert local.artifact_id.encode() not in sent
    bypass_model = QueueCloudModel([plan_json()])
    with pytest.raises(TypeError, match="authoritative"):
        asyncio.run(adapter(bypass_model, fixture).propose_plan(bundle))  # type: ignore[arg-type]
    assert bypass_model.requests == []
    with pytest.raises(EgressDenied):
        fixture.builder.build(run_id="run_cloud", work_order_id="wo_cloud", artifact_ids=[local.artifact_id])


def test_malformed_json_has_bounded_repair_then_explicit_failure(fixture: CloudFixture) -> None:
    model = QueueCloudModel(["not-json", "{}", '{"proposal_id": 3}'])
    with pytest.raises(CloudSchemaInvalid, match="3 attempts"):
        asyncio.run(adapter(model, fixture, requests=3).propose_plan(CloudContextSelection(run_id="run_cloud")))
    assert len(model.requests) == 3
    assert model.requests[0].repair_instruction is None
    assert all(request.repair_instruction is not None for request in model.requests[1:])
    with fixture.sessions() as session:
        record = session.scalar(select(AgentInteractionRecord).where(AgentInteractionRecord.purpose == "PLAN"))
        assert record is not None
        assert record.status == "FAILED" and record.reason_code == "CLOUD_SCHEMA_INVALID" and record.attempts == 3
        assert record.response_json is None


def test_repair_success_accumulates_tokens_and_cost(fixture: CloudFixture) -> None:
    model = QueueCloudModel(["{}", plan_json()], usage=CloudUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120))
    result = asyncio.run(adapter(model, fixture).propose_plan(CloudContextSelection(run_id="run_cloud")))
    assert result.cost.attempts == 2 and result.cost.total_tokens == 240
    assert result.cost.cost_usd == Decimal("0.00054")
    with fixture.sessions() as session:
        record = session.get(AgentInteractionRecord, result.interaction_id)
        assert record is not None and record.status == "COMPLETED"
        assert record.bundle_sha256 and record.response_json == result.output.model_dump(mode="json")
        governance = session.get(CloudInteractionGovernanceRecord, result.interaction_id)
        assert governance is not None and governance.provider_configuration_id == "dq03-test-provider-v1"
        stored_configuration = CloudProviderConfiguration.model_validate(governance.provider_configuration_json)
        assert governance.provider_configuration_sha256 == stored_configuration.snapshot_sha256()
        assert stored_configuration.account_id == "test-account" and stored_configuration.training_opt_out
        events = session.scalars(
            select(AuditEventRecord)
            .where(AuditEventRecord.entity_id == result.interaction_id)
            .order_by(AuditEventRecord.timestamp)
        ).all()
        assert [event.event_type for event in events] == [
            "CLOUD_INTERACTION_STARTED", "CLOUD_INTERACTION_FINISHED",
        ]

    with pytest.raises(IntegrityError, match="configuration snapshot is immutable"):
        with fixture.sessions.begin() as session:
            governance = session.get(CloudInteractionGovernanceRecord, result.interaction_id)
            assert governance is not None
            governance.provider_configuration_id = "mutated"


def test_provider_configuration_mismatch_rejected_before_egress(fixture: CloudFixture) -> None:
    model = QueueCloudModel([plan_json()])
    configuration = provider_configuration(model).model_copy(update={"model": "different-model"})
    with pytest.raises(ValueError, match="does not match"):
        CloudLeadAdapter(
            model, fixture.sessions, fixture.builder, configuration=configuration,
            budget=CloudCallBudget(max_requests=3),
            pricing=CloudPricing(prompt_usd_per_million=Decimal("0"), completion_usd_per_million=Decimal("0")),
        )
    assert model.requests == []


def test_cost_budget_exhaustion_is_persisted(fixture: CloudFixture) -> None:
    model = QueueCloudModel(
        [plan_json()], usage=CloudUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000),
    )
    with pytest.raises(CloudBudgetExceeded, match="budget"):
        asyncio.run(adapter(model, fixture, max_cost_usd=Decimal("0.01")).propose_plan(CloudContextSelection(run_id="run_cloud")))
    with fixture.sessions() as session:
        record = session.scalar(select(AgentInteractionRecord))
        assert record is not None and record.status == "FAILED" and record.reason_code == "CLOUD_BUDGET_EXCEEDED"


def test_request_cancellation_is_terminal_and_auditable(fixture: CloudFixture) -> None:
    entered = asyncio.Event()

    class BlockingModel:
        provider_name = "fake-cloud"
        model_name = "fake-structured-v1"

        async def complete(self, request: CloudModelRequest) -> CloudModelResponse:
            del request
            entered.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    async def cancel_call() -> None:
        call = asyncio.create_task(adapter(BlockingModel(), fixture).propose_plan(CloudContextSelection(run_id="run_cloud")))  # type: ignore[arg-type]
        await entered.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(cancel_call())
    with fixture.sessions() as session:
        record = session.scalar(select(AgentInteractionRecord))
        assert record is not None and record.status == "CANCELLED" and record.reason_code == "CLOUD_CANCELLED"
        assert record.completed_at is not None


def test_cloud_requested_forbidden_capability_is_structured_then_policy_denies(fixture: CloudFixture) -> None:
    proposal = json.dumps({
        "proposal_id": "wo-proposal-1", "objective": "open external network",
        "inputs": [], "requested_capabilities": ["network.external"],
        "constraints": {"network": "full", "writable_paths": []}, "budget": {"max_wall_seconds": 10},
        "acceptance": [], "expected_outputs": [], "data_policy": {"default_classification": "LOCAL_ONLY"}, "evidence_refs": [],
    })
    result = asyncio.run(adapter(QueueCloudModel([proposal]), fixture).propose_work_order(CloudContextSelection(run_id="run_cloud")))
    budget = BudgetLimits(100, 100, 0, 100, 10)
    decision = DeterministicPolicyEngine().evaluate(PolicyRequest(
        requested_capabilities=frozenset(result.output.requested_capabilities),
        workspace_capabilities=frozenset({Capability.WORKSPACE_READ}),
        user_capabilities=frozenset({Capability.WORKSPACE_READ}), approved_capabilities=frozenset(),
        requested_budget=budget, maximum_budget=budget, data_classification=DataClassification.LOCAL_ONLY,
    ))
    assert decision.outcome is PolicyOutcome.DENY
    assert "POLICY_DENY_CAPABILITY" in decision.reason_codes


def test_cloud_accept_cannot_override_failed_hard_verification(fixture: CloudFixture) -> None:
    _, verification = fixture.verification(value=0, threshold=1)
    review = json.dumps({
        "decision": "ACCEPT", "work_order_id": "wo_cloud",
        "evidence_refs": [str(verification.verification_id)], "deficiencies": [],
        "rationale": "Cloud recommends acceptance", "requested_next_objective": None, "requested_evidence": [],
    })
    output = asyncio.run(adapter(QueueCloudModel([review]), fixture).review(CloudContextSelection(
        run_id="run_cloud", work_order_id="wo_cloud", verification_id=str(verification.verification_id),
    ))).output
    assert output.decision.value == "ACCEPT"
    with pytest.raises(TransitionPreconditionFailed):
        TransactionalTransitionService(fixture.sessions).transition_work_order(
            "wo_cloud", 1, WorkOrderState.REVIEW_READY,
            event_type="CLOUD_REVIEW_ACCEPT", actor_type="cloud_lead", actor_id="fake-cloud",
            correlation_id="att_cloud",
        )


def test_provider_timeout_persists_waiting_external(fixture: CloudFixture) -> None:
    model = QueueCloudModel([CloudProviderUnavailable("timeout")])
    with pytest.raises(CloudProviderUnavailable):
        asyncio.run(adapter(model, fixture).propose_plan(CloudContextSelection(run_id="run_cloud")))
    with fixture.sessions() as session:
        record = session.scalar(select(AgentInteractionRecord))
        assert record is not None
        assert record.status == "WAITING_EXTERNAL" and record.reason_code == "CLOUD_UNAVAILABLE"


def test_retryable_provider_failure_is_bounded_and_audited(fixture: CloudFixture) -> None:
    model = QueueCloudModel([CloudProviderUnavailable("temporary", retryable=True), plan_json()])
    result = asyncio.run(adapter(model, fixture, retry_backoff=0).propose_plan(CloudContextSelection(run_id="run_cloud")))
    assert result.output.proposal_id == "plan-proposal-1"
    assert len(model.requests) == 2
    with fixture.sessions() as session:
        record = session.scalar(select(AgentInteractionRecord))
        assert record is not None and record.status == "COMPLETED" and record.attempts == 2


def test_retryable_provider_failure_stops_at_request_budget(fixture: CloudFixture) -> None:
    model = QueueCloudModel([CloudProviderUnavailable("temporary", retryable=True)] * 2)
    with pytest.raises(CloudProviderUnavailable):
        asyncio.run(adapter(model, fixture, requests=2, retry_backoff=0).propose_plan(CloudContextSelection(run_id="run_cloud")))
    with fixture.sessions() as session:
        record = session.scalar(select(AgentInteractionRecord))
        assert record is not None and record.status == "WAITING_EXTERNAL" and record.attempts == 2


def test_configured_https_provider_is_outbound_only_without_tools_or_tracing(fixture: CloudFixture) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"id": "provider-request-1", "choices": [{"message": {"content": plan_json()}}], "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}},
            headers={"x-request-id": "request-header-1"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    model = OpenAICompatibleCloudModel(
        base_url="https://provider.example", api_key="fixture-key", model="configured-model",
        allowed_hosts=frozenset({"provider.example"}), client=client,
    )
    result = asyncio.run(adapter(model, fixture).propose_plan(CloudContextSelection(run_id="run_cloud")))
    asyncio.run(client.aclose())
    assert result.output.proposal_id == "plan-proposal-1"
    assert model.sdk_tracing_enabled is False
    assert len(captured) == 1 and captured[0].url == httpx.URL("https://provider.example/v1/chat/completions")
    body = json.loads(captured[0].content)
    assert "tools" not in body and body["store"] is False and body["stream"] is False
    assert body.get("conversation") is None and len(body["messages"]) == 2


def test_context_serializer_is_deterministic(fixture: CloudFixture) -> None:
    bundle = fixture.builder.build(run_id="run_cloud")
    assert serialize_cloud_bundle(bundle) == serialize_cloud_bundle(bundle)
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatibleCloudModel(
            base_url="http://provider.example", api_key="x", model="x",
            allowed_hosts=frozenset({"provider.example"}),
        )


def test_provider_transport_response_has_hard_byte_limit() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"x" * 65),
    ), trust_env=False)
    model = OpenAICompatibleCloudModel(
        base_url="https://provider.example", api_key="fixture-key", model="configured-model",
        allowed_hosts=frozenset({"provider.example"}), max_transport_response_bytes=64,
        client=client,
    )
    request = CloudModelRequest(
        system_prompt="structured", context_json="{}", response_schema={"type": "object"},
        response_type="Object", repair_instruction=None, max_output_tokens=10,
    )
    with pytest.raises(CloudProviderUnavailable, match="transport byte limit"):
        asyncio.run(model.complete(request))
    asyncio.run(client.aclose())


def test_provider_429_is_marked_retryable() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(429, headers={"retry-after": "2"}),
    ), trust_env=False)
    model = OpenAICompatibleCloudModel(
        base_url="https://provider.example", api_key="fixture-key", model="configured-model",
        allowed_hosts=frozenset({"provider.example"}), client=client,
    )
    request = CloudModelRequest(
        system_prompt="structured", context_json="{}", response_schema={"type": "object"},
        response_type="Object", repair_instruction=None, max_output_tokens=10,
    )
    with pytest.raises(CloudProviderUnavailable) as raised:
        asyncio.run(model.complete(request))
    assert raised.value.retryable and raised.value.retry_after_seconds == 2
    asyncio.run(client.aclose())
