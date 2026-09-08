use std::fmt;

/// Errors raised while driving the Agent state machine or its journal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JournalError {
    /// The requested state transition is not legal from the current state.
    IllegalTransition,
    /// A tool-call operation referenced a call that is not active.
    UnknownToolCall,
    /// The underlying journal store failed.
    Storage(String),
}

impl fmt::Display for JournalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::IllegalTransition => write!(f, "illegal state transition"),
            Self::UnknownToolCall => write!(f, "unknown or inactive tool call"),
            Self::Storage(msg) => write!(f, "journal storage error: {msg}"),
        }
    }
}

impl std::error::Error for JournalError {}
