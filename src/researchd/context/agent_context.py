import hashlib
import json
from researchd.context.builder import CloudContextSelection, ContextBuilder
from researchd.context.cloud_bundle import CloudContextBundle
from researchd.domain.base import DomainModel
from researchd.domain.enums import AgentTrustZone, DelegationPurpose


class AgentContextSelection(DomainModel):
    target_agent_id: str
    target_runtime_id: str
    target_trust_zone: AgentTrustZone
    purpose: DelegationPurpose
    run_id: str
    work_order_id: str | None = None
    artifact_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    verification_id: str | None = None


class AgentContextBundle(DomainModel):
    target_agent_id: str
    target_runtime_id: str
    target_trust_zone: AgentTrustZone
    purpose: DelegationPurpose
    run_id: str
    work_order_id: str | None
    selected_context: CloudContextBundle
    bundle_sha256: str


class AgentContextBuilder:
    """Build target-aware bundles while reusing the proven egress checks."""
    def __init__(self, cloud_builder: ContextBuilder) -> None:
        self.cloud_builder = cloud_builder

    def build(self, selection: AgentContextSelection) -> AgentContextBundle:
        if selection.target_trust_zone is not AgentTrustZone.EXTERNAL_CLOUD and selection.target_trust_zone is not AgentTrustZone.REMOTE_PRIVATE:
            # Until a local/private serializer is introduced, only the cloud-safe
            # compatibility path is allowed; untrusted targets never get context.
            raise PermissionError("target trust zone has no approved context policy")
        context = self.cloud_builder.build_selection(CloudContextSelection(
            run_id=selection.run_id, work_order_id=selection.work_order_id,
            artifact_ids=selection.artifact_ids, observation_ids=selection.observation_ids,
            verification_id=selection.verification_id,
        ))
        canonical = json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        return AgentContextBundle(target_agent_id=selection.target_agent_id, target_runtime_id=selection.target_runtime_id, target_trust_zone=selection.target_trust_zone, purpose=selection.purpose, run_id=selection.run_id, work_order_id=selection.work_order_id, selected_context=context, bundle_sha256=hashlib.sha256(canonical).hexdigest())
