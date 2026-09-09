//! Small async model-client abstraction for the native Coding Agent.

mod client;
mod openai;
mod types;

pub use client::{ModelClient, StubModelClient};
pub use openai::{openai_request_body, OpenAiClient};
pub use types::{ModelContext, ModelDecision, ModelError, Observation};

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn stub_returns_configured_decision() {
        use agent_code_tools::ToolRequest;
        let req = ToolRequest::ViewFile(agent_code_tools::ViewFile {
            path: "a.rs".into(),
            start_line: 1,
            end_line: 10,
        });
        let client = StubModelClient::new(ModelDecision::ToolCall(req.clone()));
        let decision = client.decide(&ModelContext::default()).await.unwrap();
        assert_eq!(decision, ModelDecision::ToolCall(req));
    }
}
