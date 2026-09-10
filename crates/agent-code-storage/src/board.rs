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
