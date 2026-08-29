import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from researchd.agents.cloud_lead import CloudLeadAdapter
from researchd.context.builder import CloudContextSelection, EgressDenied
from researchd.domain.enums import DataClassification
from researchd.models.cloud import (
    CloudBudgetExceeded,
    CloudCallBudget,
    CloudModelRequest,
    CloudModelResponse,
    CloudPricing,
    CloudProviderConfiguration,
    CloudProviderUnavailable,
    CloudSchemaInvalid,
    CloudUsage,
)
from researchd.models.openai_compatible import OpenAICompatibleCloudModel
from researchd.storage.models import (
    AgentInteractionRecord,
    AgentInvocationRecord,
    AgentRecord,
    AgentRuntimeRecord,
    CloudInteractionGovernanceRecord,
    DelegationRecord,
)
from tests.integration.test_cloud_lead import CloudFixture, QueueCloudModel, adapter, plan_json, provider_configuration


def _fixture(root: Path, name: str) -> CloudFixture:
    path = root / name
    path.mkdir()
    return CloudFixture(path)


def _assert_http_failure(status: int) -> CloudProviderUnavailable:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status)), trust_env=False,
    )
    model = OpenAICompatibleCloudModel(
        base_url="https://provider.example", api_key="fixture-key", model="configured-model",
        allowed_hosts=frozenset({"provider.example"}), client=client,
    )
    request = CloudModelRequest(
        system_prompt="structured", context_json="{}", response_schema={"type": "object"},
        response_type="Object", max_output_tokens=10,
    )
    try:
        with pytest.raises(CloudProviderUnavailable) as raised:
            asyncio.run(model.complete(request))
        return raised.value
    finally:
        asyncio.run(client.aclose())


def _assert_timeout_failure() -> CloudProviderUnavailable:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("injected timeout", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout), trust_env=False)
    model = OpenAICompatibleCloudModel(
        base_url="https://provider.example", api_key="fixture-key", model="configured-model",
        allowed_hosts=frozenset({"provider.example"}), client=client,
    )
    request = CloudModelRequest(
        system_prompt="structured", context_json="{}", response_schema={"type": "object"},
        response_type="Object", max_output_tokens=10,
    )
    try:
        with pytest.raises(CloudProviderUnavailable) as raised:
            asyncio.run(model.complete(request))
        return raised.value
    finally:
        asyncio.run(client.aclose())


def test_dq03_provider_and_egress_matrix(tmp_path: Path) -> None:
    checks = [f"DQ03-{number:02d}" for number in range(1, 13)]

    configured = _fixture(tmp_path, "configured")
    configured_model = QueueCloudModel([plan_json()])
    configured_result = asyncio.run(
        adapter(configured_model, configured).propose_plan(
            CloudContextSelection(run_id="run_cloud", work_order_id="wo_cloud"),
        ),
    )
    with configured.sessions() as session:
        configured_record = session.get(AgentInteractionRecord, configured_result.interaction_id)
        assert configured_record is not None
        configured_governance = session.get(CloudInteractionGovernanceRecord, configured_result.interaction_id)
        assert configured_governance is not None
        snapshot = CloudProviderConfiguration.model_validate(configured_governance.provider_configuration_json)
        assert configured_governance.provider_configuration_sha256 == snapshot.snapshot_sha256()
        assert snapshot.account_id and snapshot.project_id and snapshot.region
        assert snapshot.retention_policy and snapshot.privacy_mode and snapshot.training_opt_out

    classified = _fixture(tmp_path, "classified")
    public = classified.artifacts.register(
        b"public", mime_type="text/plain", artifact_type="fixture",
        classification=DataClassification.PUBLIC, producer_type="tool", producer_id="dq03",
    )
    safe = classified.artifacts.register(
        b"safe", mime_type="text/plain", artifact_type="fixture",
        classification=DataClassification.CLOUD_SAFE, producer_type="tool", producer_id="dq03",
    )
    bundle = classified.builder.build(
        run_id="run_cloud", work_order_id="wo_cloud", artifact_ids=[public.artifact_id, safe.artifact_id],
    )
    assert {item.classification for item in bundle.selected_artifacts} == {"PUBLIC", "CLOUD_SAFE"}

    private = classified.artifacts.register(
        b"private raw", mime_type="text/plain", artifact_type="fixture",
        classification=DataClassification.PROJECT_PRIVATE, producer_type="tool", producer_id="dq03",
    )
    with pytest.raises(EgressDenied):
        classified.builder.build(run_id="run_cloud", artifact_ids=[private.artifact_id])
    derived = classified.artifacts.derive(
        [private.artifact_id], b"safe aggregate", mime_type="text/plain", artifact_type="summary",
        classification=DataClassification.CLOUD_SAFE, producer="dq03-extractor", producer_version="1",
        parameters={"allowlist": ["aggregate"]},
    )
    private_bundle = classified.builder.build(run_id="run_cloud", artifact_ids=[private.artifact_id])
    assert [item.artifact_id for item in private_bundle.selected_artifacts] == [derived.artifact_id]
    assert "private raw" not in private_bundle.model_dump_json()

    forbidden_markers = []
    for classification, marker in [
        (DataClassification.LOCAL_ONLY, "DQ03-LOCAL-MARKER"),
        (DataClassification.SECRET, "DQ03-SECRET-MARKER"),
    ]:
        artifact = classified.artifacts.register(
            marker.encode(), mime_type="text/plain", artifact_type="fixture",
            classification=classification, producer_type="tool", producer_id="dq03",
        )
        with pytest.raises(EgressDenied):
            classified.builder.build(run_id="run_cloud", artifact_ids=[artifact.artifact_id])
        forbidden_markers.append(marker)
    nested = classified.artifacts.register(
        b'{"nested":{"authorization":"Bearer abc.def","path":"/host/private/data"}}',
        mime_type="application/json", artifact_type="fixture",
        classification=DataClassification.CLOUD_SAFE, producer_type="tool", producer_id="dq03",
    )
    nested_payload = classified.builder.build(run_id="run_cloud", artifact_ids=[nested.artifact_id]).model_dump_json()
    assert "abc.def" not in nested_payload and "/host/private" not in nested_payload

    malformed = _fixture(tmp_path, "malformed")
    with pytest.raises(CloudSchemaInvalid):
        asyncio.run(adapter(QueueCloudModel(["not-json", "{}"]), malformed, requests=2).propose_plan(CloudContextSelection(run_id="run_cloud")))

    retrying = _fixture(tmp_path, "retrying")
    retry_model = QueueCloudModel([CloudProviderUnavailable("temporary", retryable=True), plan_json()])
    asyncio.run(adapter(retry_model, retrying, retry_backoff=0).propose_plan(CloudContextSelection(run_id="run_cloud")))
    assert len(retry_model.requests) == 2
    assert _assert_http_failure(429).retryable and _assert_http_failure(503).retryable
    assert _assert_timeout_failure().retryable
    assert not _assert_http_failure(400).retryable

    budgeted = _fixture(tmp_path, "budgeted")
    budget_model = QueueCloudModel(
        [plan_json()], usage=CloudUsage(prompt_tokens=10_000, completion_tokens=10_000, total_tokens=20_000),
    )
    with pytest.raises(CloudBudgetExceeded):
        asyncio.run(adapter(budget_model, budgeted, max_cost_usd=Decimal("0.000001")).propose_plan(CloudContextSelection(run_id="run_cloud")))

    cancelled = _fixture(tmp_path, "cancelled")
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
        call = asyncio.create_task(adapter(BlockingModel(), cancelled).propose_plan(CloudContextSelection(run_id="run_cloud")))  # type: ignore[arg-type]
        await entered.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(cancel_call())
    with cancelled.sessions() as session:
        cancelled_record = session.scalar(select(AgentInteractionRecord))
        assert cancelled_record is not None and cancelled_record.status == "CANCELLED"

    unavailable = _fixture(tmp_path, "unavailable")
    unavailable_model = QueueCloudModel([CloudProviderUnavailable("offline")])
    with pytest.raises(CloudProviderUnavailable):
        asyncio.run(adapter(unavailable_model, unavailable).propose_plan(CloudContextSelection(run_id="run_cloud")))
    assert len(unavailable_model.requests) == 1
    with unavailable.sessions() as session:
        unavailable_record = session.scalar(select(AgentInteractionRecord))
        assert unavailable_record is not None and unavailable_record.provider == "fake-cloud"
        assert unavailable_record.status == "WAITING_EXTERNAL"

    mismatch = _fixture(tmp_path, "mismatch")
    mismatch_model = QueueCloudModel([plan_json()])
    mismatched_configuration = provider_configuration(mismatch_model).model_copy(update={"provider": "other-provider"})
    with pytest.raises(ValueError, match="does not match"):
        CloudLeadAdapter(
            mismatch_model, mismatch.sessions, mismatch.builder,
            configuration=mismatched_configuration, budget=CloudCallBudget(max_requests=3),
            pricing=CloudPricing(prompt_usd_per_million=Decimal("0"), completion_usd_per_million=Decimal("0")),
        )
    assert mismatch_model.requests == []

    attributed = _fixture(tmp_path, "attributed")
    now = datetime.now(UTC)
    with attributed.sessions.begin() as session:
        session.add(AgentRecord(
            agent_id="agent_dq03", display_name="DQ03 Agent", roles_json=["cloud_lead"], skills_json=[],
            trust_zone="CLOUD", constraints_json=[], labels_json={}, max_parallel_delegations=1,
            enabled=True, profile_version=1, version=1, created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(AgentRuntimeRecord(
            runtime_id="runtime_dq03", agent_id="agent_dq03", adapter_kind="CLOUD_LEAD",
            runtime_name="DQ03", endpoint_ref=None, framework=None, model_provider="fake-cloud",
            model_name="fake-structured-v1", protocols_json=[], metadata_json={}, enabled=True,
            last_heartbeat_at=None, lease_expires_at=None, runtime_lease_id=None, lease_owner_id=None,
            lease_acquired_at=None, version=1, created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(DelegationRecord(
            delegation_id="delegation_dq03", run_id="run_cloud", work_order_id="wo_cloud", purpose="PLAN",
            required_roles_json=[], required_skills_json=[], required_trust_zones_json=[],
            assigned_agent_id="agent_dq03", assigned_runtime_id="runtime_dq03", agent_profile_version=1,
            agent_snapshot_json={}, assignment_sha256="a" * 64, state="ASSIGNED",
            idempotency_key="dq03-attribution", completed_at=None, version=1, created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(AgentInvocationRecord(
            invocation_id="invocation_dq03", delegation_id="delegation_dq03", run_id="run_cloud",
            work_order_id="wo_cloud", attempt_id=None, workspace_grant_id=None,
            agent_id="agent_dq03", runtime_id="runtime_dq03", purpose="PLAN", status="RUNNING",
            input_sha256="b" * 64, context_bundle_sha256=None, context_bundle_json=None,
            output_type=None, output_json=None, reason_code=None, runtime_lease_id=None,
            external_invocation_id=None, dispatched_at=None, external_started_at=None,
            reconciliation_requested_at=None, last_reconciled_at=None, cancel_requested_at=None,
            deadline_at=None, created_at=now, completed_at=None,
        ))
    attributed_result = asyncio.run(
        adapter(QueueCloudModel([plan_json()]), attributed).propose_plan(
            CloudContextSelection(
                run_id="run_cloud", work_order_id="wo_cloud", invocation_id="invocation_dq03",
            ),
        ),
    )
    with attributed.sessions() as session:
        attributed_record = session.get(AgentInteractionRecord, attributed_result.interaction_id)
        assert attributed_record is not None
        assert (attributed_record.run_id, attributed_record.work_order_id, attributed_record.invocation_id) == (
            "run_cloud", "wo_cloud", "invocation_dq03",
        )

    report_path = os.environ.get("DQ03_REPORT")
    if report_path:
        report = {
            "gate_id": "DQ03",
            "checks": checks,
            "provider_configuration_id": configured_governance.provider_configuration_id,
            "provider_configuration_sha256": configured_governance.provider_configuration_sha256,
            "forbidden_markers_observed": False,
            "retry_request_count": len(retry_model.requests),
            "unauthorized_fallback_count": 0,
            "terminal_cancellation_count": 1,
            "attribution": {"run_id": "run_cloud", "work_order_id": "wo_cloud", "invocation_id": "invocation_dq03"},
            "accounting": {
                "prompt_tokens": configured_record.prompt_tokens,
                "completion_tokens": configured_record.completion_tokens,
                "total_tokens": configured_record.total_tokens,
                "cost_usd": configured_record.cost_usd,
            },
        }
        Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
