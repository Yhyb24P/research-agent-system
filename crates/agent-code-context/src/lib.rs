//! Context budgeting, truncation and compaction.
//!
//! This crate turns a durable, unbounded observation history into a bounded,
//! model-facing [`agent_code_model::ModelContext`]. It owns the token budget,
//! section selection and compaction; it never mutates the durable history.

mod budget;
mod build;

pub use budget::{truncate_chars, BytesTokenCounter, ContextBudget, ContextError, TokenCounter};
pub use build::{build_context, serialize_context, ContextSpec};
