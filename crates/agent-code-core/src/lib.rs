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
pub use state::{AgentState, SessionId, ToolCallId, ToolCallState};

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh() -> Session<InMemoryJournal> {
        Session::create(InMemoryJournal::new(SessionId::new("s"))).expect("create session")
    }

    #[test]
    fn happy_path_reaches_completed() {
        let mut s = fresh();
        s.observe().unwrap();
        s.wait_model().unwrap();
        let call = ToolCallId::new(1);
        s.begin_tool(call).unwrap();
        s.tool_succeeded(call).unwrap();
        s.verify().unwrap();
        s.deliver().unwrap();
        s.complete().unwrap();
        assert_eq!(s.state(), AgentState::Completed);
    }

    #[test]
    fn illegal_transition_is_rejected() {
        let mut s = fresh();
        assert!(s.verify().is_err());
    }

    #[test]
    fn terminal_state_rejects_all_transitions() {
        let mut s = fresh();
        s.fail().unwrap();
        assert!(s.observe().is_err());
    }

    #[test]
    fn inactive_tool_call_is_rejected() {
        let mut s = fresh();
        s.observe().unwrap();
        s.wait_model().unwrap();
        s.begin_tool(ToolCallId::new(1)).unwrap();
        assert!(s.tool_succeeded(ToolCallId::new(2)).is_err());
    }

    #[test]
    fn recover_marks_running_tools_interrupted() {
        let mut journal = InMemoryJournal::new(SessionId::new("s"));
        journal.create(AgentState::Initializing).unwrap();
        journal
            .record_transition(AgentState::Initializing, AgentState::Observing)
            .unwrap();
        journal
            .record_transition(AgentState::Observing, AgentState::WaitingModel)
            .unwrap();
        journal
            .begin_tool_call(AgentState::WaitingModel, ToolCallId::new(7))
            .unwrap();
        let interrupted = recover_interrupted_tools(&mut journal).unwrap();
        assert_eq!(interrupted, vec![ToolCallId::new(7)]);
        assert!(journal.running_tools().unwrap().is_empty());
    }

    #[test]
    fn session_recover_loads_persisted_state() {
        let mut journal = InMemoryJournal::new(SessionId::new("s"));
        journal.create(AgentState::Initializing).unwrap();
        journal
            .record_transition(AgentState::Initializing, AgentState::Observing)
            .unwrap();
        let s = Session::recover(journal).unwrap();
        assert_eq!(s.state(), AgentState::Observing);
    }

    #[test]
    fn session_recover_requires_existing_session() {
        let journal = InMemoryJournal::new(SessionId::new("missing"));
        assert!(Session::recover(journal).is_err());
    }

    #[test]
    fn begin_tool_failure_does_not_diverge_state() {
        let journal = FaultyJournal::failing("begin");
        let mut s = Session::create(journal).unwrap();
        s.observe().unwrap();
        s.wait_model().unwrap();
        assert!(s.begin_tool(ToolCallId::new(1)).is_err());
        // The in-memory state did not advance past the failed step.
        assert_eq!(s.state(), AgentState::WaitingModel);
    }

    #[test]
    fn running_tools_fault_propagates_instead_of_panicking() {
        let journal = FaultyJournal::failing("running");
        assert!(journal.running_tools().is_err());
    }

    /// A journal that fails a single named operation, to prove the session
    /// surfaces errors instead of panicking or diverging.
    struct FaultyJournal {
        fail: &'static str,
    }

    impl FaultyJournal {
        fn failing(op: &'static str) -> Self {
            Self { fail: op }
        }
    }

    impl Journal for FaultyJournal {
        fn create(&mut self, _initial: AgentState) -> Result<(), JournalError> {
            if self.fail == "create" {
                return Err(JournalError::Storage("injected".into()));
            }
            Ok(())
        }

        fn current_state(&self) -> Result<Option<AgentState>, JournalError> {
            if self.fail == "current" {
                return Err(JournalError::Storage("injected".into()));
            }
            Ok(None)
        }

        fn record_transition(
            &mut self,
            _from: AgentState,
            _to: AgentState,
        ) -> Result<(), JournalError> {
            if self.fail == "transition" {
                return Err(JournalError::Storage("injected".into()));
            }
            Ok(())
        }

        fn record_tool_state(
            &mut self,
            _call: ToolCallId,
            _state: ToolCallState,
        ) -> Result<(), JournalError> {
            if self.fail == "tool" {
                return Err(JournalError::Storage("injected".into()));
            }
            Ok(())
        }

        fn begin_tool_call(
            &mut self,
            _from: AgentState,
            _call: ToolCallId,
        ) -> Result<(), JournalError> {
            if self.fail == "begin" {
                return Err(JournalError::Storage("injected".into()));
            }
            Ok(())
        }

        fn finish_tool_call(
            &mut self,
            _call: ToolCallId,
            _tool_state: ToolCallState,
            _to: AgentState,
        ) -> Result<(), JournalError> {
            if self.fail == "finish" {
                return Err(JournalError::Storage("injected".into()));
            }
            Ok(())
        }

        fn running_tools(&self) -> Result<Vec<ToolCallId>, JournalError> {
            if self.fail == "running" {
                return Err(JournalError::Storage("injected".into()));
            }
            Ok(Vec::new())
        }
    }
}
