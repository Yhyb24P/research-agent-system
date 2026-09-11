//! Deterministic ACC/0.1 Rust-v2 closed loop through canonical SQLite state.

use std::collections::BTreeSet;

use agent_code_storage::SqliteAccStore;
use agent_code_team::{
    select_assignment, AccState, AccTaskState, AcceptanceCriterion, AgentCapability, Authority,
    Classification, CollaborationEvent, CollaborationPayload, ContextItem, ContextManifest,
    DependencyKind, ObservabilityLevel, Provenance, RuntimeDescriptor, RuntimeFeatures,
    TaskContract, TaskDependency, TaskGraph, TaskGraphProposal, TaskRole, TrustStatus,
    TrustedCapability,
};
use rusqlite::Connection;

fn task(id: &str, capability: &str) -> TaskContract {
    TaskContract {
        task_id: id.into(),
        objective: format!("{id} objective"),
        required_agent_capabilities: BTreeSet::from([AgentCapability(capability.into())]),
        requested_capabilities: BTreeSet::new(),
        expected_outputs: vec!["artifact".into()],
        acceptance: vec![AcceptanceCriterion {
            criterion_id: "review".into(),
            requirement: "independent review".into(),
            independent_review: true,
        }],
        idempotency_key: format!("{id}-key"),
    }
}

fn runtime(agent: &str, capability: &str) -> RuntimeDescriptor {
    RuntimeDescriptor {
        runtime_id: format!("runtime-{agent}"),
        agent_id: agent.into(),
        adapter_kind: "deterministic-fixture".into(),
        runtime_version: "1".into(),
        agent_capabilities: BTreeSet::from([AgentCapability(capability.into())]),
        observability_level: ObservabilityLevel::O1,
        features: RuntimeFeatures {
            streaming: false,
            cancellation: true,
            continuation: false,
            midrun_input: true,
            structured_output: true,
            worktree_isolation: true,
        },
        protocols: vec![],
    }
}

fn event(
    id: &str,
    task_id: &str,
    actor: &str,
    payload: CollaborationPayload,
) -> CollaborationEvent {
    CollaborationEvent {
        event_id: id.into(),
        task_id: task_id.into(),
        actor_id: actor.into(),
        runtime_id: format!("runtime-{actor}"),
        correlation_id: "goal-1".into(),
        causation_id: None,
        authority: Authority::Observation,
        payload,
    }
}

#[test]
fn deterministic_acc_loop_survives_restart_and_enforces_boundaries() {
    let path = std::env::temp_dir().join(format!("agent_code_acc_e2e_{}.db", std::process::id()));
    let _ = std::fs::remove_file(&path);
    let execute = task("execute", "code.implement");
    let help = task("help", "code.review");
    let graph = TaskGraph::validate(TaskGraphProposal {
        proposal_id: "coordinator-proposal".into(),
        tasks: vec![execute.clone(), help.clone()],
        dependencies: vec![TaskDependency {
            predecessor: "help".into(),
            successor: "execute".into(),
            kind: DependencyKind::RelatedTo,
        }],
    })
    .expect("coordinator proposal validates");
    let mut state = AccState::new(graph.clone());
    state.mark_ready(&BTreeSet::new());
    let executor = select_assignment(
        &execute,
        TaskRole::Implementer,
        &[runtime("executor", "code.implement")],
        BTreeSet::from([TrustedCapability("workspace.write".into())]),
    )
    .expect("executor selected");
    let helper = select_assignment(
        &help,
        TaskRole::Helper,
        &[runtime("helper", "code.review")],
        BTreeSet::new(),
    )
    .expect("helper selected");
    assert!(
        helper.trusted_grants.is_empty(),
        "helper does not inherit executor grant"
    );
    state.assign(executor).expect("assign executor");
    state.assign(helper).expect("assign helper");
    let manifest = ContextManifest::build(
        "manifest-execute".into(),
        "execute".into(),
        vec![
            ContextItem {
                item_id: "contract".into(),
                content: "implement safely".into(),
                authority: Authority::TaskContract,
                trust: TrustStatus::Verified,
                classification: Classification::Internal,
                provenance: Provenance {
                    source: "coordinator".into(),
                    source_version: "1".into(),
                },
                forwardable: true,
            },
            ContextItem {
                item_id: "private".into(),
                content: "hidden chain-of-thought".into(),
                authority: Authority::Observation,
                trust: TrustStatus::Unverified,
                classification: Classification::Internal,
                provenance: Provenance {
                    source: "runtime".into(),
                    source_version: "1".into(),
                },
                forwardable: true,
            },
        ],
    );
    assert_eq!(
        manifest.items.len(),
        1,
        "private state cannot enter shared context"
    );
    state
        .persist_manifest(manifest.clone())
        .expect("persist in canonical state");

    {
        let store =
            SqliteAccStore::open(Connection::open(&path).expect("open db")).expect("open store");
        store.put_graph(&graph).expect("persist graph");
        store.put_manifest(&manifest).expect("persist manifest");
        let flow = vec![
            (
                "executor",
                event(
                    "help-request",
                    "execute",
                    "executor",
                    CollaborationPayload::HelpRequest {
                        requested_capability: AgentCapability("code.review".into()),
                        question: "please review".into(),
                    },
                ),
            ),
            (
                "helper",
                event(
                    "help-response",
                    "execute",
                    "helper",
                    CollaborationPayload::HelpResponse {
                        response: "check the artifact hash".into(),
                    },
                ),
            ),
            (
                "executor",
                event(
                    "artifact",
                    "execute",
                    "executor",
                    CollaborationPayload::ArtifactPublish {
                        artifact_id: "artifact-v1".into(),
                        artifact_sha256: "0123".into(),
                        artifact_version: 1,
                    },
                ),
            ),
            (
                "executor",
                event(
                    "result",
                    "execute",
                    "executor",
                    CollaborationPayload::TaskResultSubmitted {
                        artifact_id: "artifact-v1".into(),
                        artifact_sha256: "0123".into(),
                        artifact_version: 1,
                    },
                ),
            ),
            (
                "executor",
                event(
                    "review-request",
                    "execute",
                    "executor",
                    CollaborationPayload::ReviewRequest {
                        artifact_id: "artifact-v1".into(),
                        artifact_sha256: "0123".into(),
                        artifact_version: 1,
                    },
                ),
            ),
            (
                "reviewer",
                event(
                    "review",
                    "execute",
                    "reviewer",
                    CollaborationPayload::ReviewResponse {
                        approved: true,
                        artifact_id: "artifact-v1".into(),
                        artifact_sha256: "0123".into(),
                        artifact_version: 1,
                    },
                ),
            ),
        ];
        for (actor, item) in flow {
            let runtime_id = format!("runtime-{actor}");
            state
                .ingest(actor, &runtime_id, item)
                .expect("typed broker accepts valid event");
            let normalized = state.events().last().expect("event appended").1.clone();
            assert!(store
                .append_event(&normalized)
                .expect("append event")
                .is_some());
        }
        store
            .put_artifact("artifact-v1", "execute", "0123", 1)
            .expect("persist immutable ref");
        store
            .set_task_state("execute", state.state("execute").unwrap())
            .expect("persist accepted state");
        assert_eq!(state.state("execute"), Some(AccTaskState::Accepted));
        assert!(store
            .append_event(&event(
                "review",
                "execute",
                "reviewer",
                CollaborationPayload::ReviewResponse {
                    approved: true,
                    artifact_id: "artifact-v1".into(),
                    artifact_sha256: "0123".into(),
                    artifact_version: 1,
                }
            ))
            .expect("replay")
            .is_none());
    }
    let reopened =
        SqliteAccStore::open(Connection::open(&path).expect("reopen db")).expect("reopen store");
    assert_eq!(
        reopened
            .graph("recovered-proposal".into())
            .expect("recover graph")
            .tasks()
            .len(),
        2
    );
    assert!(reopened
        .manifest("manifest-execute")
        .expect("read manifest")
        .expect("manifest exists")
        .verify_hash());
    let recovered = reopened.events_after(0).expect("recover events");
    assert_eq!(recovered.len(), 6);
    assert_eq!(
        recovered
            .iter()
            .map(|(sequence, _)| *sequence)
            .collect::<Vec<_>>(),
        vec![1, 2, 3, 4, 5, 6]
    );
    assert!(matches!(
        recovered[1].1.payload,
        CollaborationPayload::HelpResponse { .. }
    ));
    let _ = std::fs::remove_file(&path);
}
