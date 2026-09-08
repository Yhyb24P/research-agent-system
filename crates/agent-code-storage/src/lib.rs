//! Small SQLite journal for durable Agent state.

mod journal;
mod schema;

pub use journal::SqliteJournal;
pub use schema::SCHEMA;

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
}
