import hashlib
import json
from researchd.context.builder import CloudContextSelection, ContextBuilder
from researchd.context.cloud_bundle import CloudContextBundle
from researchd.domain.base import DomainModel
from researchd.domain.enums import AgentTrustZone, DataClassification, DelegationPurpose


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
    previous_bundle_sha256: str | None = None


class AgentContextPolicy(DomainModel):
    target_trust_zone: AgentTrustZone
    allowed_classifications: frozenset[DataClassification]
    redaction_required: bool = True


class AgentContextBundle(DomainModel):
    target_agent_id: str
    target_runtime_id: str
    target_trust_zone: AgentTrustZone
    purpose: DelegationPurpose
    run_id: str
    work_order_id: str | None
    selected_context: CloudContextBundle
    bundle_sha256: str
    policy: AgentContextPolicy
    rebuilt_from_previous_bundle: bool = False


class AgentContextBuilder:
    """Build target-aware bundles while reusing the proven egress checks."""
    def __init__(self, cloud_builder: ContextBuilder) -> None:
        self.cloud_builder = cloud_builder

    @staticmethod
    def policy_for(target: AgentTrustZone) -> AgentContextPolicy:
        policies = {
            AgentTrustZone.LOCAL_PRIVATE: frozenset({DataClassification.PUBLIC, DataClassification.CLOUD_SAFE, DataClassification.PROJECT_PRIVATE, DataClassification.LOCAL_ONLY}),
            AgentTrustZone.REMOTE_PRIVATE: frozenset({DataClassification.PUBLIC, DataClassification.CLOUD_SAFE, DataClassification.PROJECT_PRIVATE}),
            AgentTrustZone.EXTERNAL_CLOUD: frozenset({DataClassification.PUBLIC, DataClassification.CLOUD_SAFE}),
            AgentTrustZone.EXTERNAL_UNTRUSTED: frozenset({DataClassification.PUBLIC}),
        }
        return AgentContextPolicy(target_trust_zone=target, allowed_classifications=policies[target])

    def build(self, selection: AgentContextSelection) -> AgentContextBundle:
        policy = self.policy_for(selection.target_trust_zone)
        context = self.cloud_builder.build(
            run_id=selection.run_id, work_order_id=selection.work_order_id,
            artifact_ids=selection.artifact_ids, observation_ids=selection.observation_ids,
            verification_id=selection.verification_id,
            allowed_classifications=policy.allowed_classifications,
        )
        canonical = json.dumps({"target_agent_id": selection.target_agent_id, "target_runtime_id": selection.target_runtime_id, "target_trust_zone": selection.target_trust_zone.value, "purpose": selection.purpose.value, "context": context.model_dump(mode="json")}, sort_keys=True, separators=(",", ":")).encode()
        return AgentContextBundle(target_agent_id=selection.target_agent_id, target_runtime_id=selection.target_runtime_id, target_trust_zone=selection.target_trust_zone, purpose=selection.purpose, run_id=selection.run_id, work_order_id=selection.work_order_id, selected_context=context, bundle_sha256=hashlib.sha256(canonical).hexdigest(), policy=policy, rebuilt_from_previous_bundle=selection.previous_bundle_sha256 is not None)
