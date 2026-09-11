//! Upgrade a pre-ACC journal through the real SQLite versioning path.

use std::collections::BTreeSet;

use agent_code_storage::{
    ExternalRuntimeBinding, RuntimeCollaborationRecord, SqliteAccStore, SqliteTaskBoard,
    SCHEMA_VERSION,
};
use agent_code_team::{
    AcceptanceCriterion, AgentCapability, TaskBoard, TaskContract, TaskGraph, TaskGraphProposal,
    TaskKind,
};
use rusqlite::Connection;

const PRE_ACC_V4: &str = r#"
CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL, active_call INTEGER, created_at TEXT NOT NULL);
CREATE TABLE agent_turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, decision TEXT NOT NULL, error TEXT);
CREATE TABLE tool_calls (session_id TEXT NOT NULL, call_id INTEGER NOT NULL, state TEXT NOT NULL, request TEXT, PRIMARY KEY (session_id, call_id));
CREATE TABLE checkpoints (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, git_head TEXT NOT NULL);
CREATE TABLE team_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, objective TEXT NOT NULL, parent_task INTEGER, kind TEXT NOT NULL, target TEXT, assignee TEXT, status TEXT NOT NULL);
CREATE TABLE team_task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, attempt INTEGER NOT NULL, agent_id TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, from_agent TEXT NOT NULL, to_agent TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, task_id INTEGER, path TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE transitions (seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL);
CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
"#;

fn task() -> TaskContract {
    TaskContract {
        task_id: "acc-task".into(),
        objective: "upgrade journal".into(),
        required_agent_capabilities: BTreeSet::from([AgentCapability("code.implement".into())]),
        requested_capabilities: BTreeSet::new(),
        expected_outputs: vec!["artifact".into()],
        acceptance: vec![AcceptanceCriterion {
            criterion_id: "c".into(),
            requirement: "works".into(),
            independent_review: true,
        }],
        idempotency_key: "upgrade-key".into(),
    }
}

#[test]
fn pre_acc_journal_migrates_preserves_existing_rows_and_recovers_acc_state() {
    let path = std::env::temp_dir().join(format!("agent_code_pre_acc_{}.db", std::process::id()));
    let _ = std::fs::remove_file(&path);
    {
        let conn = Connection::open(&path).expect("create old journal");
        conn.execute_batch(PRE_ACC_V4).expect("install v4 schema");
        conn.pragma_update(None, "user_version", 4)
            .expect("mark v4");
        conn.execute("INSERT INTO sessions (id, state, active_call, created_at) VALUES ('legacy-session', 'Observing', NULL, 'now')", []).expect("seed legacy session");
        conn.execute("INSERT INTO team_tasks (objective, kind, status) VALUES ('legacy task', 'bulk', 'succeeded')", []).expect("seed legacy task");
    }
    {
        let store = SqliteAccStore::open(Connection::open(&path).expect("open upgrade target"))
            .expect("migrate v4 to current");
        let graph = TaskGraph::validate(TaskGraphProposal {
            proposal_id: "upgrade".into(),
            tasks: vec![task()],
            dependencies: vec![],
        })
        .expect("valid acc graph");
        store
            .put_graph(&graph)
            .expect("ACC graph usable after migration");
        assert_eq!(
            store
                .graph("recovered".into())
                .expect("recover graph")
                .tasks()
                .len(),
            1
        );
    }
    let conn = Connection::open(&path).expect("reopen upgraded journal");
    let version: i32 = conn
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .expect("version");
    assert_eq!(version, SCHEMA_VERSION);
    let board = SqliteTaskBoard::open(conn).expect("open preserved board");
    assert_eq!(
        board
            .task(1)
            .expect("read legacy task")
            .expect("legacy task exists")
            .objective,
        "legacy task"
    );
    assert_eq!(board.task(1).unwrap().unwrap().kind, TaskKind::Bulk);
    board
        .upsert_external_binding(&ExternalRuntimeBinding {
            team_task_id: 1,
            attempt: 1,
            agent_id: "codex".into(),
            runtime_kind: "codex-app-server".into(),
            native_thread_id: Some("external-thread".into()),
            native_turn_id: None,
            lifecycle_state: "reconcile_pending".into(),
        })
        .expect("v7 binding usable after v4 migration");
    board
        .record_runtime_collaboration(&RuntimeCollaborationRecord {
            team_task_id: 1,
            attempt: 1,
            runtime_kind: "codex-app-server".into(),
            native_call_id: "call".into(),
            kind: "request_context".into(),
            payload_summary: "bounded".into(),
            response_summary: Some("context returned".into()),
        })
        .expect("v7 collaboration usable after v4 migration");
    let _ = std::fs::remove_file(&path);
}
