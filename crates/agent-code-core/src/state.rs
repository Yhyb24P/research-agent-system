/// Identifies a persisted Agent session.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct SessionId(String);

impl SessionId {
    /// Wrap an identifier.
    pub fn new(id: impl Into<String>) -> Self {
        Self(id.into())
    }

    /// The identifier as a string slice.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// Lifecycle states of a native Coding Agent session.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentState {
    Initializing,
    Observing,
    WaitingModel,
    ExecutingTool { call: ToolCallId },
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

    /// The stable string name of the variant (payload ignored).
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Initializing => "Initializing",
            Self::Observing => "Observing",
            Self::WaitingModel => "WaitingModel",
            Self::ExecutingTool { .. } => "ExecutingTool",
            Self::Verifying => "Verifying",
            Self::Delivering => "Delivering",
            Self::Completed => "Completed",
            Self::Failed => "Failed",
            Self::RolledBack => "RolledBack",
        }
    }

    /// The active tool call, if the session is executing one.
    pub fn active_call(self) -> Option<ToolCallId> {
        match self {
            Self::ExecutingTool { call } => Some(call),
            _ => None,
        }
    }

    /// Reconstruct a state from its persisted variant name and active call.
    pub fn restore(variant: &str, active_call: Option<ToolCallId>) -> Option<Self> {
        Some(match variant {
            "Initializing" => Self::Initializing,
            "Observing" => Self::Observing,
            "WaitingModel" => Self::WaitingModel,
            "ExecutingTool" => Self::ExecutingTool { call: active_call? },
            "Verifying" => Self::Verifying,
            "Delivering" => Self::Delivering,
            "Completed" => Self::Completed,
            "Failed" => Self::Failed,
            "RolledBack" => Self::RolledBack,
            _ => return None,
        })
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

impl ToolCallState {
    /// The stable string name of the variant.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Requested => "Requested",
            Self::Running => "Running",
            Self::Succeeded => "Succeeded",
            Self::Failed => "Failed",
            Self::Interrupted => "Interrupted",
        }
    }
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
