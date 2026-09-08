use crate::error::JournalError;
use crate::state::{AgentState, ToolCallId, ToolCallState};

/// Persists Agent transitions and tool-call states so a crashed session can be
/// recovered without blindly replaying non-idempotent side effects.
pub trait Journal {
    /// Record a session state transition.
    fn record_transition(&mut self, from: AgentState, to: AgentState) -> Result<(), JournalError>;

    /// Record a tool-call state change.
    fn record_tool_state(
        &mut self,
        call: ToolCallId,
        state: ToolCallState,
    ) -> Result<(), JournalError>;

    /// Return the tool calls still in the non-terminal `Running` state.
    fn running_tools(&self) -> Vec<ToolCallId>;
}

/// A journal backed by in-memory vectors. Used by tests and when no durable
/// store is attached.
#[derive(Debug, Default)]
pub struct InMemoryJournal {
    transitions: Vec<(AgentState, AgentState)>,
    tool_states: Vec<(ToolCallId, ToolCallState)>,
}

impl InMemoryJournal {
    /// An empty in-memory journal.
    pub fn new() -> Self {
        Self::default()
    }

    /// The recorded transitions, in order.
    pub fn transitions(&self) -> &[(AgentState, AgentState)] {
        &self.transitions
    }
}

impl Journal for InMemoryJournal {
    fn record_transition(&mut self, from: AgentState, to: AgentState) -> Result<(), JournalError> {
        self.transitions.push((from, to));
        Ok(())
    }

    fn record_tool_state(
        &mut self,
        call: ToolCallId,
        state: ToolCallState,
    ) -> Result<(), JournalError> {
        self.tool_states.push((call, state));
        Ok(())
    }

    fn running_tools(&self) -> Vec<ToolCallId> {
        // The latest recorded state per call wins; only calls whose latest
        // state is `Running` count. Matches the SQLite upsert semantics.
        let mut latest: std::collections::HashMap<ToolCallId, ToolCallState> =
            std::collections::HashMap::new();
        for (call, state) in &self.tool_states {
            latest.insert(*call, *state);
        }
        latest
            .into_iter()
            .filter(|(_, s)| matches!(s, ToolCallState::Running))
            .map(|(c, _)| c)
            .collect()
    }
}
