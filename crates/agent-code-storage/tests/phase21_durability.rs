use agent_code_storage::{ExternalRuntimeBinding, RuntimeCollaborationRecord, SqliteTaskBoard};
use agent_code_team::{TaskBoard, TaskKind};
use rusqlite::Connection;

#[test]
fn external_binding_and_idempotent_collaboration_survive_reopen() {
    let path = std::env::temp_dir().join(format!("ras_phase21_{}.db", std::process::id()));
    let _ = std::fs::remove_file(&path);
    {
        let mut board = SqliteTaskBoard::open(Connection::open(&path).unwrap()).unwrap();
        let task = board
            .create_task("codex work", None, TaskKind::Reasoning, None)
            .unwrap();
        board
            .upsert_external_binding(&ExternalRuntimeBinding {
                team_task_id: task,
                attempt: 1,
                agent_id: "codex".into(),
                runtime_kind: "codex-app-server".into(),
                native_thread_id: Some("thread-external".into()),
                native_turn_id: Some("turn-external".into()),
                lifecycle_state: "running".into(),
            })
            .unwrap();
        let call = RuntimeCollaborationRecord {
            team_task_id: task,
            attempt: 1,
            runtime_kind: "codex-app-server".into(),
            native_call_id: "call-1".into(),
            kind: "request_help".into(),
            payload_summary: "bounded question".into(),
            response_summary: Some("bounded response".into()),
        };
        assert!(board.record_runtime_collaboration(&call).unwrap());
        assert!(!board.record_runtime_collaboration(&call).unwrap());
    }
    let board = SqliteTaskBoard::open(Connection::open(&path).unwrap()).unwrap();
    let binding = board.external_binding(1, 1).unwrap().unwrap();
    assert_eq!(binding.native_thread_id.as_deref(), Some("thread-external"));
    assert_eq!(board.runtime_collaboration(1, 1).unwrap().len(), 1);
    let _ = std::fs::remove_file(path);
}
