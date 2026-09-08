use crate::error::JournalError;
use crate::journal::Journal;
use crate::state::{AgentState, ToolCallId, ToolCallState};

/// A single native Coding Agent session. Drives the state machine and journals
/// every transition so a crash can be recovered.
pub struct Session<J: Journal> {
    state: AgentState,
    active_call: Option<ToolCallId>,
    journal: J,
}

impl<J: Journal> Session<J> {
    /// Start a session in `Initializing`.
    pub fn new(journal: J) -> Self {
        Self {
            state: AgentState::Initializing,
            active_call: None,
            journal,
        }
    }

    /// The current state.
    pub fn state(&self) -> AgentState {
        self.state
    }

    /// Move to `Observing`.
    pub fn observe(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::Observing)
    }

    /// Move to `WaitingModel`: the Agent is about to ask the model for a decision.
    pub fn wait_model(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::WaitingModel)
    }

    /// Begin executing a tool call. Journals `Requested`, moves to
    /// `ExecutingTool`, then journals `Running` immediately before the
    /// (possibly non-idempotent) side effect.
    pub fn begin_tool(&mut self, call: ToolCallId) -> Result<(), JournalError> {
        self.journal
            .record_tool_state(call, ToolCallState::Requested)?;
        self.active_call = Some(call);
        self.transition_to(AgentState::ExecutingTool)?;
        self.journal
            .record_tool_state(call, ToolCallState::Running)?;
        Ok(())
    }

    /// Record a successful tool result and return to `Observing`.
    pub fn tool_succeeded(&mut self, call: ToolCallId) -> Result<(), JournalError> {
        self.finish_tool(call, ToolCallState::Succeeded)
    }

    /// Record a failed tool result and return to `Observing`.
    pub fn tool_failed(&mut self, call: ToolCallId) -> Result<(), JournalError> {
        self.finish_tool(call, ToolCallState::Failed)
    }

    /// Move to `Verifying` after a logical edit batch.
    pub fn verify(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::Verifying)
    }

    /// Move to `Delivering` after verification passes.
    pub fn deliver(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::Delivering)
    }

    /// Finish the session successfully.
    pub fn complete(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::Completed)
    }

    /// Fail the session from any non-terminal state.
    pub fn fail(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::Failed)
    }

    /// Roll back a write-bearing batch and end the session.
    pub fn roll_back(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::RolledBack)
    }

    fn finish_tool(&mut self, call: ToolCallId, state: ToolCallState) -> Result<(), JournalError> {
        if self.active_call != Some(call) {
            return Err(JournalError::UnknownToolCall);
        }
        self.journal.record_tool_state(call, state)?;
        self.active_call = None;
        self.transition_to(AgentState::Observing)
    }

    fn transition_to(&mut self, to: AgentState) -> Result<(), JournalError> {
        let from = self.state;
        if !Self::legal(from, to) {
            return Err(JournalError::IllegalTransition);
        }
        self.journal.record_transition(from, to)?;
        self.state = to;
        Ok(())
    }

    /// The transition table. Kept small and explicit.
    fn legal(from: AgentState, to: AgentState) -> bool {
        if from.is_terminal() {
            return false;
        }
        use AgentState::*;
        if to == Failed {
            return true;
        }
        matches!(
            (from, to),
            (Initializing, Observing)
                | (Observing, WaitingModel)
                | (WaitingModel, ExecutingTool)
                | (ExecutingTool, Observing)
                | (Observing, Verifying)
                | (Verifying, Observing)
                | (Verifying, Delivering)
                | (Delivering, Completed)
                | (Observing, RolledBack)
                | (Verifying, RolledBack)
                | (ExecutingTool, RolledBack)
        )
    }
}
