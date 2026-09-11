//! Command-line entry point (clap).
//!
//! Read-only inspection surface for the active Rust-v2 product.

use agent_code_storage::SqliteAccStore;
use rusqlite::Connection;
use serde::Serialize;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AccInspection {
    pub tasks: Vec<(String, String)>,
    pub graph_task_ids: Vec<String>,
    pub events: Vec<(u64, String)>,
    pub manifest_id: Option<String>,
    pub artifact_refs: Vec<(String, String, String, u32)>,
}

/// Query the authoritative SQLite journal only. `after_sequence` is an event
/// cursor; no fixture or runtime-local state is consulted.
pub fn inspect_acc(
    database_path: &std::path::Path,
    after_sequence: u64,
    manifest_id: Option<&str>,
) -> Result<AccInspection, String> {
    let store = SqliteAccStore::open(Connection::open(database_path).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let graph = store
        .graph("inspection".into())
        .map_err(|e| e.to_string())?;
    let events = store
        .events_after(after_sequence)
        .map_err(|e| e.to_string())?
        .into_iter()
        .map(|(sequence, event)| (sequence, event.event_id))
        .collect();
    let manifest_id = match manifest_id {
        Some(id) => store
            .manifest(id)
            .map_err(|e| e.to_string())?
            .map(|m| m.manifest_id),
        None => None,
    };
    Ok(AccInspection {
        tasks: store.task_states().map_err(|e| e.to_string())?,
        graph_task_ids: graph.tasks().keys().cloned().collect(),
        events,
        manifest_id,
        artifact_refs: store.artifact_refs().map_err(|e| e.to_string())?,
    })
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use agent_code_storage::SqliteAccStore;
    use agent_code_team::{
        AccTaskState, AcceptanceCriterion, AgentCapability, Authority, Classification,
        CollaborationEvent, CollaborationPayload, ContextItem, ContextManifest, Provenance,
        TaskContract, TaskGraph, TaskGraphProposal, TrustStatus,
    };

    use super::inspect_acc;

    #[test]
    fn inspection_reads_authoritative_task_graph_events_manifest_and_artifacts() {
        let path =
            std::env::temp_dir().join(format!("agent_code_acc_inspect_{}.db", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let task = TaskContract {
            task_id: "task".into(),
            objective: "inspect".into(),
            required_agent_capabilities: BTreeSet::from([AgentCapability("code.implement".into())]),
            requested_capabilities: BTreeSet::new(),
            expected_outputs: vec!["artifact".into()],
            acceptance: vec![AcceptanceCriterion {
                criterion_id: "c".into(),
                requirement: "ok".into(),
                independent_review: false,
            }],
            idempotency_key: "key".into(),
        };
        let store = SqliteAccStore::open(rusqlite::Connection::open(&path).unwrap()).unwrap();
        store
            .put_graph(
                &TaskGraph::validate(TaskGraphProposal {
                    proposal_id: "p".into(),
                    tasks: vec![task],
                    dependencies: vec![],
                })
                .unwrap(),
            )
            .unwrap();
        let manifest = ContextManifest::build(
            "manifest".into(),
            "task".into(),
            vec![ContextItem {
                item_id: "i".into(),
                content: "context".into(),
                authority: Authority::TaskContract,
                trust: TrustStatus::Verified,
                classification: Classification::Internal,
                provenance: Provenance {
                    source: "test".into(),
                    source_version: "1".into(),
                },
                forwardable: true,
            }],
        );
        store.put_manifest(&manifest).unwrap();
        store
            .append_event(&CollaborationEvent {
                event_id: "event".into(),
                task_id: "task".into(),
                actor_id: "agent".into(),
                runtime_id: "runtime".into(),
                correlation_id: "c".into(),
                causation_id: None,
                authority: Authority::Observation,
                payload: CollaborationPayload::TaskStatus {
                    state: AccTaskState::Ready,
                },
            })
            .unwrap();
        store.put_artifact("artifact", "task", "hash", 1).unwrap();
        drop(store);
        let result = inspect_acc(&path, 0, Some("manifest")).unwrap();
        assert_eq!(result.graph_task_ids, vec!["task"]);
        assert_eq!(result.events, vec![(1, "event".into())]);
        assert_eq!(result.manifest_id.as_deref(), Some("manifest"));
        assert_eq!(
            result.artifact_refs,
            vec![("artifact".into(), "task".into(), "hash".into(), 1)]
        );
        let cursor = inspect_acc(&path, 1, None).unwrap();
        assert!(cursor.events.is_empty());
        let _ = std::fs::remove_file(&path);
    }
}
