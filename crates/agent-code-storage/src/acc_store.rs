//! Durable projections for Rust-v2 ACC contracts.

use agent_code_team::{
    AccTaskState, CollaborationEvent, ContextManifest, DependencyKind, TaskContract,
    TaskDependency, TaskGraph, TaskGraphProposal,
};
use rusqlite::{params, Connection};

use crate::schema::{migrate, SCHEMA};

pub struct SqliteAccStore {
    conn: Connection,
}

impl SqliteAccStore {
    pub fn open(mut conn: Connection) -> Result<Self, rusqlite::Error> {
        conn.execute_batch(SCHEMA)?;
        migrate(&mut conn)?;
        conn.execute_batch("PRAGMA foreign_keys = ON")?;
        Ok(Self { conn })
    }

    pub fn in_memory() -> Result<Self, rusqlite::Error> {
        Self::open(Connection::open_in_memory()?)
    }

    pub fn put_task(
        &self,
        task: &TaskContract,
        state: AccTaskState,
    ) -> Result<(), rusqlite::Error> {
        let json = serde_json::to_string(task).expect("ACC task is serializable");
        self.conn.execute(
            "INSERT INTO acc_tasks (task_id, contract_json, state) VALUES (?1, ?2, ?3)",
            params![task.task_id, json, state_name(state)],
        )?;
        Ok(())
    }

    pub fn put_graph(&self, graph: &TaskGraph) -> Result<(), rusqlite::Error> {
        let transaction = self.conn.unchecked_transaction()?;
        for task in graph.tasks().values() {
            let json = serde_json::to_string(task).expect("ACC task is serializable");
            transaction.execute(
                "INSERT INTO acc_tasks (task_id, contract_json, state) VALUES (?1, ?2, 'PROPOSED')",
                params![task.task_id, json],
            )?;
        }
        for dependency in graph.dependencies() {
            transaction.execute(
                "INSERT INTO acc_dependencies (predecessor, successor, kind) VALUES (?1, ?2, ?3)",
                params![
                    dependency.predecessor,
                    dependency.successor,
                    dependency_kind_name(dependency.kind)
                ],
            )?;
        }
        transaction.commit()
    }

    pub fn graph(&self, proposal_id: String) -> Result<TaskGraph, rusqlite::Error> {
        let mut tasks_stmt = self
            .conn
            .prepare("SELECT contract_json FROM acc_tasks ORDER BY task_id")?;
        let task_rows = tasks_stmt.query_map([], |row| row.get::<_, String>(0))?;
        let tasks = task_rows
            .map(|json| serde_json::from_str(&json?).map_err(|_| rusqlite::Error::InvalidQuery))
            .collect::<Result<Vec<TaskContract>, _>>()?;
        let mut dependency_stmt = self.conn.prepare(
            "SELECT predecessor, successor, kind FROM acc_dependencies ORDER BY predecessor, successor, kind",
        )?;
        let dependency_rows = dependency_stmt.query_map([], |row| {
            let kind: String = row.get(2)?;
            Ok(TaskDependency {
                predecessor: row.get(0)?,
                successor: row.get(1)?,
                kind: dependency_kind(&kind).ok_or(rusqlite::Error::InvalidQuery)?,
            })
        })?;
        let dependencies = dependency_rows.collect::<Result<Vec<_>, _>>()?;
        TaskGraph::validate(TaskGraphProposal {
            proposal_id,
            tasks,
            dependencies,
        })
        .map_err(|_| rusqlite::Error::InvalidQuery)
    }

    pub fn set_task_state(
        &self,
        task_id: &str,
        state: AccTaskState,
    ) -> Result<(), rusqlite::Error> {
        self.conn.execute(
            "UPDATE acc_tasks SET state = ?2 WHERE task_id = ?1",
            params![task_id, state_name(state)],
        )?;
        Ok(())
    }

    pub fn put_manifest(&self, manifest: &ContextManifest) -> Result<(), rusqlite::Error> {
        if !manifest.verify_hash() {
            return Err(rusqlite::Error::InvalidQuery);
        }
        let json = serde_json::to_string(manifest).expect("manifest serializable");
        self.conn.execute("INSERT INTO acc_context_manifests (manifest_id, task_id, manifest_json, manifest_sha256) VALUES (?1, ?2, ?3, ?4)", params![manifest.manifest_id, manifest.task_id, json, manifest.manifest_sha256])?;
        Ok(())
    }

    pub fn put_artifact(
        &self,
        artifact_id: &str,
        task_id: &str,
        sha256: &str,
        version: u32,
    ) -> Result<(), rusqlite::Error> {
        self.conn.execute(
            "INSERT INTO acc_artifacts (artifact_id, task_id, sha256, version) VALUES (?1, ?2, ?3, ?4)",
            params![artifact_id, task_id, sha256, version],
        )?;
        Ok(())
    }

    /// SQLite's unique event ID makes replay idempotent, and `sequence` is the
    /// only durable order returned to subscribers.
    pub fn append_event(&self, event: &CollaborationEvent) -> Result<Option<u64>, rusqlite::Error> {
        let json = serde_json::to_string(event).expect("event serializable");
        let changed = self.conn.execute(
            "INSERT OR IGNORE INTO acc_events (event_id, task_id, event_json) VALUES (?1, ?2, ?3)",
            params![event.event_id, event.task_id, json],
        )?;
        if changed == 0 {
            return Ok(None);
        }
        Ok(Some(self.conn.last_insert_rowid() as u64))
    }

    pub fn events_after(
        &self,
        sequence: u64,
    ) -> Result<Vec<(u64, CollaborationEvent)>, rusqlite::Error> {
        let mut stmt = self.conn.prepare(
            "SELECT sequence, event_json FROM acc_events WHERE sequence > ?1 ORDER BY sequence",
        )?;
        let rows = stmt.query_map(params![sequence as i64], |row| {
            let json: String = row.get(1)?;
            let event = serde_json::from_str(&json).map_err(|_| rusqlite::Error::InvalidQuery)?;
            Ok((row.get::<_, i64>(0)? as u64, event))
        })?;
        rows.collect()
    }

    pub fn manifest(&self, id: &str) -> Result<Option<ContextManifest>, rusqlite::Error> {
        let result = self.conn.query_row(
            "SELECT manifest_json FROM acc_context_manifests WHERE manifest_id = ?1",
            params![id],
            |row| row.get::<_, String>(0),
        );
        match result {
            Ok(json) => serde_json::from_str(&json)
                .map(Some)
                .map_err(|_| rusqlite::Error::InvalidQuery),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(error) => Err(error),
        }
    }

    pub fn task_states(&self) -> Result<Vec<(String, String)>, rusqlite::Error> {
        let mut stmt = self
            .conn
            .prepare("SELECT task_id, state FROM acc_tasks ORDER BY task_id")?;
        let rows = stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
        rows.collect()
    }

    pub fn artifact_refs(&self) -> Result<Vec<(String, String, String, u32)>, rusqlite::Error> {
        let mut stmt = self.conn.prepare(
            "SELECT artifact_id, task_id, sha256, version FROM acc_artifacts ORDER BY artifact_id",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get::<_, i64>(3)? as u32,
            ))
        })?;
        rows.collect()
    }
}

fn state_name(state: AccTaskState) -> &'static str {
    match state {
        AccTaskState::Proposed => "PROPOSED",
        AccTaskState::Ready => "READY",
        AccTaskState::Assigned => "ASSIGNED",
        AccTaskState::ResultSubmitted => "RESULT_SUBMITTED",
        AccTaskState::Reviewing => "REVIEWING",
        AccTaskState::Accepted => "ACCEPTED",
        AccTaskState::RevisionRequired => "REVISION_REQUIRED",
        AccTaskState::Failed => "FAILED",
    }
}

fn dependency_kind_name(kind: DependencyKind) -> &'static str {
    match kind {
        DependencyKind::DependsOn => "DEPENDS_ON",
        DependencyKind::Blocks => "BLOCKS",
        DependencyKind::ProducesFor => "PRODUCES_FOR",
        DependencyKind::RelatedTo => "RELATED_TO",
    }
}

fn dependency_kind(value: &str) -> Option<DependencyKind> {
    match value {
        "DEPENDS_ON" => Some(DependencyKind::DependsOn),
        "BLOCKS" => Some(DependencyKind::Blocks),
        "PRODUCES_FOR" => Some(DependencyKind::ProducesFor),
        "RELATED_TO" => Some(DependencyKind::RelatedTo),
        _ => None,
    }
}
