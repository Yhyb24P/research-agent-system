use crate::error::JournalError;
use crate::journal::Journal;
use crate::state::{AgentState, ToolCallId, ToolCallState};

/// A single native Coding Agent session. Drives the state machine and journals
/// every transition so a crash can be recovered.
pub struct Session<J: Journal> {
    state: AgentState,
    journal: J,
}

impl<J: Journal> Session<J> {
    /// Create a brand-new session in the initial state.
    pub fn create(mut journal: J) -> Result<Self, JournalError> {
        journal.create(AgentState::Initializing)?;
        Ok(Self {
            state: AgentState::Initializing,
            journal,
        })
    }

    /// Recover an existing session from its journal, marking any `Running`
    /// tool as `Interrupted` so it is not replayed.
    pub fn recover(mut journal: J) -> Result<Self, JournalError> {
        let state = journal
            .current_state()?
            .ok_or(JournalError::UnknownSession)?;
        for call in journal.running_tools()? {
            journal.record_tool_state(call, ToolCallState::Interrupted)?;
        }
        Ok(Self { state, journal })
    }

    /// The current state.
    pub fn state(&self) -> AgentState {
        self.state
    }

    /// Move to `Observing`.
    pub fn observe(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::Observing)
    }

    /// Move to `WaitingModel`.
    pub fn wait_model(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::WaitingModel)
    }

    /// Begin executing a tool call. One atomic persisted step: transition to
    /// `ExecutingTool { call }` and mark the tool `Running`. The in-memory
    /// state only advances after the journal step succeeds.
    pub fn begin_tool(&mut self, call: ToolCallId) -> Result<(), JournalError> {
        let to = AgentState::ExecutingTool { call };
        if !Self::legal(self.state, to) {
            return Err(JournalError::IllegalTransition);
        }
        let from = self.state;
        self.journal.begin_tool_call(from, call)?;
        self.state = to;
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

    /// Move to `Verifying`.
    pub fn verify(&mut self) -> Result<(), JournalError> {
        self.transition_to(AgentState::Verifying)
    }

    /// Move to `Delivering`.
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

    fn finish_tool(
        &mut self,
        call: ToolCallId,
        tool_state: ToolCallState,
    ) -> Result<(), JournalError> {
        let to = AgentState::Observing;
        match self.state {
            AgentState::ExecutingTool { call: active } if active == call => {}
            _ => return Err(JournalError::UnknownToolCall),
        }
        self.journal.finish_tool_call(call, tool_state, to)?;
        self.state = to;
        Ok(())
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
        if matches!(to, AgentState::Failed) {
            return true;
        }
        matches!(
            (from, to),
            (AgentState::Initializing, AgentState::Observing)
                | (AgentState::Observing, AgentState::WaitingModel)
                | (AgentState::WaitingModel, AgentState::ExecutingTool { .. })
                | (AgentState::ExecutingTool { .. }, AgentState::Observing)
                | (AgentState::Observing, AgentState::Verifying)
                | (AgentState::Verifying, AgentState::Observing)
                | (AgentState::Verifying, AgentState::Delivering)
                | (AgentState::Delivering, AgentState::Completed)
                | (AgentState::Observing, AgentState::RolledBack)
                | (AgentState::Verifying, AgentState::RolledBack)
                | (AgentState::ExecutingTool { .. }, AgentState::RolledBack)
        )
    }
}
