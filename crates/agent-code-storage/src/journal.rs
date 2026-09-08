use agent_code_core::{AgentState, Journal, JournalError, ToolCallId, ToolCallState};
use rusqlite::{params, Connection};

use crate::schema::SCHEMA;

const SESSION: &str = "s";

/// A durable journal backed by a SQLite connection.
pub struct SqliteJournal {
    conn: Connection,
}

impl SqliteJournal {
    /// Open a connection and apply the journal schema.
    pub fn open(conn: Connection) -> Result<Self, rusqlite::Error> {
        conn.execute_batch(SCHEMA)?;
        Ok(Self { conn })
    }

    /// Open a journal on an in-memory database.
    pub fn in_memory() -> Result<Self, rusqlite::Error> {
        Self::open(Connection::open_in_memory()?)
    }
}

impl Journal for SqliteJournal {
    fn record_transition(&mut self, from: AgentState, to: AgentState) -> Result<(), JournalError> {
        self.conn
            .execute(
                "INSERT INTO transitions (session_id, from_state, to_state) VALUES (?1, ?2, ?3)",
                params![SESSION, format!("{from:?}"), format!("{to:?}")],
            )
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        Ok(())
    }

    fn record_tool_state(
        &mut self,
        call: ToolCallId,
        state: ToolCallState,
    ) -> Result<(), JournalError> {
        self.conn
            .execute(
                "INSERT INTO tool_calls (session_id, call_id, state) VALUES (?1, ?2, ?3)
                 ON CONFLICT(session_id, call_id) DO UPDATE SET state = excluded.state",
                params![SESSION, call.as_u64(), format!("{state:?}")],
            )
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        Ok(())
    }

    fn running_tools(&self) -> Vec<ToolCallId> {
        let state = format!("{:?}", ToolCallState::Running);
        let mut stmt = self
            .conn
            .prepare("SELECT call_id FROM tool_calls WHERE session_id = ?1 AND state = ?2")
            .expect("prepare running_tools query");
        let rows = stmt
            .query_map(params![SESSION, state], |row| row.get::<_, i64>(0))
            .expect("query running_tools");
        rows.filter_map(Result::ok)
            .map(|id| ToolCallId::new(id as u64))
            .collect()
    }
}
