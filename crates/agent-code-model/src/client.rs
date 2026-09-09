use async_trait::async_trait;

use crate::types::{ModelContext, ModelDecision, ModelError};

/// A model-backed decision source. The native Agent calls this to get its next
/// move. Implementations may be a local OpenAI-compatible endpoint or a
/// frontier API.
#[async_trait]
pub trait ModelClient: Send {
    /// Ask the model for the next decision given the compact context.
    async fn decide(&self, ctx: &ModelContext) -> Result<ModelDecision, ModelError>;

    /// The fixed, context-independent part of this client's outbound request
    /// (tool schemas + message framing), as the exact text that is sent. A
    /// caller bills it against the context budget so the full request — not
    /// just the context — fits the configured input budget (N11).
    fn protocol_overhead(&self) -> String {
        String::new()
    }
}

/// A deterministic stub that always returns the same decision. Used by tests
/// and as a placeholder before the real OpenAI-compatible client lands.
pub struct StubModelClient {
    decision: ModelDecision,
}

impl StubModelClient {
    /// A stub that always returns `decision`.
    pub fn new(decision: ModelDecision) -> Self {
        Self { decision }
    }
}

#[async_trait]
impl ModelClient for StubModelClient {
    async fn decide(&self, _ctx: &ModelContext) -> Result<ModelDecision, ModelError> {
        Ok(self.decision.clone())
    }
}
