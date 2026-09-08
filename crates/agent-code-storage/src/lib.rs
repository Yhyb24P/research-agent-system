//! Small SQLite journal for durable Agent state.

mod journal;
mod schema;

pub use journal::SqliteJournal;
pub use schema::SCHEMA;

#[cfg(test)]
mod tests {
    use agent_code_core::{AgentState, Journal, ToolCallId, ToolCallState};
    use rusqlite::Connection;

    use super::SqliteJournal;

    #[test]
    fn persists_a_running_tool_across_reopen() {
        let path = std::env::temp_dir().join(format!("agent_code_r1_{}.db", std::process::id()));
        let _ = std::fs::remove_file(&path);
        {
            let conn = Connection::open(&path).expect("open db");
            let mut journal = SqliteJournal::open(conn).expect("apply schema");
            journal
                .record_transition(AgentState::Initializing, AgentState::Observing)
                .expect("record transition");
            journal
                .record_tool_state(ToolCallId::new(3), ToolCallState::Running)
                .expect("record tool state");
        }
        let conn = Connection::open(&path).expect("reopen db");
        let journal = SqliteJournal::open(conn).expect("apply schema");
        assert_eq!(journal.running_tools(), vec![ToolCallId::new(3)]);
        let _ = std::fs::remove_file(&path);
    }
}
