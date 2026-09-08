//! Context budgeting, truncation and compaction.
//!
//! R1 provides the budget shape only; selection and compaction land in R3.

/// A token budget for the model-facing context.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ContextBudget {
    pub max_tokens: u32,
    /// Tokens always reserved for the next model output / tool call.
    pub reserved_output: u32,
}
