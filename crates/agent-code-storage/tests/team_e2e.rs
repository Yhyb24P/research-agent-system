//! End-to-end heterogeneous team test on a durable SQLite board.
//!
//! Deterministic mock Reasoner/Worker/Utility drivers (no real Qwen/Codex;
//! real drivers are R6). Proves: objective -> Lead delegates >=2 tasks ->
//! worker and utility run concurrently -> one task retries after a failure ->
//! another reassigns -> results/artifacts/directed messages flow back -> the
//! Lead follows up on a result and synthesizes a grounded answer. Then the
//! database is closed and reopened, and the whole task tree, attempt history,
//! assignments, messages, and artifacts are asserted to be reconstructable.

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::sync::Mutex;

use agent_code_storage::SqliteTaskBoard;
use agent_code_team::{
    AgentConfig, AgentDriver, AgentMessage, AgentRegistry, AgentTask, AgentTaskResult, AgentTier,
    ArtifactMeta, Lead, LeadBrain, LeadContext, LeadDecision, Scheduler, TaskBoard, TaskKind,
    TaskSpec, TaskStatus, TeamResult,
};
use async_trait::async_trait;
use rusqlite::Connection;

/// A deterministic mock driver. Tracks the peak number of in-flight tasks so
/// the test can prove real concurrency, and gives task-specific behavior so a
/// retry and a reassignment are exercised.
struct E2eDriver {
    agent: String,
    in_flight: Arc<AtomicUsize>,
    peak: Arc<AtomicUsize>,
    summarize_calls: Arc<Mutex<u32>>,
}

impl E2eDriver {
    fn do_task(&self, task: &AgentTask) -> Result<AgentTaskResult, String> {
        match (self.agent.as_str(), task.objective.as_str()) {
            // Fails once, then succeeds: exercises retry on the same agent.
            ("worker-a", "summarize data") => {
                let mut n = self.summarize_calls.lock().unwrap();
                *n += 1;
                if *n == 1 {
                    return Err("transient failure".into());
                }
                Ok(AgentTaskResult {
                    task_id: task.id,
                    summary: "data summary".into(),
                    artifacts: vec![ArtifactMeta {
                        path: "summary.md".into(),
                        sha256: "abc123".into(),
                    }],
                    message: Some(AgentMessage {
                        from_agent: "worker-a".into(),
                        to_agent: "reasoner-a".into(),
                        body: "data ready for refinement".into(),
                    }),
                })
            }
            // Always fails: forces reassignment to worker-b.
            ("worker-a", _) => Err("worker-a cannot handle this".into()),
            ("worker-b", _) => Ok(AgentTaskResult {
                task_id: task.id,
                summary: "bulk done".into(),
                artifacts: Vec::new(),
                message: None,
            }),
            ("utility-a", _) => Ok(AgentTaskResult {
                task_id: task.id,
                summary: "utility output".into(),
                artifacts: vec![ArtifactMeta {
                    path: "util.txt".into(),
                    sha256: "def456".into(),
                }],
                message: None,
            }),
            ("reasoner-a", _) => {
                // Echo the context so the test can prove the parent's result
                // flowed into this follow-up task's context.
                let ctx = task.context.join("|");
                Ok(AgentTaskResult {
                    task_id: task.id,
                    summary: format!("refined insight ({ctx})"),
                    artifacts: Vec::new(),
                    message: None,
                })
            }
            _ => Ok(AgentTaskResult {
                task_id: task.id,
                summary: "ok".into(),
                artifacts: Vec::new(),
                message: None,
            }),
        }
    }
}

#[async_trait]
impl AgentDriver for E2eDriver {
    async fn run_task(&self, task: AgentTask) -> Result<AgentTaskResult, String> {
        let cur = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
        self.peak.fetch_max(cur, Ordering::SeqCst);
        // Yield so other in-flight tasks can start, proving real overlap.
        tokio::task::yield_now().await;
        let outcome = self.do_task(&task);
        self.in_flight.fetch_sub(1, Ordering::SeqCst);
        outcome
    }
}

/// The Lead brain: delegate three tasks, follow up on a result, then complete
/// with an answer grounded in the actual results.
struct E2eBrain;

impl LeadBrain for E2eBrain {
    fn decide(&self, ctx: &LeadContext) -> LeadDecision {
        match ctx.round {
            0 => LeadDecision::Delegate(vec![
                TaskSpec {
                    objective: "summarize data".into(),
                    kind: TaskKind::Bulk,
                    target: Some("worker-a".into()),
                    parent: None,
                    context: Vec::new(),
                },
                TaskSpec {
                    objective: "bulk process".into(),
                    kind: TaskKind::Bulk,
                    target: None,
                    parent: None,
                    context: Vec::new(),
                },
                TaskSpec {
                    objective: "fetch utility".into(),
                    kind: TaskKind::Utility,
                    target: Some("utility-a".into()),
                    parent: None,
                    context: Vec::new(),
                },
            ]),
            1 => {
                // A follow-up grounded in a worker result (T10).
                let parent = ctx.results.first().map(|(id, _)| *id);
                LeadDecision::FollowUp(vec![TaskSpec {
                    objective: "refine".into(),
                    kind: TaskKind::Reasoning,
                    target: Some("reasoner-a".into()),
                    parent,
                    context: Vec::new(),
                }])
            }
            _ => {
                // The answer is synthesized from the actual results (T16).
                let task_refs: Vec<u64> = ctx.results.iter().map(|(id, _)| *id).collect();
                let answer = ctx
                    .results
                    .iter()
                    .map(|(_, r)| r.summary.clone())
                    .collect::<Vec<_>>()
                    .join("; ");
                LeadDecision::Complete(TeamResult { answer, task_refs })
            }
        }
    }
}

fn build_registry() -> AgentRegistry {
    AgentRegistry::new(vec![
        AgentConfig {
            id: "reasoner-a".into(),
            name: "reasoner-a".into(),
            tier: AgentTier::Reasoner,
            tags: Vec::new(),
            max_concurrency: 2,
        },
        AgentConfig {
            id: "worker-a".into(),
            name: "worker-a".into(),
            tier: AgentTier::Worker,
            tags: Vec::new(),
            max_concurrency: 2,
        },
        AgentConfig {
            id: "worker-b".into(),
            name: "worker-b".into(),
            tier: AgentTier::Worker,
            tags: Vec::new(),
            max_concurrency: 2,
        },
        AgentConfig {
            id: "utility-a".into(),
            name: "utility-a".into(),
            tier: AgentTier::Utility,
            tags: Vec::new(),
            max_concurrency: 2,
        },
    ])
    .expect("a full tier set is valid")
}

fn build_drivers(
    in_flight: Arc<AtomicUsize>,
    peak: Arc<AtomicUsize>,
    summarize_calls: Arc<Mutex<u32>>,
) -> BTreeMap<String, Arc<dyn AgentDriver>> {
    let mk = |agent: &str| -> Arc<dyn AgentDriver> {
        Arc::new(E2eDriver {
            agent: agent.to_string(),
            in_flight: in_flight.clone(),
            peak: peak.clone(),
            summarize_calls: summarize_calls.clone(),
        })
    };
    BTreeMap::from([
        ("reasoner-a".to_string(), mk("reasoner-a")),
        ("worker-a".to_string(), mk("worker-a")),
        ("worker-b".to_string(), mk("worker-b")),
        ("utility-a".to_string(), mk("utility-a")),
    ])
}

#[tokio::test]
async fn heterogeneous_team_end_to_end() {
    let path = std::env::temp_dir().join(format!("agent_code_team_e2e_{}.db", std::process::id()));
    let _ = std::fs::remove_file(&path);

    let in_flight = Arc::new(AtomicUsize::new(0));
    let peak = Arc::new(AtomicUsize::new(0));
    let summarize_calls = Arc::new(Mutex::new(0u32));

    // Phase 1: run the team on a file-backed board.
    let (result, task_ids) = {
        let conn = Connection::open(&path).expect("open db");
        let board = SqliteTaskBoard::open(conn).expect("open board");
        let drivers = build_drivers(in_flight.clone(), peak.clone(), summarize_calls.clone());
        let sched = Scheduler::new(build_registry(), drivers, board, 2);
        let mut lead = Lead::new(Box::new(E2eBrain), sched, 5, 10);
        let result = lead
            .run("analyze the dataset")
            .await
            .expect("lead completes");
        let task_ids = lead.task_ids().to_vec();
        (result, task_ids)
    }; // the board is dropped here, closing the database

    // The in-memory result is grounded in the actual worker results.
    assert!(result.answer.contains("data summary"));
    assert!(result.answer.contains("utility output"));
    assert!(result.answer.contains("refined insight"));
    assert!(result.task_refs.len() >= 2);
    // A worker and a utility task were in-flight at the same time.
    assert!(
        peak.load(Ordering::SeqCst) >= 2,
        "worker and utility overlapped"
    );

    // Phase 2: reopen the database and assert everything is reconstructable.
    let conn = Connection::open(&path).expect("reopen db");
    let board = SqliteTaskBoard::open(conn).expect("reopen board");

    // The full task tree: four tasks, in creation order.
    assert_eq!(task_ids.len(), 4);
    let objectives: Vec<String> = task_ids
        .iter()
        .map(|&id| board.task(id).expect("task").expect("exists").objective)
        .collect();
    assert_eq!(
        objectives,
        vec!["summarize data", "bulk process", "fetch utility", "refine"]
    );
    for &id in &task_ids {
        assert_eq!(
            board.task(id).expect("task").expect("exists").status,
            TaskStatus::Succeeded
        );
    }
    // The follow-up (task 4) is a child of the first worker task.
    let follow_up = board.task(task_ids[3]).expect("task").expect("exists");
    assert_eq!(follow_up.parent_task, Some(task_ids[0]));

    // Attempt history: the retried task kept its failure; the reassigned task
    // shows two agents.
    let retried = board.attempts(task_ids[0]).expect("attempts");
    assert_eq!(retried.len(), 2);
    assert_eq!(retried[0].status, TaskStatus::Failed);
    assert_eq!(retried[1].status, TaskStatus::Succeeded);
    let reassigned = board.attempts(task_ids[1]).expect("attempts");
    assert_eq!(reassigned.len(), 3);
    assert_eq!(reassigned[0].agent_id, "worker-a");
    assert_eq!(reassigned[2].agent_id, "worker-b");

    // The follow-up task's context carried its parent's result (T07/T10): the
    // reasoner's recorded result echoes the parent's "data summary".
    let follow_up_attempts = board.attempts(task_ids[3]).expect("attempts");
    let follow_up_result = follow_up_attempts
        .iter()
        .find(|a| a.status == TaskStatus::Succeeded)
        .and_then(|a| a.result.clone())
        .expect("follow-up result");
    assert!(follow_up_result.contains("data summary"));

    // Assignment: the reassigned task's actual assignee is worker-b.
    let reassigned_record = board.task(task_ids[1]).expect("task").expect("exists");
    assert_eq!(reassigned_record.assignee.as_deref(), Some("worker-b"));

    // Directed message: the worker's message reached the intended agent.
    let msgs = board.messages_to("reasoner-a").expect("messages");
    assert!(msgs.iter().any(|m| m.body == "data ready for refinement"));

    // Artifacts: the worker and utility artifacts are reconstructable.
    let worker_artifacts = board.artifacts(task_ids[0]).expect("artifacts");
    assert!(worker_artifacts.iter().any(|a| a.path == "summary.md"));
    let utility_artifacts = board.artifacts(task_ids[2]).expect("artifacts");
    assert!(utility_artifacts.iter().any(|a| a.path == "util.txt"));

    let _ = std::fs::remove_file(&path);
}
