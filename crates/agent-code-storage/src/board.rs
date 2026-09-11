//! The SQLite implementation of the team's durable task board.

use agent_code_team::{
    AgentMessage, ArtifactMeta, BoardError, TaskAttempt, TaskBoard, TaskKind, TaskRecord,
    TaskStatus,
};
use rusqlite::{params, Connection, Row};

use crate::schema::{migrate, SCHEMA};

/// A durable task board backed by a SQLite connection.
pub struct SqliteTaskBoard {
    conn: Connection,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExternalRuntimeBinding {
    pub team_task_id: u64,
    pub attempt: u32,
    pub agent_id: String,
    pub runtime_kind: String,
    pub native_thread_id: Option<String>,
    pub native_turn_id: Option<String>,
    pub lifecycle_state: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeCollaborationRecord {
    pub team_task_id: u64,
    pub attempt: u32,
    pub runtime_kind: String,
    pub native_call_id: String,
    pub kind: String,
    pub payload_summary: String,
    pub response_summary: Option<String>,
}

impl SqliteTaskBoard {
    /// Open a board on a connection, applying the schema and migrating.
    pub fn open(mut conn: Connection) -> Result<Self, rusqlite::Error> {
        conn.execute_batch(SCHEMA)?;
        migrate(&mut conn)?;
        Ok(Self { conn })
    }

    /// Open a board on an in-memory database.
    pub fn in_memory() -> Result<Self, rusqlite::Error> {
        Self::open(Connection::open_in_memory()?)
    }

    /// The database's schema version.
    pub fn schema_version(&self) -> Result<i32, rusqlite::Error> {
        self.conn
            .pragma_query_value(None, "user_version", |r| r.get(0))
    }

    pub fn upsert_external_binding(
        &self,
        binding: &ExternalRuntimeBinding,
    ) -> Result<(), rusqlite::Error> {
        self.conn.execute("INSERT INTO external_runtime_bindings (team_task_id, attempt, agent_id, runtime_kind, native_thread_id, native_turn_id, lifecycle_state) VALUES (?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(team_task_id,attempt) DO UPDATE SET native_thread_id=excluded.native_thread_id,native_turn_id=excluded.native_turn_id,lifecycle_state=excluded.lifecycle_state", params![binding.team_task_id as i64,binding.attempt as i64,binding.agent_id,binding.runtime_kind,binding.native_thread_id,binding.native_turn_id,binding.lifecycle_state])?;
        Ok(())
    }

    pub fn external_binding(
        &self,
        task: u64,
        attempt: u32,
    ) -> Result<Option<ExternalRuntimeBinding>, rusqlite::Error> {
        let row = self.conn.query_row("SELECT team_task_id,attempt,agent_id,runtime_kind,native_thread_id,native_turn_id,lifecycle_state FROM external_runtime_bindings WHERE team_task_id=?1 AND attempt=?2", params![task as i64,attempt as i64], |r| Ok(ExternalRuntimeBinding { team_task_id:r.get::<_,i64>(0)? as u64,attempt:r.get::<_,i64>(1)? as u32,agent_id:r.get(2)?,runtime_kind:r.get(3)?,native_thread_id:r.get(4)?,native_turn_id:r.get(5)?,lifecycle_state:r.get(6)? }));
        match row {
            Ok(v) => Ok(Some(v)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Idempotently persists a bounded external tool request and its response.
    pub fn record_runtime_collaboration(
        &self,
        record: &RuntimeCollaborationRecord,
    ) -> Result<bool, rusqlite::Error> {
        Ok(self.conn.execute("INSERT INTO runtime_collaboration_records (team_task_id,attempt,runtime_kind,native_call_id,kind,payload_summary,response_summary) VALUES (?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(runtime_kind,native_call_id) DO NOTHING", params![record.team_task_id as i64,record.attempt as i64,record.runtime_kind,record.native_call_id,record.kind,record.payload_summary,record.response_summary])? == 1)
    }

    pub fn runtime_collaboration(
        &self,
        task: u64,
        attempt: u32,
    ) -> Result<Vec<RuntimeCollaborationRecord>, rusqlite::Error> {
        let mut s=self.conn.prepare("SELECT team_task_id,attempt,runtime_kind,native_call_id,kind,payload_summary,response_summary FROM runtime_collaboration_records WHERE team_task_id=?1 AND attempt=?2 ORDER BY id")?;
        let rows = s.query_map(params![task as i64, attempt as i64], |r| {
            Ok(RuntimeCollaborationRecord {
                team_task_id: r.get::<_, i64>(0)? as u64,
                attempt: r.get::<_, i64>(1)? as u32,
                runtime_kind: r.get(2)?,
                native_call_id: r.get(3)?,
                kind: r.get(4)?,
                payload_summary: r.get(5)?,
                response_summary: r.get(6)?,
            })
        })?;
        rows.collect()
    }

    /// The underlying connection, for direct queries in tests.
    #[cfg(test)]
    pub(crate) fn conn(&self) -> &Connection {
        &self.conn
    }
}

impl TaskBoard for SqliteTaskBoard {
    fn create_task(
        &mut self,
        objective: &str,
        parent: Option<u64>,
        kind: TaskKind,
        target: Option<String>,
    ) -> Result<u64, BoardError> {
        self.conn
            .execute(
                "INSERT INTO team_tasks (objective, parent_task, kind, target, status)
                 VALUES (?1, ?2, ?3, ?4, 'pending')",
                params![objective, parent.map(|p| p as i64), kind.as_str(), target],
            )
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        Ok(self.conn.last_insert_rowid() as u64)
    }

    fn assign(&mut self, task: u64, agent: &str) -> Result<(), BoardError> {
        let n = self
            .conn
            .execute(
                "UPDATE team_tasks SET assignee = ?2, status = 'assigned' WHERE id = ?1",
                params![task as i64, agent],
            )
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        if n == 0 {
            return Err(BoardError::UnknownTask(task));
        }
        Ok(())
    }

    fn set_status(&mut self, task: u64, status: TaskStatus) -> Result<(), BoardError> {
        let n = self
            .conn
            .execute(
                "UPDATE team_tasks SET status = ?2 WHERE id = ?1",
                params![task as i64, status.as_str()],
            )
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        if n == 0 {
            return Err(BoardError::UnknownTask(task));
        }
        Ok(())
    }

    fn record_attempt(&mut self, attempt: &TaskAttempt) -> Result<(), BoardError> {
        self.conn
            .execute(
                "INSERT INTO team_task_runs (task_id, attempt, agent_id, status, result, error)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![
                    attempt.task_id as i64,
                    attempt.attempt as i64,
                    attempt.agent_id,
                    attempt.status.as_str(),
                    attempt.result,
                    attempt.error,
                ],
            )
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        Ok(())
    }

    fn complete_attempt(&mut self, attempt: &TaskAttempt) -> Result<(), BoardError> {
        let n = self
            .conn
            .execute(
                "UPDATE team_task_runs
                 SET status = ?3, result = ?4, error = ?5
                 WHERE task_id = ?1 AND attempt = ?2",
                params![
                    attempt.task_id as i64,
                    attempt.attempt as i64,
                    attempt.status.as_str(),
                    attempt.result,
                    attempt.error,
                ],
            )
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        if n == 0 {
            return Err(BoardError::UnknownTask(attempt.task_id));
        }
        Ok(())
    }

    fn record_message(&mut self, message: &AgentMessage) -> Result<(), BoardError> {
        self.conn
            .execute(
                "INSERT INTO messages (from_agent, to_agent, body) VALUES (?1, ?2, ?3)",
                params![message.from_agent, message.to_agent, message.body],
            )
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        Ok(())
    }

    fn record_artifact(&mut self, task: u64, artifact: &ArtifactMeta) -> Result<(), BoardError> {
        self.conn
            .execute(
                "INSERT INTO artifacts (task_id, path, sha256) VALUES (?1, ?2, ?3)",
                params![task as i64, artifact.path, artifact.sha256],
            )
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        Ok(())
    }

    fn task(&self, id: u64) -> Result<Option<TaskRecord>, BoardError> {
        let row = self.conn.query_row(
            "SELECT id, objective, parent_task, kind, target, assignee, status
             FROM team_tasks WHERE id = ?1",
            params![id as i64],
            row_to_task,
        );
        match row {
            Ok(record) => Ok(Some(record)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(BoardError::Storage(e.to_string())),
        }
    }

    fn attempts(&self, task: u64) -> Result<Vec<TaskAttempt>, BoardError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT task_id, attempt, agent_id, status, result, error
                 FROM team_task_runs WHERE task_id = ?1 ORDER BY attempt",
            )
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        let rows = stmt
            .query_map(params![task as i64], row_to_attempt)
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        rows.map(|r| r.map_err(|e| BoardError::Storage(e.to_string())))
            .collect()
    }

    fn messages_to(&self, agent: &str) -> Result<Vec<AgentMessage>, BoardError> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT from_agent, to_agent, body FROM messages WHERE to_agent = ?1 ORDER BY id",
            )
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        let rows = stmt
            .query_map(params![agent], |r| {
                Ok(AgentMessage {
                    from_agent: r.get(0)?,
                    to_agent: r.get(1)?,
                    body: r.get(2)?,
                })
            })
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        rows.map(|r| r.map_err(|e| BoardError::Storage(e.to_string())))
            .collect()
    }

    fn artifacts(&self, task: u64) -> Result<Vec<ArtifactMeta>, BoardError> {
        let mut stmt = self
            .conn
            .prepare("SELECT path, sha256 FROM artifacts WHERE task_id = ?1 ORDER BY id")
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        let rows = stmt
            .query_map(params![task as i64], |r| {
                Ok(ArtifactMeta {
                    path: r.get(0)?,
                    sha256: r.get(1)?,
                })
            })
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        rows.map(|r| r.map_err(|e| BoardError::Storage(e.to_string())))
            .collect()
    }

    fn task_ids(&self) -> Result<Vec<u64>, BoardError> {
        let mut stmt = self
            .conn
            .prepare("SELECT id FROM team_tasks ORDER BY id")
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        let rows = stmt
            .query_map([], |r| r.get::<_, i64>(0))
            .map_err(|e| BoardError::Storage(e.to_string()))?;
        rows.map(|r| {
            r.map(|v| v as u64)
                .map_err(|e| BoardError::Storage(e.to_string()))
        })
        .collect()
    }
}

fn row_to_task(r: &Row) -> rusqlite::Result<TaskRecord> {
    let kind = r.get::<_, String>(3)?;
    let status = r.get::<_, String>(6)?;
    Ok(TaskRecord {
        id: r.get::<_, i64>(0)? as u64,
        objective: r.get(1)?,
        parent_task: r.get::<_, Option<i64>>(2)?.map(|p| p as u64),
        kind: TaskKind::restore(&kind).ok_or(rusqlite::Error::InvalidQuery)?,
        target: r.get(4)?,
        assignee: r.get(5)?,
        status: TaskStatus::restore(&status).ok_or(rusqlite::Error::InvalidQuery)?,
    })
}

fn row_to_attempt(r: &Row) -> rusqlite::Result<TaskAttempt> {
    let status = r.get::<_, String>(3)?;
    Ok(TaskAttempt {
        task_id: r.get::<_, i64>(0)? as u64,
        attempt: r.get::<_, i64>(1)? as u32,
        agent_id: r.get(2)?,
        status: TaskStatus::restore(&status).ok_or(rusqlite::Error::InvalidQuery)?,
        result: r.get(4)?,
        error: r.get(5)?,
    })
}
