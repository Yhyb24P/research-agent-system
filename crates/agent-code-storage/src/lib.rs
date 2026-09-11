//! Small SQLite journal for durable Agent state.

mod acc_store;
mod board;
mod journal;
mod observations;
mod schema;

pub use acc_store::SqliteAccStore;
pub use board::SqliteTaskBoard;
pub use journal::SqliteJournal;
pub use schema::{migrate, SCHEMA, SCHEMA_VERSION};

#[cfg(test)]
mod tests {
    use agent_code_core::recover_interrupted_tools;
    use agent_code_core::{AgentState, Journal, SessionId, ToolCallId};
    use rusqlite::Connection;

    use super::SqliteJournal;

    fn temp_db(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("agent_code_r1fix_{name}_{}.db", std::process::id()))
    }

    #[test]
    fn reopen_recovers_current_state() {
        let path = temp_db("reopen");
        let _ = std::fs::remove_file(&path);
        {
            let conn = Connection::open(&path).expect("open db");
            let mut j = SqliteJournal::open(conn, SessionId::new("s")).expect("bind");
            j.create(AgentState::Initializing).expect("create");
            j.record_transition(AgentState::Initializing, AgentState::Observing)
                .expect("t1");
            j.record_transition(AgentState::Observing, AgentState::WaitingModel)
                .expect("t2");
        }
        let conn = Connection::open(&path).expect("reopen db");
        let j = SqliteJournal::open(conn, SessionId::new("s")).expect("bind");
        assert_eq!(
            j.current_state().expect("read"),
            Some(AgentState::WaitingModel)
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn two_sessions_are_isolated() {
        let path = temp_db("isolated");
        let _ = std::fs::remove_file(&path);
        let c1 = Connection::open(&path).expect("open a");
        let c2 = Connection::open(&path).expect("open b");
        let mut a = SqliteJournal::open(c1, SessionId::new("a")).expect("bind a");
        let b = SqliteJournal::open(c2, SessionId::new("b")).expect("bind b");
        a.create(AgentState::Initializing).expect("create a");
        a.record_transition(AgentState::Initializing, AgentState::Observing)
            .expect("a t1");
        a.record_transition(AgentState::Observing, AgentState::WaitingModel)
            .expect("a t2");
        a.begin_tool_call(AgentState::WaitingModel, ToolCallId::new(1))
            .expect("a begin");

        assert_eq!(
            a.current_state().expect("a state"),
            Some(AgentState::ExecutingTool {
                call: ToolCallId::new(1)
            })
        );
        assert_eq!(
            a.running_tools().expect("a running"),
            vec![ToolCallId::new(1)]
        );
        // Session b is untouched by session a's activity.
        assert_eq!(b.current_state().expect("b state"), None);
        assert_eq!(
            b.running_tools().expect("b running"),
            Vec::<ToolCallId>::new()
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn reopen_marks_running_tool_interrupted() {
        let path = temp_db("interrupted");
        let _ = std::fs::remove_file(&path);
        {
            let conn = Connection::open(&path).expect("open db");
            let mut j = SqliteJournal::open(conn, SessionId::new("s")).expect("bind");
            j.create(AgentState::Initializing).expect("create");
            j.record_transition(AgentState::Initializing, AgentState::Observing)
                .expect("t1");
            j.record_transition(AgentState::Observing, AgentState::WaitingModel)
                .expect("t2");
            j.begin_tool_call(AgentState::WaitingModel, ToolCallId::new(9))
                .expect("begin");
        }
        let conn = Connection::open(&path).expect("reopen db");
        let mut j = SqliteJournal::open(conn, SessionId::new("s")).expect("bind");
        let interrupted = recover_interrupted_tools(&mut j).expect("recover");
        assert_eq!(interrupted, vec![ToolCallId::new(9)]);
        assert!(j.running_tools().expect("running").is_empty());
        let _ = std::fs::remove_file(&path);
    }

    /// The exact R3 (version 1) schema: the R4 schema minus the two new
    /// columns (`agent_turns.error`, `tool_calls.request`).
    const R3_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    active_call INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    decision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    session_id TEXT NOT NULL,
    call_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (session_id, call_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    git_head TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES team_tasks(id),
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"#;

    /// The exact R4 (version 2) schema: the R3 schema plus the two R4 columns
    /// (`agent_turns.error`, `tool_calls.request`) and the v2 team tables.
    const V2_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    active_call INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    decision TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    session_id TEXT NOT NULL,
    call_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    request TEXT,
    PRIMARY KEY (session_id, call_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    git_head TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES team_tasks(id),
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"#;

    /// Opening an R3 database migrates it to the current version: old data is
    /// preserved, the new columns become writable, and the session recovers.
    #[test]
    fn r3_database_migrates_preserving_data() {
        use crate::SCHEMA_VERSION;
        let path = temp_db("migrate");
        let _ = std::fs::remove_file(&path);

        // Build a database with the exact R3 schema and seed it.
        {
            let conn = Connection::open(&path).expect("open db");
            conn.execute_batch(R3_SCHEMA).expect("apply R3 schema");
            conn.pragma_update(None, "user_version", 1)
                .expect("set version 1");
            conn.execute(
                "INSERT INTO sessions (id, state, active_call, created_at) VALUES ('s', 'Observing', NULL, 'now')",
                [],
            )
            .expect("seed session");
            conn.execute(
                "INSERT INTO agent_turns (session_id, decision) VALUES ('s', 'view a.txt')",
                [],
            )
            .expect("seed turn");
            conn.execute(
                "INSERT INTO tool_calls (session_id, call_id, state) VALUES ('s', 1, 'Succeeded')",
                [],
            )
            .expect("seed tool");
        }

        // Open with the current version: the migration adds the missing columns.
        let conn = Connection::open(&path).expect("reopen db");
        let mut j = SqliteJournal::open(conn, SessionId::new("s")).expect("bind");

        // The database was migrated to the current version.
        let version: i32 = j
            .conn()
            .pragma_query_value(None, "user_version", |r| r.get(0))
            .unwrap_or(0);
        assert_eq!(version, SCHEMA_VERSION);

        // The session recovers and the seeded data is preserved.
        assert_eq!(
            j.current_state().expect("state"),
            Some(AgentState::Observing)
        );
        let seeded_turn: String = j
            .conn()
            .query_row(
                "SELECT decision FROM agent_turns WHERE session_id = 's'",
                [],
                |r| r.get(0),
            )
            .expect("seeded turn");
        assert_eq!(seeded_turn, "view a.txt");
        let seeded_tool: String = j
            .conn()
            .query_row(
                "SELECT state FROM tool_calls WHERE session_id = 's' AND call_id = 1",
                [],
                |r| r.get(0),
            )
            .expect("seeded tool");
        assert_eq!(seeded_tool, "Succeeded");

        // The new columns are writable.
        j.record_turn("edit a.txt", Some("boom"))
            .expect("record turn");
        j.record_tool_requested(ToolCallId::new(2), "edit a.txt")
            .expect("record request");
        let error: Option<String> = j
            .conn()
            .query_row(
                "SELECT error FROM agent_turns WHERE decision = 'edit a.txt'",
                [],
                |r| r.get(0),
            )
            .expect("turn error");
        assert_eq!(error, Some("boom".into()));
        let request: Option<String> = j
            .conn()
            .query_row(
                "SELECT request FROM tool_calls WHERE call_id = 2",
                [],
                |r| r.get(0),
            )
            .expect("tool request");
        assert_eq!(request, Some("edit a.txt".into()));

        let _ = std::fs::remove_file(&path);
    }

    /// Opening an R4 (version 2) database migrates it to v3: the team-table
    /// columns are added, `artifacts` is rebuilt with a nullable `task_id`, and
    /// the board is usable. Old rows are preserved.
    #[test]
    fn v2_database_migrates_to_v3() {
        use agent_code_team::{ArtifactMeta, TaskAttempt, TaskBoard, TaskKind, TaskStatus};

        use super::SqliteTaskBoard;

        let path = temp_db("v2to3");
        let _ = std::fs::remove_file(&path);

        // Build a version-2 database and seed team data.
        {
            let conn = Connection::open(&path).expect("open db");
            conn.execute_batch(V2_SCHEMA).expect("apply v2 schema");
            conn.pragma_update(None, "user_version", 2)
                .expect("set version 2");
            conn.execute(
                "INSERT INTO sessions (id, state, active_call, created_at) VALUES ('s', 'Observing', NULL, 'now')",
                [],
            )
            .expect("seed session");
            conn.execute(
                "INSERT INTO team_tasks (objective, status) VALUES ('old obj', 'pending')",
                [],
            )
            .expect("seed task");
            conn.execute(
                "INSERT INTO team_task_runs (task_id, agent_id, status) VALUES (1, 'worker-a', 'running')",
                [],
            )
            .expect("seed run");
            conn.execute(
                "INSERT INTO artifacts (session_id, path, sha256) VALUES ('s', 'out.txt', 'abc')",
                [],
            )
            .expect("seed artifact");
        }

        // Open the board: the migration brings the database to v3.
        let conn = Connection::open(&path).expect("reopen db");
        let mut board = SqliteTaskBoard::open(conn).expect("open board");
        assert_eq!(
            board.schema_version().expect("version"),
            crate::SCHEMA_VERSION
        );

        // The session-keyed artifact survived the `artifacts` rebuild.
        let session_artifact: Option<String> = board
            .conn()
            .query_row(
                "SELECT path FROM artifacts WHERE session_id = 's'",
                [],
                |r| r.get(0),
            )
            .expect("session artifact");
        assert_eq!(session_artifact, Some("out.txt".into()));

        // The new columns are writable: a task with a kind, an attempt with a
        // result, and a task-keyed artifact.
        let id = board
            .create_task("follow-up", Some(1), TaskKind::Reasoning, None)
            .expect("create task");
        board
            .record_attempt(&TaskAttempt {
                task_id: id,
                attempt: 1,
                agent_id: "reasoner-a".into(),
                status: TaskStatus::Succeeded,
                result: Some("done".into()),
                error: None,
            })
            .expect("record attempt");
        board
            .record_artifact(
                id,
                &ArtifactMeta {
                    path: "r.txt".into(),
                    sha256: "def".into(),
                },
            )
            .expect("record artifact");

        let task = board.task(id).expect("read task").expect("task exists");
        assert_eq!(task.kind, TaskKind::Reasoning);
        assert_eq!(task.parent_task, Some(1));
        let attempts = board.attempts(id).expect("attempts");
        assert_eq!(attempts.len(), 1);
        assert_eq!(attempts[0].result.as_deref(), Some("done"));
        let arts = board.artifacts(id).expect("task artifacts");
        assert_eq!(arts.len(), 1);
        assert_eq!(arts[0].path, "r.txt");

        // The seeded v2 task and run rows were preserved.
        let old_task_obj: String = board
            .conn()
            .query_row(
                "SELECT objective FROM team_tasks WHERE objective = 'old obj'",
                [],
                |r| r.get(0),
            )
            .expect("old task preserved");
        assert_eq!(old_task_obj, "old obj");
        let old_run_agent: String = board
            .conn()
            .query_row(
                "SELECT agent_id FROM team_task_runs WHERE task_id = 1",
                [],
                |r| r.get(0),
            )
            .expect("old run preserved");
        assert_eq!(old_run_agent, "worker-a");

        // And the same rows reconstruct through the public TaskBoard API: the
        // migration filled explicit defaults (kind, attempt) so they are
        // readable, not merely present as raw rows.
        let old_task = board.task(1).expect("read old task").expect("exists");
        assert_eq!(old_task.objective, "old obj");
        assert_eq!(old_task.kind, TaskKind::Bulk);
        let old_runs = board.attempts(1).expect("read old attempts");
        assert_eq!(old_runs.len(), 1);
        assert_eq!(old_runs[0].agent_id, "worker-a");
        assert_eq!(old_runs[0].attempt, 1);
        assert_eq!(old_runs[0].status, TaskStatus::Running);

        let _ = std::fs::remove_file(&path);
    }
}
