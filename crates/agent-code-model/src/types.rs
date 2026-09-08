/// A request to the model: the compact context the Agent wants a decision on.
#[derive(Debug, Clone, Default)]
pub struct ModelContext {
    /// The current task objective.
    pub task: String,
    /// Compact observations gathered so far.
    pub observations: Vec<String>,
}

/// The model's decision: either a final answer or a tool call to run next.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModelDecision {
    /// The task is complete; the inner value is the final summary.
    Final(String),
    /// Run a tool next; the inner value is an opaque request the caller assigns.
    ToolCall(String),
}

/// Errors returned by a [`ModelClient`](crate::ModelClient).
#[derive(Debug)]
pub enum ModelError {
    /// The model endpoint could not be reached.
    Transport(String),
    /// The model returned a response the Agent cannot use.
    BadResponse(String),
}

impl std::fmt::Display for ModelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Transport(msg) => write!(f, "model transport error: {msg}"),
            Self::BadResponse(msg) => write!(f, "model returned an unusable response: {msg}"),
        }
    }
}

impl std::error::Error for ModelError {}
