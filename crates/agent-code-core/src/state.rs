/// Lifecycle states of a native Coding Agent session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentState {
    Initializing,
    Observing,
    WaitingModel,
    ExecutingTool,
    Verifying,
    Delivering,
    Completed,
    Failed,
    RolledBack,
}

impl AgentState {
    /// True when no further transitions are possible.
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Completed | Self::Failed | Self::RolledBack)
    }
}

/// Lifecycle states of a single tool call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolCallState {
    Requested,
    Running,
    Succeeded,
    Failed,
    Interrupted,
}

/// Identifier for a tool call within a session.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ToolCallId(u64);

impl ToolCallId {
    /// Wrap a numeric id.
    pub fn new(id: u64) -> Self {
        Self(id)
    }

    /// The numeric id.
    pub fn as_u64(self) -> u64 {
        self.0
    }
}
