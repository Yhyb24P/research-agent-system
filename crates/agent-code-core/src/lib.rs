//! Durable state machine for the native Rust Coding Agent.
//!
//! This crate owns the session state machine, the journal boundary, and crash
//! recovery. It has no external dependencies so the core stays small and
//! testable.

mod error;
mod journal;
mod recovery;
mod session;
mod state;

pub use error::JournalError;
pub use journal::{InMemoryJournal, Journal};
pub use recovery::recover_interrupted_tools;
pub use session::Session;
pub use state::{AgentState, ToolCallId, ToolCallState};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn happy_path_reaches_completed() {
        let mut session = Session::new(InMemoryJournal::new());
        session.observe().unwrap();
        session.wait_model().unwrap();
        let call = ToolCallId::new(1);
        session.begin_tool(call).unwrap();
        session.tool_succeeded(call).unwrap();
        session.verify().unwrap();
        session.deliver().unwrap();
        session.complete().unwrap();
        assert_eq!(session.state(), AgentState::Completed);
    }

    #[test]
    fn illegal_transition_is_rejected() {
        let mut session = Session::new(InMemoryJournal::new());
        assert!(session.verify().is_err());
    }

    #[test]
    fn terminal_state_rejects_all_transitions() {
        let mut session = Session::new(InMemoryJournal::new());
        session.fail().unwrap();
        assert!(session.observe().is_err());
    }

    #[test]
    fn inactive_tool_call_is_rejected() {
        let mut session = Session::new(InMemoryJournal::new());
        session.observe().unwrap();
        session.wait_model().unwrap();
        session.begin_tool(ToolCallId::new(1)).unwrap();
        assert!(session.tool_succeeded(ToolCallId::new(2)).is_err());
    }

    #[test]
    fn recovery_marks_running_tools_interrupted() {
        let mut journal = InMemoryJournal::new();
        let call = ToolCallId::new(7);
        journal
            .record_tool_state(call, ToolCallState::Requested)
            .unwrap();
        journal
            .record_tool_state(call, ToolCallState::Running)
            .unwrap();
        let interrupted = recover_interrupted_tools(&mut journal).unwrap();
        assert_eq!(interrupted, vec![call]);
        assert!(journal.running_tools().is_empty());
    }
}
