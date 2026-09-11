/// The schema version this crate writes. Version 2 adds `agent_turns.error`
/// and `tool_calls.request` on top of the R3 (version 1) schema. Version 3
/// extends the team tables for the durable task board: task parent/kind/
/// target/assignee, run attempt/result/error, and task-keyed artifacts.
pub const SCHEMA_VERSION: i32 = 7;

/// The durable journal schema. Deliberately small; it does not reproduce the
/// legacy qualification/audit schema.
pub const SCHEMA: &str = r#"
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
    parent_task INTEGER,
    kind TEXT NOT NULL,
    target TEXT,
    assignee TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS team_task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES team_tasks(id),
    attempt INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    task_id INTEGER,
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
CREATE TABLE IF NOT EXISTS acc_tasks (
    task_id TEXT PRIMARY KEY,
    contract_json TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS acc_dependencies (
    predecessor TEXT NOT NULL REFERENCES acc_tasks(task_id),
    successor TEXT NOT NULL REFERENCES acc_tasks(task_id),
    kind TEXT NOT NULL,
    PRIMARY KEY (predecessor, successor, kind)
);
CREATE TABLE IF NOT EXISTS acc_context_manifests (
    manifest_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES acc_tasks(task_id),
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS acc_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES acc_tasks(task_id),
    sha256 TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS acc_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES acc_tasks(task_id),
    event_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_runtime_bindings (
    team_task_id INTEGER NOT NULL REFERENCES team_tasks(id),
    attempt INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    runtime_kind TEXT NOT NULL,
    native_thread_id TEXT,
    native_turn_id TEXT,
    lifecycle_state TEXT NOT NULL,
    PRIMARY KEY (team_task_id, attempt)
);
CREATE TABLE IF NOT EXISTS runtime_collaboration_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_task_id INTEGER NOT NULL REFERENCES team_tasks(id),
    attempt INTEGER NOT NULL,
    runtime_kind TEXT NOT NULL,
    native_call_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_summary TEXT NOT NULL,
    response_summary TEXT,
    UNIQUE (runtime_kind, native_call_id)
);
"#;

/// Idempotently bring `conn` up to [`SCHEMA_VERSION`]. A fresh database is
/// created at the current version by [`SCHEMA`]; an older database is migrated
/// by adding the missing columns in one transaction. Already-current databases
/// are left untouched.
pub fn migrate(conn: &mut rusqlite::Connection) -> Result<(), rusqlite::Error> {
    // Propagate a real PRAGMA read error rather than treating it as version 0
    // (which would silently re-run the migration).
    let version: i32 = conn.pragma_query_value(None, "user_version", |row| row.get(0))?;
    if version >= SCHEMA_VERSION {
        return Ok(());
    }
    let tx = conn.transaction()?;
    // R3 -> R4: the two columns the durable turn/request record needs.
    ensure_column(&tx, "agent_turns", "error", "TEXT")?;
    ensure_column(&tx, "tool_calls", "request", "TEXT")?;
    // R4 -> R5: the durable task-board columns.
    ensure_column(&tx, "team_tasks", "parent_task", "INTEGER")?;
    ensure_column(&tx, "team_tasks", "kind", "TEXT")?;
    ensure_column(&tx, "team_tasks", "target", "TEXT")?;
    ensure_column(&tx, "team_tasks", "assignee", "TEXT")?;
    ensure_column(&tx, "team_task_runs", "attempt", "INTEGER")?;
    ensure_column(&tx, "team_task_runs", "result", "TEXT")?;
    ensure_column(&tx, "team_task_runs", "error", "TEXT")?;
    // Pre-v3 team rows lack `kind` and `attempt`. Fill explicit defaults so the
    // public TaskBoard API can read them: a recoverable legacy mapping rather
    // than rows that fail to reconstruct.
    tx.execute("UPDATE team_tasks SET kind = 'bulk' WHERE kind IS NULL", [])?;
    tx.execute(
        "UPDATE team_task_runs SET attempt = id WHERE attempt IS NULL",
        [],
    )?;
    // R6 -> R7: external Coding Agent references remain subordinate to the
    // durable team task/run, while bounded collaboration calls survive reopen.
    tx.execute_batch(
        "CREATE TABLE IF NOT EXISTS external_runtime_bindings (
            team_task_id INTEGER NOT NULL REFERENCES team_tasks(id), attempt INTEGER NOT NULL,
            agent_id TEXT NOT NULL, runtime_kind TEXT NOT NULL, native_thread_id TEXT,
            native_turn_id TEXT, lifecycle_state TEXT NOT NULL,
            PRIMARY KEY (team_task_id, attempt)
         );
         CREATE TABLE IF NOT EXISTS runtime_collaboration_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team_task_id INTEGER NOT NULL REFERENCES team_tasks(id), attempt INTEGER NOT NULL,
            runtime_kind TEXT NOT NULL, native_call_id TEXT NOT NULL, kind TEXT NOT NULL,
            payload_summary TEXT NOT NULL, response_summary TEXT,
            UNIQUE (runtime_kind, native_call_id)
         );",
    )?;
    // `artifacts` gains a nullable `task_id` and a nullable `session_id`, so a
    // team task's artifacts and a single-agent session's coexist. SQLite cannot
    // relax a NOT NULL constraint in place, so the table is rebuilt.
    migrate_artifacts_to_v3(&tx)?;
    // R5 -> ACC/0.1: one SQLite journal gains typed ACC projections. The
    // sequence remains this table's SQLite rowid; no parallel ordering clock.
    tx.execute_batch(
        "CREATE TABLE IF NOT EXISTS acc_tasks (
            task_id TEXT PRIMARY KEY, contract_json TEXT NOT NULL, state TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS acc_context_manifests (
            manifest_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES acc_tasks(task_id),
            manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL
         );
         CREATE TABLE IF NOT EXISTS acc_dependencies (
            predecessor TEXT NOT NULL REFERENCES acc_tasks(task_id),
            successor TEXT NOT NULL REFERENCES acc_tasks(task_id), kind TEXT NOT NULL,
            PRIMARY KEY (predecessor, successor, kind)
         );
         CREATE TABLE IF NOT EXISTS acc_artifacts (
            artifact_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES acc_tasks(task_id), sha256 TEXT NOT NULL,
            version INTEGER NOT NULL
         );
         CREATE TABLE IF NOT EXISTS acc_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL REFERENCES acc_tasks(task_id), event_json TEXT NOT NULL
         );",
    )?;
    ensure_column(&tx, "acc_artifacts", "version", "INTEGER")?;
    tx.execute(
        "UPDATE acc_artifacts SET version = 1 WHERE version IS NULL",
        [],
    )?;
    tx.pragma_update(None, "user_version", SCHEMA_VERSION)?;
    tx.commit()?;
    Ok(())
}

/// Rebuild `artifacts` so both `session_id` and `task_id` are nullable,
/// preserving existing rows. Skipped when the table is already at v3.
fn migrate_artifacts_to_v3(tx: &rusqlite::Transaction) -> Result<(), rusqlite::Error> {
    let has_task_id: i64 = tx.query_row(
        "SELECT COUNT(*) FROM pragma_table_info('artifacts') WHERE name = 'task_id'",
        [],
        |r| r.get(0),
    )?;
    if has_task_id > 0 {
        return Ok(());
    }
    tx.execute_batch(
        "CREATE TABLE artifacts_v3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            task_id INTEGER,
            path TEXT NOT NULL,
            sha256 TEXT NOT NULL
         );
         INSERT INTO artifacts_v3 (id, session_id, path, sha256)
             SELECT id, session_id, path, sha256 FROM artifacts;
         DROP TABLE artifacts;
         ALTER TABLE artifacts_v3 RENAME TO artifacts;",
    )?;
    Ok(())
}

/// Add `column` of type `ty` to `table` if it is not already present. Table
/// and column names are internal constants, so interpolation is safe.
fn ensure_column(
    conn: &rusqlite::Transaction,
    table: &str,
    column: &str,
    ty: &str,
) -> Result<(), rusqlite::Error> {
    let sql = format!("SELECT COUNT(*) FROM pragma_table_info('{table}') WHERE name = '{column}'");
    let count: i64 = conn.query_row(&sql, [], |r| r.get(0))?;
    if count == 0 {
        conn.execute(&format!("ALTER TABLE {table} ADD COLUMN {column} {ty}"), [])?;
    }
    Ok(())
}
