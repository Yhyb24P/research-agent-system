use agent_code_core::{AgentState, Journal, JournalError, SessionId, ToolCallId, ToolCallState};
use rusqlite::{params, Connection, Transaction};

use crate::schema::SCHEMA;

/// A durable journal backed by a SQLite connection, bound to one session.
pub struct SqliteJournal {
    conn: Connection,
    session: SessionId,
}

impl SqliteJournal {
    /// The underlying connection, for the observation store.
    pub(crate) fn conn(&self) -> &Connection {
        &self.conn
    }

    /// The bound session, for the observation store.
    pub(crate) fn session(&self) -> &SessionId {
        &self.session
    }
}

impl SqliteJournal {
    /// Open a connection, apply the schema, and bind to `session`.
    pub fn open(conn: Connection, session: SessionId) -> Result<Self, rusqlite::Error> {
        conn.execute_batch(SCHEMA)?;
        Ok(Self { conn, session })
    }

    /// Open a journal on an in-memory database, bound to `session`.
    pub fn in_memory(session: SessionId) -> Result<Self, rusqlite::Error> {
        Self::open(Connection::open_in_memory()?, session)
    }
}

impl Journal for SqliteJournal {
    fn create(&mut self, initial: AgentState) -> Result<(), JournalError> {
        let sid = self.session.as_str();
        let active = initial.active_call().map(|c| c.as_u64());
        self.conn
            .execute(
                "INSERT INTO sessions (id, state, active_call, created_at) VALUES (?1, ?2, ?3, ?4)",
                params![sid, initial.as_str(), active, now()],
            )
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        Ok(())
    }

    fn current_state(&self) -> Result<Option<AgentState>, JournalError> {
        let sid = self.session.as_str();
        let mut stmt = self
            .conn
            .prepare("SELECT state, active_call FROM sessions WHERE id = ?1")
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        let mut rows = stmt
            .query_map(params![sid], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, Option<i64>>(1)?))
            })
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        match rows.next() {
            Some(row) => {
                let (variant, active) = row.map_err(|e| JournalError::Storage(e.to_string()))?;
                let active = active.map(|id| ToolCallId::new(id as u64));
                let state = AgentState::restore(&variant, active)
                    .ok_or_else(|| JournalError::Storage("unknown state".into()))?;
                Ok(Some(state))
            }
            None => Ok(None),
        }
    }

    fn record_transition(&mut self, from: AgentState, to: AgentState) -> Result<(), JournalError> {
        let sid = self.session.as_str();
        let tx = self
            .conn
            .transaction()
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        insert_transition(&tx, sid, &from, &to)?;
        update_state(&tx, sid, &to)?;
        tx.commit()
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        Ok(())
    }

    fn record_tool_state(
        &mut self,
        call: ToolCallId,
        state: ToolCallState,
    ) -> Result<(), JournalError> {
        let sid = self.session.as_str();
        upsert_tool_state(&self.conn, sid, call, state.as_str())
    }

    fn begin_tool_call(&mut self, from: AgentState, call: ToolCallId) -> Result<(), JournalError> {
        let to = AgentState::ExecutingTool { call };
        let sid = self.session.as_str();
        let tx = self
            .conn
            .transaction()
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        insert_transition(&tx, sid, &from, &to)?;
        update_state(&tx, sid, &to)?;
        upsert_tool_state(&tx, sid, call, ToolCallState::Running.as_str())?;
        tx.commit()
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        Ok(())
    }

    fn finish_tool_call(
        &mut self,
        call: ToolCallId,
        tool_state: ToolCallState,
        to: AgentState,
    ) -> Result<(), JournalError> {
        let from = AgentState::ExecutingTool { call };
        let sid = self.session.as_str();
        let tx = self
            .conn
            .transaction()
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        upsert_tool_state(&tx, sid, call, tool_state.as_str())?;
        insert_transition(&tx, sid, &from, &to)?;
        update_state(&tx, sid, &to)?;
        tx.commit()
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        Ok(())
    }

    fn running_tools(&self) -> Result<Vec<ToolCallId>, JournalError> {
        let sid = self.session.as_str();
        let mut stmt = self
            .conn
            .prepare("SELECT call_id FROM tool_calls WHERE session_id = ?1 AND state = ?2")
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        let rows = stmt
            .query_map(params![sid, ToolCallState::Running.as_str()], |row| {
                row.get::<_, i64>(0)
            })
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        let mut out = Vec::new();
        for row in rows {
            let id = row.map_err(|e| JournalError::Storage(e.to_string()))?;
            out.push(ToolCallId::new(id as u64));
        }
        Ok(out)
    }

    fn highest_call_id(&self) -> Result<Option<u64>, JournalError> {
        let sid = self.session.as_str();
        let max: Option<i64> = self
            .conn
            .query_row(
                "SELECT MAX(call_id) FROM tool_calls WHERE session_id = ?1",
                params![sid],
                |row| row.get(0),
            )
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        Ok(max.map(|m| m as u64))
    }

    fn record_turn(&mut self, decision: &str, error: Option<&str>) -> Result<(), JournalError> {
        let sid = self.session.as_str();
        self.conn
            .execute(
                "INSERT INTO agent_turns (session_id, decision, error) VALUES (?1, ?2, ?3)",
                params![sid, decision, error],
            )
            .map_err(|e| JournalError::Storage(e.to_string()))?;
        Ok(())
    }

    fn record_tool_requested(
        &mut self,
        call: ToolCallId,
        request: &str,
    ) -> Result<(), JournalError> {
        let sid = self.session.as_str();
        upsert_tool_requested(&self.conn, sid, call, request)
    }
}

fn insert_transition(
    tx: &Transaction,
    sid: &str,
    from: &AgentState,
    to: &AgentState,
) -> Result<(), JournalError> {
    tx.execute(
        "INSERT INTO transitions (session_id, from_state, to_state) VALUES (?1, ?2, ?3)",
        params![sid, from.as_str(), to.as_str()],
    )
    .map_err(|e| JournalError::Storage(e.to_string()))?;
    Ok(())
}

fn update_state(tx: &Transaction, sid: &str, state: &AgentState) -> Result<(), JournalError> {
    let active = state.active_call().map(|c| c.as_u64());
    tx.execute(
        "UPDATE sessions SET state = ?2, active_call = ?3 WHERE id = ?1",
        params![sid, state.as_str(), active],
    )
    .map_err(|e| JournalError::Storage(e.to_string()))?;
    Ok(())
}

fn upsert_tool_state(
    conn: &Connection,
    sid: &str,
    call: ToolCallId,
    state: &str,
) -> Result<(), JournalError> {
    // Preserves any recorded `request` payload: only `state` is touched on
    // conflict, so the durable request written at the `Requested` boundary
    // survives the later `Running`/terminal updates.
    conn.execute(
        "INSERT INTO tool_calls (session_id, call_id, state) VALUES (?1, ?2, ?3)
         ON CONFLICT(session_id, call_id) DO UPDATE SET state = excluded.state",
        params![sid, call.as_u64(), state],
    )
    .map_err(|e| JournalError::Storage(e.to_string()))?;
    Ok(())
}

/// Record the `Requested` boundary: store the typed request payload alongside
/// the state. On conflict (a later lifecycle step already ran) the payload is
/// refreshed but the newer state is kept.
fn upsert_tool_requested(
    conn: &Connection,
    sid: &str,
    call: ToolCallId,
    request: &str,
) -> Result<(), JournalError> {
    conn.execute(
        "INSERT INTO tool_calls (session_id, call_id, state, request) VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(session_id, call_id) DO UPDATE SET request = excluded.request",
        params![
            sid,
            call.as_u64(),
            ToolCallState::Requested.as_str(),
            request
        ],
    )
    .map_err(|e| JournalError::Storage(e.to_string()))?;
    Ok(())
}

pub(crate) fn now() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs().to_string())
        .unwrap_or_default()
}
