//! Small async model-client abstraction for the native Coding Agent.

mod client;
mod types;

pub use client::{ModelClient, StubModelClient};
pub use types::{ModelContext, ModelDecision, ModelError, Observation};

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn stub_returns_configured_decision() {
        let client = StubModelClient::new(ModelDecision::ToolCall("view_file".into()));
        let decision = client.decide(&ModelContext::default()).await.unwrap();
        assert_eq!(decision, ModelDecision::ToolCall("view_file".into()));
    }
}
