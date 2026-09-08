use std::collections::HashMap;

use crate::error::JournalError;
use crate::state::{AgentState, SessionId, ToolCallId, ToolCallState};

/// Persists one Agent session's transitions and tool-call states so a crashed
/// session can be recovered without blindly replaying non-idempotent side
/// effects. Each journal is bound to a single session.
pub trait Journal {
    /// Create the session in `initial` state.
    fn create(&mut self, initial: AgentState) -> Result<(), JournalError>;

    /// The persisted current state, or None if the session was not created.
    fn current_state(&self) -> Result<Option<AgentState>, JournalError>;

    /// Atomically record a transition and update the persisted session state.
    fn record_transition(&mut self, from: AgentState, to: AgentState) -> Result<(), JournalError>;

    /// Record a tool-call state change for this session.
    fn record_tool_state(
        &mut self,
        call: ToolCallId,
        state: ToolCallState,
    ) -> Result<(), JournalError>;

    /// Atomically begin a tool call: transition `from` to
    /// `ExecutingTool { call }` and mark the tool `Running`, as one step.
    fn begin_tool_call(&mut self, from: AgentState, call: ToolCallId) -> Result<(), JournalError>;

    /// Atomically finish a tool call: mark the tool `tool_state` and transition
    /// to `to`, as one step.
    fn finish_tool_call(
        &mut self,
        call: ToolCallId,
        tool_state: ToolCallState,
        to: AgentState,
    ) -> Result<(), JournalError>;

    /// The tool calls still in the non-terminal `Running` state for this session.
    fn running_tools(&self) -> Result<Vec<ToolCallId>, JournalError>;
}

/// A journal backed by in-memory vectors. Bound to one session.
pub struct InMemoryJournal {
    session: SessionId,
    state: Option<AgentState>,
    transitions: Vec<(AgentState, AgentState)>,
    tool_states: Vec<(ToolCallId, ToolCallState)>,
}

impl InMemoryJournal {
    /// An empty journal bound to `session`.
    pub fn new(session: SessionId) -> Self {
        Self {
            session,
            state: None,
            transitions: Vec::new(),
            tool_states: Vec::new(),
        }
    }

    /// The bound session.
    pub fn session(&self) -> &SessionId {
        &self.session
    }

    /// The recorded transitions, in order.
    pub fn transitions(&self) -> &[(AgentState, AgentState)] {
        &self.transitions
    }
}

impl Journal for InMemoryJournal {
    fn create(&mut self, initial: AgentState) -> Result<(), JournalError> {
        self.state = Some(initial);
        Ok(())
    }

    fn current_state(&self) -> Result<Option<AgentState>, JournalError> {
        Ok(self.state)
    }

    fn record_transition(&mut self, from: AgentState, to: AgentState) -> Result<(), JournalError> {
        self.transitions.push((from, to));
        self.state = Some(to);
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

    fn begin_tool_call(&mut self, from: AgentState, call: ToolCallId) -> Result<(), JournalError> {
        let to = AgentState::ExecutingTool { call };
        self.transitions.push((from, to));
        self.state = Some(to);
        self.tool_states.push((call, ToolCallState::Running));
        Ok(())
    }

    fn finish_tool_call(
        &mut self,
        call: ToolCallId,
        tool_state: ToolCallState,
        to: AgentState,
    ) -> Result<(), JournalError> {
        self.tool_states.push((call, tool_state));
        self.transitions
            .push((AgentState::ExecutingTool { call }, to));
        self.state = Some(to);
        Ok(())
    }

    fn running_tools(&self) -> Result<Vec<ToolCallId>, JournalError> {
        // The latest recorded state per call wins; only calls whose latest
        // state is `Running` count.
        let mut latest: HashMap<ToolCallId, ToolCallState> = HashMap::new();
        for (call, state) in &self.tool_states {
            latest.insert(*call, *state);
        }
        Ok(latest
            .into_iter()
            .filter(|(_, s)| matches!(s, ToolCallState::Running))
            .map(|(c, _)| c)
            .collect())
    }
}
