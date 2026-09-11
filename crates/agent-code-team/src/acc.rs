//! ACC/0.1 collaboration contracts for the Rust-v2 team runtime.
//!
//! These types are deliberately separate from the scheduler's execution
//! mechanics.  They encode what is authoritative, what may be shared, and
//! what an Agent is merely allowed to suggest.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const ACC_VERSION: &str = "0.1";
/// Checked-in drift sentinel for the generated JSON Schema. The schema is
/// generated directly from the authoritative Rust DTOs below.
pub const ACC_WIRE_SCHEMA_SHA256: &str =
    "19cee109cd16d036fd63b7726e076cc795fbd22413de284050576dadb4ffe53c";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AccWireContractBundle {
    pub task_contract: TaskContract,
    pub task_graph_proposal: TaskGraphProposal,
    pub runtime_descriptor: RuntimeDescriptor,
    pub collaboration_event: CollaborationEvent,
    pub context_manifest: ContextManifest,
    pub finding: Finding,
    pub decision: Decision,
    pub artifact_manifest: ArtifactManifest,
}

/// Deterministic JSON Schema exporter for the ACC wire contract.
pub fn acc_wire_schema_json() -> String {
    serde_json::to_string_pretty(&schemars::schema_for!(AccWireContractBundle))
        .expect("schema is serializable")
}

pub fn acc_wire_schema_sha256() -> String {
    format!("{:x}", Sha256::digest(acc_wire_schema_json().as_bytes()))
}

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema, Ord, PartialOrd,
)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Authority {
    SystemPolicy,
    HumanDirective,
    TaskContract,
    AcceptedDecision,
    VerifiedEvidence,
    PeerAdvice,
    Observation,
    ExternalUntrusted,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TrustStatus {
    Unverified,
    Supported,
    Disputed,
    Verified,
    Rejected,
    Superseded,
    Revoked,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TaskRole {
    Coordinator,
    Implementer,
    Helper,
    Reviewer,
    Critic,
}

/// A capability describes aptitude only. It is never a grant.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema, Ord, PartialOrd)]
#[serde(transparent)]
pub struct AgentCapability(pub String);

/// A trusted grant is supplied by the runtime/controller, never Agent text.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema, Ord, PartialOrd)]
#[serde(transparent)]
pub struct TrustedCapability(pub String);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ObservabilityLevel {
    O0,
    O1,
    O2,
    O3,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RuntimeFeatures {
    pub streaming: bool,
    pub cancellation: bool,
    pub continuation: bool,
    pub midrun_input: bool,
    pub structured_output: bool,
    pub worktree_isolation: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RuntimeDescriptor {
    pub runtime_id: String,
    pub agent_id: String,
    pub adapter_kind: String,
    pub runtime_version: String,
    pub agent_capabilities: BTreeSet<AgentCapability>,
    pub observability_level: ObservabilityLevel,
    pub features: RuntimeFeatures,
    pub protocols: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AcceptanceCriterion {
    pub criterion_id: String,
    pub requirement: String,
    pub independent_review: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TaskContract {
    pub task_id: String,
    pub objective: String,
    pub required_agent_capabilities: BTreeSet<AgentCapability>,
    /// Requested grants remain requests; the trusted controller decides them.
    pub requested_capabilities: BTreeSet<TrustedCapability>,
    pub expected_outputs: Vec<String>,
    pub acceptance: Vec<AcceptanceCriterion>,
    pub idempotency_key: String,
}

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema, Ord, PartialOrd,
)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DependencyKind {
    DependsOn,
    Blocks,
    ProducesFor,
    RelatedTo,
}

impl DependencyKind {
    fn affects_readiness(self) -> bool {
        matches!(self, Self::DependsOn | Self::Blocks)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TaskDependency {
    pub predecessor: String,
    pub successor: String,
    pub kind: DependencyKind,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TaskGraphProposal {
    pub proposal_id: String,
    pub tasks: Vec<TaskContract>,
    pub dependencies: Vec<TaskDependency>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphError {
    DuplicateTask(String),
    UnknownTask(String),
    SelfEdge(String),
    DuplicateEdge(String, String, DependencyKind),
    Cycle,
}

/// Validated task DAG. Only `DEPENDS_ON` and `BLOCKS` affect readiness.
#[derive(Debug, Clone)]
pub struct TaskGraph {
    tasks: BTreeMap<String, TaskContract>,
    dependencies: Vec<TaskDependency>,
}

impl TaskGraph {
    pub fn validate(proposal: TaskGraphProposal) -> Result<Self, GraphError> {
        let mut tasks = BTreeMap::new();
        for task in proposal.tasks {
            if tasks.insert(task.task_id.clone(), task).is_some() {
                return Err(GraphError::DuplicateTask("duplicate task id".into()));
            }
        }
        let mut edges = BTreeSet::new();
        for edge in &proposal.dependencies {
            if !tasks.contains_key(&edge.predecessor) {
                return Err(GraphError::UnknownTask(edge.predecessor.clone()));
            }
            if !tasks.contains_key(&edge.successor) {
                return Err(GraphError::UnknownTask(edge.successor.clone()));
            }
            if edge.predecessor == edge.successor {
                return Err(GraphError::SelfEdge(edge.predecessor.clone()));
            }
            if !edges.insert((edge.predecessor.clone(), edge.successor.clone(), edge.kind)) {
                return Err(GraphError::DuplicateEdge(
                    edge.predecessor.clone(),
                    edge.successor.clone(),
                    edge.kind,
                ));
            }
        }
        let graph = Self {
            tasks,
            dependencies: proposal.dependencies,
        };
        if graph.has_cycle() {
            return Err(GraphError::Cycle);
        }
        Ok(graph)
    }

    pub fn tasks(&self) -> &BTreeMap<String, TaskContract> {
        &self.tasks
    }

    pub fn dependencies(&self) -> &[TaskDependency] {
        &self.dependencies
    }

    pub fn ready_tasks(&self, completed: &BTreeSet<String>) -> Vec<&TaskContract> {
        self.tasks
            .values()
            .filter(|task| !completed.contains(&task.task_id))
            .filter(|task| {
                self.dependencies
                    .iter()
                    .filter(|d| d.successor == task.task_id && d.kind.affects_readiness())
                    .all(|d| completed.contains(&d.predecessor))
            })
            .collect()
    }

    fn has_cycle(&self) -> bool {
        let mut indegree: BTreeMap<&str, usize> =
            self.tasks.keys().map(|k| (k.as_str(), 0)).collect();
        let mut outgoing: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
        for d in &self.dependencies {
            if d.kind.affects_readiness() {
                *indegree.get_mut(d.successor.as_str()).expect("validated") += 1;
                outgoing
                    .entry(d.predecessor.as_str())
                    .or_default()
                    .push(d.successor.as_str());
            }
        }
        let mut queue: VecDeque<&str> = indegree
            .iter()
            .filter_map(|(id, degree)| (*degree == 0).then_some(*id))
            .collect();
        let mut seen = 0usize;
        while let Some(id) = queue.pop_front() {
            seen += 1;
            for next in outgoing.get(id).into_iter().flatten() {
                let degree = indegree.get_mut(next).expect("validated");
                *degree -= 1;
                if *degree == 0 {
                    queue.push_back(next);
                }
            }
        }
        seen != self.tasks.len()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Classification {
    Public,
    Internal,
    Confidential,
    Secret,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Provenance {
    pub source: String,
    pub source_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ContextItem {
    pub item_id: String,
    pub content: String,
    pub authority: Authority,
    pub trust: TrustStatus,
    pub classification: Classification,
    pub provenance: Provenance,
    pub forwardable: bool,
}

impl ContextItem {
    pub fn shared(&self) -> bool {
        self.forwardable
            && !matches!(self.classification, Classification::Secret)
            && !contains_private_runtime_material(&self.content)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ContextManifest {
    pub manifest_id: String,
    pub task_id: String,
    pub items: Vec<ContextItem>,
    pub manifest_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Finding {
    pub finding_id: String,
    pub task_id: String,
    pub statement: String,
    pub authority: Authority,
    pub trust: TrustStatus,
    pub provenance: Provenance,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Decision {
    pub decision_id: String,
    pub task_id: String,
    pub outcome: String,
    pub authority: Authority,
    pub evidence_refs: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ArtifactManifest {
    pub artifact_id: String,
    pub task_id: String,
    pub version: u32,
    pub sha256: String,
    pub immutable: bool,
}

impl ContextManifest {
    pub fn build(manifest_id: String, task_id: String, mut items: Vec<ContextItem>) -> Self {
        items.retain(ContextItem::shared);
        items.sort_by(|a, b| (a.authority, &a.item_id).cmp(&(b.authority, &b.item_id)));
        let digest = manifest_digest(&task_id, &items);
        Self {
            manifest_id,
            task_id,
            items,
            manifest_sha256: digest,
        }
    }

    pub fn verify_hash(&self) -> bool {
        self.manifest_sha256 == manifest_digest(&self.task_id, &self.items)
    }
}

fn manifest_digest(task_id: &str, items: &[ContextItem]) -> String {
    let canonical = serde_json::to_vec(&(task_id, items)).expect("serializable context");
    format!("{:x}", Sha256::digest(canonical))
}

fn contains_private_runtime_material(content: &str) -> bool {
    let lowered = content.to_ascii_lowercase();
    [
        "chain-of-thought",
        "system prompt",
        "scratchpad",
        "secret",
        "credential",
        "ssh key",
    ]
    .iter()
    .any(|marker| lowered.contains(marker))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AccTaskState {
    Proposed,
    Ready,
    Assigned,
    ResultSubmitted,
    Reviewing,
    Accepted,
    RevisionRequired,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TaskAssignment {
    pub task_id: String,
    pub agent_id: String,
    pub runtime_id: String,
    pub role: TaskRole,
    pub trusted_grants: BTreeSet<TrustedCapability>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case", tag = "kind", content = "payload")]
pub enum CollaborationPayload {
    TaskStatus {
        state: AccTaskState,
    },
    TaskResultSubmitted {
        artifact_id: String,
        artifact_sha256: String,
        artifact_version: u32,
    },
    HelpRequest {
        requested_capability: AgentCapability,
        question: String,
    },
    HelpResponse {
        response: String,
    },
    ReviewRequest {
        artifact_id: String,
        artifact_sha256: String,
        artifact_version: u32,
    },
    ReviewResponse {
        approved: bool,
        artifact_id: String,
        artifact_sha256: String,
        artifact_version: u32,
    },
    FindingPublish {
        finding_id: String,
        statement: String,
    },
    ContextRequest {
        purpose: String,
    },
    ContextPublish {
        manifest_id: String,
    },
    ArtifactPublish {
        artifact_id: String,
        artifact_sha256: String,
        artifact_version: u32,
    },
    ReplanRequest {
        reason: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CollaborationEvent {
    pub event_id: String,
    pub task_id: String,
    /// Injected by the runtime binding; callers must not supply an arbitrary value.
    pub actor_id: String,
    pub runtime_id: String,
    pub correlation_id: String,
    pub causation_id: Option<String>,
    pub authority: Authority,
    pub payload: CollaborationPayload,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BrokerError {
    EmptyId,
    UnknownTask(String),
    ActorBinding,
    DuplicateEvent,
    AuthorityEscalation,
    InvalidTransition,
    SelfReview,
    ArtifactMismatch,
}

/// Canonical ACC state. A persistence adapter stores this state; this type
/// centralizes transitions so text, advice, and runtime completion cannot
/// mutate acceptance by implication.
#[derive(Debug, Clone)]
pub struct AccState {
    pub graph: TaskGraph,
    states: BTreeMap<String, AccTaskState>,
    assignments: BTreeMap<String, TaskAssignment>,
    manifests: BTreeMap<String, ContextManifest>,
    artifacts: BTreeMap<String, (String, u32)>,
    events: Vec<(u64, CollaborationEvent)>,
    seen_event_ids: BTreeSet<String>,
    next_sequence: u64,
}

impl AccState {
    pub fn new(graph: TaskGraph) -> Self {
        let states = graph
            .tasks()
            .keys()
            .map(|id| (id.clone(), AccTaskState::Proposed))
            .collect();
        Self {
            graph,
            states,
            assignments: BTreeMap::new(),
            manifests: BTreeMap::new(),
            artifacts: BTreeMap::new(),
            events: Vec::new(),
            seen_event_ids: BTreeSet::new(),
            next_sequence: 1,
        }
    }

    pub fn state(&self, task_id: &str) -> Option<AccTaskState> {
        self.states.get(task_id).copied()
    }
    pub fn events(&self) -> &[(u64, CollaborationEvent)] {
        &self.events
    }
    pub fn manifest(&self, id: &str) -> Option<&ContextManifest> {
        self.manifests.get(id)
    }
    pub fn assignment(&self, task_id: &str) -> Option<&TaskAssignment> {
        self.assignments.get(task_id)
    }

    pub fn mark_ready(&mut self, completed: &BTreeSet<String>) {
        for task in self.graph.ready_tasks(completed) {
            if self.state(&task.task_id) == Some(AccTaskState::Proposed) {
                self.states
                    .insert(task.task_id.clone(), AccTaskState::Ready);
            }
        }
    }

    pub fn assign(&mut self, assignment: TaskAssignment) -> Result<(), BrokerError> {
        if self.state(&assignment.task_id) != Some(AccTaskState::Ready) {
            return Err(BrokerError::InvalidTransition);
        }
        self.states
            .insert(assignment.task_id.clone(), AccTaskState::Assigned);
        self.assignments
            .insert(assignment.task_id.clone(), assignment);
        Ok(())
    }

    pub fn persist_manifest(&mut self, manifest: ContextManifest) -> Result<(), BrokerError> {
        if !manifest.verify_hash() || !self.states.contains_key(&manifest.task_id) {
            return Err(BrokerError::InvalidTransition);
        }
        self.manifests
            .insert(manifest.manifest_id.clone(), manifest);
        Ok(())
    }

    /// Ingest an event through the runtime's authenticated actor binding.
    pub fn ingest(
        &mut self,
        bound_actor: &str,
        bound_runtime: &str,
        mut event: CollaborationEvent,
    ) -> Result<u64, BrokerError> {
        if event.event_id.is_empty() || event.task_id.is_empty() {
            return Err(BrokerError::EmptyId);
        }
        if !self.states.contains_key(&event.task_id) {
            return Err(BrokerError::UnknownTask(event.task_id));
        }
        if event.actor_id != bound_actor || event.runtime_id != bound_runtime {
            return Err(BrokerError::ActorBinding);
        }
        if self.seen_event_ids.contains(&event.event_id) {
            return Err(BrokerError::DuplicateEvent);
        }
        // The authenticated runtime is an observation source, never a
        // self-declared human/controller. A peer response is even narrower.
        event.authority = if matches!(event.payload, CollaborationPayload::HelpResponse { .. }) {
            Authority::PeerAdvice
        } else {
            Authority::Observation
        };
        match &event.payload {
            CollaborationPayload::ArtifactPublish {
                artifact_id,
                artifact_sha256,
                artifact_version,
            }
            | CollaborationPayload::TaskResultSubmitted {
                artifact_id,
                artifact_sha256,
                artifact_version,
            } => {
                self.artifacts.insert(
                    artifact_id.clone(),
                    (artifact_sha256.clone(), *artifact_version),
                );
                if matches!(
                    event.payload,
                    CollaborationPayload::TaskResultSubmitted { .. }
                ) {
                    self.states
                        .insert(event.task_id.clone(), AccTaskState::ResultSubmitted);
                }
            }
            CollaborationPayload::ReviewRequest { .. } => {
                if self.state(&event.task_id) != Some(AccTaskState::ResultSubmitted) {
                    return Err(BrokerError::InvalidTransition);
                }
                self.states
                    .insert(event.task_id.clone(), AccTaskState::Reviewing);
            }
            CollaborationPayload::ReviewResponse {
                approved,
                artifact_id,
                artifact_sha256,
                artifact_version,
            } => {
                let executor = self
                    .assignments
                    .get(&event.task_id)
                    .map(|a| a.agent_id.as_str());
                if executor == Some(bound_actor) {
                    return Err(BrokerError::SelfReview);
                }
                if self.artifacts.get(artifact_id)
                    != Some(&(artifact_sha256.clone(), *artifact_version))
                {
                    self.append_audit_event(event);
                    return Err(BrokerError::ArtifactMismatch);
                }
                self.states.insert(
                    event.task_id.clone(),
                    if *approved {
                        AccTaskState::Accepted
                    } else {
                        AccTaskState::RevisionRequired
                    },
                );
            }
            _ => {}
        }
        Ok(self.append_audit_event(event))
    }

    fn append_audit_event(&mut self, event: CollaborationEvent) -> u64 {
        self.seen_event_ids.insert(event.event_id.clone());
        let sequence = self.next_sequence;
        self.next_sequence += 1;
        self.events.push((sequence, event));
        sequence
    }
}

/// Deterministically select an eligible runtime. Trusted grants are supplied
/// separately by the controller and cannot be obtained from capabilities.
pub fn select_assignment(
    task: &TaskContract,
    role: TaskRole,
    runtimes: &[RuntimeDescriptor],
    trusted_grants: BTreeSet<TrustedCapability>,
) -> Option<TaskAssignment> {
    runtimes
        .iter()
        .filter(|runtime| {
            task.required_agent_capabilities
                .is_subset(&runtime.agent_capabilities)
        })
        .min_by_key(|runtime| (&runtime.agent_id, &runtime.runtime_id))
        .map(|runtime| TaskAssignment {
            task_id: task.task_id.clone(),
            agent_id: runtime.agent_id.clone(),
            runtime_id: runtime.runtime_id.clone(),
            role,
            trusted_grants,
        })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeSnapshot {
    pub runtime_id: String,
    pub alive: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RuntimeEvent {
    Status(String),
    Artifact { artifact_id: String, sha256: String },
    Completed,
}

/// Native/external runtime adapter boundary. `Completed` is intentionally a
/// runtime fact; acceptance remains a broker transition after review.
pub trait RuntimeAdapter {
    fn describe(&self) -> RuntimeDescriptor;
    fn start(&mut self, task_id: &str, manifest: &ContextManifest) -> Result<(), String>;
    fn snapshot(&self) -> RuntimeSnapshot;
    fn poll_events(&mut self) -> Vec<RuntimeEvent>;
    fn send_input(&mut self, input: &str) -> Result<(), String>;
    fn cancel(&mut self) -> Result<(), String>;
    fn collect_artifacts(&self) -> Vec<(String, String)>;
    fn health(&self) -> bool;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RuntimeOperationError {
    Unsupported(&'static str),
    Adapter(String),
}

/// Feature negotiation is enforced by the caller-facing adapter surface, not
/// by an Agent's textual self-description.
pub fn send_runtime_input<A: RuntimeAdapter>(
    adapter: &mut A,
    input: &str,
) -> Result<(), RuntimeOperationError> {
    if !adapter.describe().features.midrun_input {
        return Err(RuntimeOperationError::Unsupported("midrun_input"));
    }
    adapter
        .send_input(input)
        .map_err(RuntimeOperationError::Adapter)
}

pub fn cancel_runtime<A: RuntimeAdapter>(adapter: &mut A) -> Result<(), RuntimeOperationError> {
    if !adapter.describe().features.cancellation {
        return Err(RuntimeOperationError::Unsupported("cancellation"));
    }
    adapter.cancel().map_err(RuntimeOperationError::Adapter)
}

/// Transport-neutral collaboration action facade. It creates typed events,
/// then routes them through the broker's authenticated binding.
pub struct CollaborationTools<'a> {
    pub state: &'a mut AccState,
    pub actor_id: &'a str,
    pub runtime_id: &'a str,
}

impl<'a> CollaborationTools<'a> {
    fn publish(
        &mut self,
        event_id: String,
        task_id: String,
        correlation_id: String,
        payload: CollaborationPayload,
    ) -> Result<u64, BrokerError> {
        self.state.ingest(
            self.actor_id,
            self.runtime_id,
            CollaborationEvent {
                event_id,
                task_id,
                actor_id: self.actor_id.into(),
                runtime_id: self.runtime_id.into(),
                correlation_id,
                causation_id: None,
                authority: Authority::Observation,
                payload,
            },
        )
    }
    pub fn request_help(
        &mut self,
        event_id: String,
        task_id: String,
        correlation_id: String,
        capability: AgentCapability,
        question: String,
    ) -> Result<u64, BrokerError> {
        self.publish(
            event_id,
            task_id,
            correlation_id,
            CollaborationPayload::HelpRequest {
                requested_capability: capability,
                question,
            },
        )
    }
    pub fn respond_help(
        &mut self,
        event_id: String,
        task_id: String,
        correlation_id: String,
        response: String,
    ) -> Result<u64, BrokerError> {
        self.publish(
            event_id,
            task_id,
            correlation_id,
            CollaborationPayload::HelpResponse { response },
        )
    }
    pub fn publish_artifact_ref(
        &mut self,
        event_id: String,
        task_id: String,
        correlation_id: String,
        artifact_id: String,
        artifact_sha256: String,
    ) -> Result<u64, BrokerError> {
        self.publish(
            event_id,
            task_id,
            correlation_id,
            CollaborationPayload::ArtifactPublish {
                artifact_id,
                artifact_sha256,
                artifact_version: 1,
            },
        )
    }
    pub fn request_review(
        &mut self,
        event_id: String,
        task_id: String,
        correlation_id: String,
        artifact_id: String,
        artifact_sha256: String,
    ) -> Result<u64, BrokerError> {
        self.publish(
            event_id,
            task_id,
            correlation_id,
            CollaborationPayload::ReviewRequest {
                artifact_id,
                artifact_sha256,
                artifact_version: 1,
            },
        )
    }
    pub fn publish_status(
        &mut self,
        event_id: String,
        task_id: String,
        correlation_id: String,
        state: AccTaskState,
    ) -> Result<u64, BrokerError> {
        self.publish(
            event_id,
            task_id,
            correlation_id,
            CollaborationPayload::TaskStatus { state },
        )
    }
    pub fn publish_finding(
        &mut self,
        event_id: String,
        task_id: String,
        correlation_id: String,
        finding_id: String,
        statement: String,
    ) -> Result<u64, BrokerError> {
        self.publish(
            event_id,
            task_id,
            correlation_id,
            CollaborationPayload::FindingPublish {
                finding_id,
                statement,
            },
        )
    }
    pub fn request_context(
        &mut self,
        event_id: String,
        task_id: String,
        correlation_id: String,
        purpose: String,
    ) -> Result<u64, BrokerError> {
        self.publish(
            event_id,
            task_id,
            correlation_id,
            CollaborationPayload::ContextRequest { purpose },
        )
    }
    pub fn request_replan(
        &mut self,
        event_id: String,
        task_id: String,
        correlation_id: String,
        reason: String,
    ) -> Result<u64, BrokerError> {
        self.publish(
            event_id,
            task_id,
            correlation_id,
            CollaborationPayload::ReplanRequest { reason },
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn task(id: &str) -> TaskContract {
        TaskContract {
            task_id: id.into(),
            objective: id.into(),
            required_agent_capabilities: BTreeSet::new(),
            requested_capabilities: BTreeSet::new(),
            expected_outputs: vec!["artifact".into()],
            acceptance: vec![AcceptanceCriterion {
                criterion_id: "ok".into(),
                requirement: "ok".into(),
                independent_review: false,
            }],
            idempotency_key: format!("key-{id}"),
        }
    }

    #[test]
    fn strict_contract_rejects_unknown_control_field() {
        let json = r#"{"task_id":"a","objective":"a","required_agent_capabilities":[],"requested_capabilities":[],"expected_outputs":["x"],"acceptance":[],"idempotency_key":"k","accepted":true}"#;
        assert!(serde_json::from_str::<TaskContract>(json).is_err());
    }

    #[test]
    fn generated_wire_schema_matches_checked_in_drift_sentinel() {
        assert_eq!(acc_wire_schema_sha256(), ACC_WIRE_SCHEMA_SHA256);
        assert!(acc_wire_schema_json().contains("AccWireContractBundle"));
    }

    #[test]
    fn graph_rejects_cycle_and_returns_ready_tasks() {
        let graph = TaskGraph::validate(TaskGraphProposal {
            proposal_id: "p".into(),
            tasks: vec![task("a"), task("b")],
            dependencies: vec![TaskDependency {
                predecessor: "a".into(),
                successor: "b".into(),
                kind: DependencyKind::DependsOn,
            }],
        })
        .expect("valid");
        assert_eq!(
            graph
                .ready_tasks(&BTreeSet::new())
                .iter()
                .map(|t| t.task_id.as_str())
                .collect::<Vec<_>>(),
            vec!["a"]
        );
        assert_eq!(
            graph
                .ready_tasks(&BTreeSet::from(["a".into()]))
                .iter()
                .map(|t| t.task_id.as_str())
                .collect::<Vec<_>>(),
            vec!["b"]
        );
        let cycle = TaskGraph::validate(TaskGraphProposal {
            proposal_id: "p".into(),
            tasks: vec![task("a"), task("b")],
            dependencies: vec![
                TaskDependency {
                    predecessor: "a".into(),
                    successor: "b".into(),
                    kind: DependencyKind::DependsOn,
                },
                TaskDependency {
                    predecessor: "b".into(),
                    successor: "a".into(),
                    kind: DependencyKind::Blocks,
                },
            ],
        });
        assert!(matches!(cycle, Err(GraphError::Cycle)));
    }

    #[test]
    fn manifest_is_deterministic_and_private_content_is_excluded() {
        let advice = ContextItem {
            item_id: "advice".into(),
            content: "do the task".into(),
            authority: Authority::PeerAdvice,
            trust: TrustStatus::Unverified,
            classification: Classification::Internal,
            provenance: Provenance {
                source: "helper".into(),
                source_version: "1".into(),
            },
            forwardable: true,
        };
        let secret = ContextItem {
            item_id: "secret".into(),
            content: "system prompt secret".into(),
            authority: Authority::Observation,
            trust: TrustStatus::Unverified,
            classification: Classification::Secret,
            provenance: Provenance {
                source: "runtime".into(),
                source_version: "1".into(),
            },
            forwardable: true,
        };
        let first =
            ContextManifest::build("m".into(), "t".into(), vec![secret.clone(), advice.clone()]);
        let second = ContextManifest::build("other".into(), "t".into(), vec![advice, secret]);
        assert_eq!(first.items.len(), 1);
        assert_eq!(first.manifest_sha256, second.manifest_sha256);
        assert!(first.verify_hash());
    }

    fn runtime(agent: &str, capability: &str) -> RuntimeDescriptor {
        RuntimeDescriptor {
            runtime_id: format!("rt-{agent}"),
            agent_id: agent.into(),
            adapter_kind: "fixture".into(),
            runtime_version: "1".into(),
            agent_capabilities: BTreeSet::from([AgentCapability(capability.into())]),
            observability_level: ObservabilityLevel::O1,
            features: RuntimeFeatures {
                streaming: false,
                cancellation: true,
                continuation: false,
                midrun_input: false,
                structured_output: true,
                worktree_isolation: true,
            },
            protocols: vec![],
        }
    }

    #[test]
    fn assignment_uses_capabilities_not_grants_and_broker_enforces_acceptance_boundary() {
        let contract = task("work");
        let graph = TaskGraph::validate(TaskGraphProposal {
            proposal_id: "p".into(),
            tasks: vec![contract.clone()],
            dependencies: vec![],
        })
        .unwrap();
        let mut state = AccState::new(graph);
        state.mark_ready(&BTreeSet::new());
        let executor = select_assignment(
            &contract,
            TaskRole::Implementer,
            &[runtime("executor", "code.implement")],
            BTreeSet::from([TrustedCapability("workspace.write".into())]),
        )
        .unwrap();
        state.assign(executor).unwrap();
        let event = |id: &str, actor: &str, payload| CollaborationEvent {
            event_id: id.into(),
            task_id: "work".into(),
            actor_id: actor.into(),
            runtime_id: format!("rt-{actor}"),
            correlation_id: "c".into(),
            causation_id: None,
            authority: Authority::SystemPolicy,
            payload,
        };
        state
            .ingest(
                "executor",
                "rt-executor",
                event(
                    "artifact",
                    "executor",
                    CollaborationPayload::ArtifactPublish {
                        artifact_id: "a".into(),
                        artifact_sha256: "hash".into(),
                        artifact_version: 1,
                    },
                ),
            )
            .unwrap();
        state
            .ingest(
                "executor",
                "rt-executor",
                event(
                    "result",
                    "executor",
                    CollaborationPayload::TaskResultSubmitted {
                        artifact_id: "a".into(),
                        artifact_sha256: "hash".into(),
                        artifact_version: 1,
                    },
                ),
            )
            .unwrap();
        assert_eq!(state.state("work"), Some(AccTaskState::ResultSubmitted));
        state
            .ingest(
                "executor",
                "rt-executor",
                event(
                    "review-request",
                    "executor",
                    CollaborationPayload::ReviewRequest {
                        artifact_id: "a".into(),
                        artifact_sha256: "hash".into(),
                        artifact_version: 1,
                    },
                ),
            )
            .unwrap();
        let self_review = state.ingest(
            "executor",
            "rt-executor",
            event(
                "self-review",
                "executor",
                CollaborationPayload::ReviewResponse {
                    approved: true,
                    artifact_id: "a".into(),
                    artifact_sha256: "hash".into(),
                    artifact_version: 1,
                },
            ),
        );
        assert!(matches!(self_review, Err(BrokerError::SelfReview)));
        state
            .ingest(
                "reviewer",
                "rt-reviewer",
                event(
                    "review",
                    "reviewer",
                    CollaborationPayload::ReviewResponse {
                        approved: true,
                        artifact_id: "a".into(),
                        artifact_sha256: "hash".into(),
                        artifact_version: 1,
                    },
                ),
            )
            .unwrap();
        assert_eq!(state.state("work"), Some(AccTaskState::Accepted));
        assert_eq!(
            state.events().iter().map(|(n, _)| *n).collect::<Vec<_>>(),
            vec![1, 2, 3, 4]
        );
    }

    #[test]
    fn broker_rejects_impersonation_and_dedupes_replay() {
        let graph = TaskGraph::validate(TaskGraphProposal {
            proposal_id: "p".into(),
            tasks: vec![task("t")],
            dependencies: vec![],
        })
        .unwrap();
        let mut state = AccState::new(graph);
        let event = CollaborationEvent {
            event_id: "e".into(),
            task_id: "t".into(),
            actor_id: "system".into(),
            runtime_id: "rt".into(),
            correlation_id: "c".into(),
            causation_id: None,
            authority: Authority::SystemPolicy,
            payload: CollaborationPayload::TaskStatus {
                state: AccTaskState::Ready,
            },
        };
        assert!(matches!(
            state.ingest("agent", "rt", event.clone()),
            Err(BrokerError::ActorBinding)
        ));
        let mut event = event;
        event.actor_id = "agent".into();
        assert_eq!(state.ingest("agent", "rt", event.clone()), Ok(1));
        assert!(matches!(
            state.ingest("agent", "rt", event),
            Err(BrokerError::DuplicateEvent)
        ));
    }

    struct NegotiationFixture {
        descriptor: RuntimeDescriptor,
        input_calls: u8,
        cancel_calls: u8,
    }

    impl RuntimeAdapter for NegotiationFixture {
        fn describe(&self) -> RuntimeDescriptor {
            self.descriptor.clone()
        }
        fn start(&mut self, _: &str, _: &ContextManifest) -> Result<(), String> {
            Ok(())
        }
        fn snapshot(&self) -> RuntimeSnapshot {
            RuntimeSnapshot {
                runtime_id: self.descriptor.runtime_id.clone(),
                alive: true,
            }
        }
        fn poll_events(&mut self) -> Vec<RuntimeEvent> {
            Vec::new()
        }
        fn send_input(&mut self, _: &str) -> Result<(), String> {
            self.input_calls += 1;
            Ok(())
        }
        fn cancel(&mut self) -> Result<(), String> {
            self.cancel_calls += 1;
            Ok(())
        }
        fn collect_artifacts(&self) -> Vec<(String, String)> {
            Vec::new()
        }
        fn health(&self) -> bool {
            true
        }
    }

    #[test]
    fn runtime_feature_negotiation_blocks_unsupported_operations_without_invocation() {
        let mut unsupported = NegotiationFixture {
            descriptor: runtime("opaque", "code.implement"),
            input_calls: 0,
            cancel_calls: 0,
        };
        assert!(matches!(
            send_runtime_input(&mut unsupported, "hello"),
            Err(RuntimeOperationError::Unsupported("midrun_input"))
        ));
        assert_eq!(unsupported.input_calls, 0);
        assert_eq!(cancel_runtime(&mut unsupported), Ok(()));
        assert_eq!(unsupported.cancel_calls, 1);
        let mut supported = NegotiationFixture {
            descriptor: RuntimeDescriptor {
                features: RuntimeFeatures {
                    midrun_input: true,
                    ..unsupported.descriptor.features.clone()
                },
                ..unsupported.descriptor.clone()
            },
            input_calls: 0,
            cancel_calls: 0,
        };
        assert_eq!(send_runtime_input(&mut supported, "hello"), Ok(()));
        assert_eq!(supported.input_calls, 1);
    }

    #[test]
    fn artifact_hash_or_version_mismatch_fails_closed_and_is_auditable() {
        let contract = task("integrity");
        let graph = TaskGraph::validate(TaskGraphProposal {
            proposal_id: "p".into(),
            tasks: vec![contract.clone()],
            dependencies: vec![],
        })
        .unwrap();
        let mut state = AccState::new(graph);
        state.mark_ready(&BTreeSet::new());
        state
            .assign(
                select_assignment(
                    &contract,
                    TaskRole::Implementer,
                    &[runtime("executor", "code.implement")],
                    BTreeSet::new(),
                )
                .unwrap(),
            )
            .unwrap();
        let event = |id: &str, actor: &str, payload| CollaborationEvent {
            event_id: id.into(),
            task_id: "integrity".into(),
            actor_id: actor.into(),
            runtime_id: format!("rt-{actor}"),
            correlation_id: "c".into(),
            causation_id: None,
            authority: Authority::Observation,
            payload,
        };
        state
            .ingest(
                "executor",
                "rt-executor",
                event(
                    "artifact",
                    "executor",
                    CollaborationPayload::ArtifactPublish {
                        artifact_id: "artifact".into(),
                        artifact_sha256: "recorded".into(),
                        artifact_version: 1,
                    },
                ),
            )
            .unwrap();
        state
            .ingest(
                "executor",
                "rt-executor",
                event(
                    "result",
                    "executor",
                    CollaborationPayload::TaskResultSubmitted {
                        artifact_id: "artifact".into(),
                        artifact_sha256: "recorded".into(),
                        artifact_version: 1,
                    },
                ),
            )
            .unwrap();
        state
            .ingest(
                "executor",
                "rt-executor",
                event(
                    "request",
                    "executor",
                    CollaborationPayload::ReviewRequest {
                        artifact_id: "artifact".into(),
                        artifact_sha256: "recorded".into(),
                        artifact_version: 1,
                    },
                ),
            )
            .unwrap();
        let rejected = state.ingest(
            "reviewer",
            "rt-reviewer",
            event(
                "bad-review",
                "reviewer",
                CollaborationPayload::ReviewResponse {
                    approved: true,
                    artifact_id: "artifact".into(),
                    artifact_sha256: "recorded".into(),
                    artifact_version: 2,
                },
            ),
        );
        assert!(matches!(rejected, Err(BrokerError::ArtifactMismatch)));
        assert_eq!(state.state("integrity"), Some(AccTaskState::Reviewing));
        assert_eq!(
            state.events().last().unwrap().1.event_id,
            "bad-review",
            "mismatch is retained in audit history"
        );
    }
}
