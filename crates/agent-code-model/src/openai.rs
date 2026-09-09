//! An OpenAI-compatible chat-completions client that speaks the native tool
//! protocol: it sends the bounded context plus the five tool schemas, and
//! parses either a tool call (into a typed [`ToolRequest`]) or a final answer.

use std::time::Duration;

use agent_code_tools::ToolRequest;
use async_trait::async_trait;
use serde_json::Value;

use crate::client::ModelClient;
use crate::types::{ModelContext, ModelDecision, ModelError};

/// A client for an OpenAI-compatible `/chat/completions` endpoint.
#[derive(Debug, Clone)]
pub struct OpenAiClient {
    endpoint: String,
    model: String,
    api_key: Option<String>,
    timeout: Duration,
}

impl OpenAiClient {
    /// Build a client. `endpoint` is the base URL (e.g. `http://host/v1`); the
    /// client posts to `{endpoint}/chat/completions`. `api_key` is sent only as
    /// an `Authorization` header and never appears in any error text.
    pub fn new(
        endpoint: impl Into<String>,
        model: impl Into<String>,
        api_key: Option<String>,
        timeout: Duration,
    ) -> Self {
        Self {
            endpoint: endpoint.into(),
            model: model.into(),
            api_key,
            timeout,
        }
    }
}

#[async_trait]
impl ModelClient for OpenAiClient {
    async fn decide(&self, ctx: &ModelContext) -> Result<ModelDecision, ModelError> {
        // ureq is blocking; keep the async executor free by running it on a
        // blocking thread.
        let me = self.clone();
        let ctx = ctx.clone();
        tokio::task::spawn_blocking(move || me.request(&ctx))
            .await
            .map_err(|e| ModelError::Transport(format!("model worker failed: {e}")))?
    }
}

impl OpenAiClient {
    /// The blocking request/parse, run off the async executor.
    fn request(&self, ctx: &ModelContext) -> Result<ModelDecision, ModelError> {
        let url = format!("{}/chat/completions", self.endpoint.trim_end_matches('/'));
        let body = serde_json::json!({
            "model": self.model,
            "messages": [{ "role": "user", "content": render_context(ctx) }],
            "tools": tool_schemas(),
            "tool_choice": "auto",
        });

        let mut req = ureq::post(&url)
            .timeout(self.timeout)
            .set("Content-Type", "application/json");
        if let Some(key) = &self.api_key {
            req = req.set("Authorization", &format!("Bearer {key}"));
        }
        let body_str = body.to_string();
        let resp = req.send_string(&body_str).map_err(map_ureq_error)?;
        let text: String = resp
            .into_string()
            .map_err(|e| ModelError::Transport(e.to_string()))?;
        parse_response(&text)
    }
}

/// Map a transport/HTTP error to [`ModelError`], never echoing the API key.
/// The key travels only in the request header, so the URL- and status-based
/// messages below cannot contain it.
fn map_ureq_error(e: ureq::Error) -> ModelError {
    match e {
        ureq::Error::Status(code, _) => {
            ModelError::Transport(format!("model endpoint returned HTTP {code}"))
        }
        ureq::Error::Transport(t) => {
            ModelError::Transport(format!("model endpoint unreachable: {t}"))
        }
    }
}

/// Turn a chat-completions response body into a decision.
fn parse_response(text: &str) -> Result<ModelDecision, ModelError> {
    let parsed: Value = serde_json::from_str(text)
        .map_err(|e| ModelError::BadResponse(format!("malformed model response: {e}")))?;
    let message = parsed
        .get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("message"))
        .ok_or_else(|| ModelError::BadResponse("response has no choices[0].message".into()))?;

    if let Some(calls) = message.get("tool_calls").and_then(Value::as_array) {
        if let Some(first) = calls.first() {
            let name = &first["function"]["name"];
            let args = &first["function"]["arguments"];
            return parse_tool_call(name, args);
        }
    }
    if let Some(content) = message.get("content").and_then(Value::as_str) {
        if !content.trim().is_empty() {
            return Ok(ModelDecision::Final(content.to_string()));
        }
    }
    Err(ModelError::BadResponse(
        "model returned neither a tool call nor a final answer".into(),
    ))
}

/// Parse one tool call's name + arguments into a typed request.
fn parse_tool_call(name: &Value, args: &Value) -> Result<ModelDecision, ModelError> {
    let name = name
        .as_str()
        .ok_or_else(|| ModelError::BadResponse("tool call missing a name".into()))?;
    // OpenAI sends `arguments` as a JSON-encoded string; tolerate an object too.
    let args_json = match args {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    };
    let req = match name {
        "view_file" => ToolRequest::ViewFile(deserialize_args(&args_json)?),
        "edit_file" => ToolRequest::EditFile(deserialize_args(&args_json)?),
        "write_file" => ToolRequest::WriteFile(deserialize_args(&args_json)?),
        "search_dir" => ToolRequest::SearchDir(deserialize_args(&args_json)?),
        "execute_command" => ToolRequest::ExecuteCommand(deserialize_args(&args_json)?),
        other => return Err(ModelError::BadResponse(format!("unknown tool: {other}"))),
    };
    Ok(ModelDecision::ToolCall(req))
}

fn deserialize_args<T: serde::de::DeserializeOwned>(s: &str) -> Result<T, ModelError> {
    serde_json::from_str(s).map_err(|e| ModelError::BadResponse(format!("bad tool arguments: {e}")))
}

/// Render the bounded context as the user message for the model.
fn render_context(ctx: &ModelContext) -> String {
    let mut s = String::new();
    s.push_str(&format!("[task]\n{}\n", ctx.task));
    if !ctx.project_rules.is_empty() {
        s.push_str(&format!("[rules]\n{}\n", ctx.project_rules));
    }
    if !ctx.repository_map.is_empty() {
        s.push_str(&format!("[repo-map]\n{}\n", ctx.repository_map));
    }
    if !ctx.compact_summary.is_empty() {
        s.push_str(&format!("[compacted]\n{}\n", ctx.compact_summary));
    }
    if !ctx.observations.is_empty() {
        s.push_str("[observations]\n");
        for line in &ctx.observations {
            s.push_str(line);
            s.push('\n');
        }
    }
    s
}

/// The five tool schemas, as the OpenAI `tools` array.
fn tool_schemas() -> Value {
    serde_json::json!([
        { "type": "function", "function": { "name": "view_file",
            "parameters": { "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"} } } } },
        { "type": "function", "function": { "name": "edit_file",
            "parameters": { "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                    "expected_file_hash": {"type": "string"} } } } },
        { "type": "function", "function": { "name": "write_file",
            "parameters": { "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create_only": {"type": "boolean"},
                    "max_bytes": {"type": "integer"},
                    "expected_file_hash": {"type": "string"} } } } },
        { "type": "function", "function": { "name": "search_dir",
            "parameters": { "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                    "max_matches": {"type": "integer"} } } } },
        { "type": "function", "function": { "name": "execute_command",
            "parameters": { "type": "object",
                "properties": {
                    "program": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                    "env": {"type": "object"} } } } },
    ])
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::time::Duration;

    use super::*;
    use agent_code_tools::ViewFile;

    /// A minimal std-only HTTP/1.1 mock: serves each of `bodies` in order with
    /// the given status line, one per accepted connection, then exits. Returns
    /// the base URL and the serving thread.
    fn mock_http(status: &str, bodies: &[&str]) -> (String, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind mock");
        let port = listener.local_addr().expect("addr").port();
        let status = status.to_string();
        let bodies: Vec<String> = bodies.iter().map(|s| s.to_string()).collect();
        let handle = std::thread::spawn(move || {
            for body in bodies {
                let (mut stream, _) = match listener.accept() {
                    Ok(v) => v,
                    Err(_) => break,
                };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
                let mut buf = [0u8; 8192];
                let _ = stream.read(&mut buf); // consume the request (ignored)
                let resp = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = stream.write_all(resp.as_bytes());
                let _ = stream.flush();
            }
        });
        (format!("http://127.0.0.1:{port}"), handle)
    }

    fn client(url: &str, key: Option<&str>) -> OpenAiClient {
        OpenAiClient::new(
            url,
            "test-model",
            key.map(|s| s.to_string()),
            Duration::from_secs(5),
        )
    }

    #[tokio::test]
    async fn parses_a_tool_call() {
        let body = r#"{"choices":[{"message":{"role":"assistant","content":null,"tool_calls":[
            {"type":"function","function":{"name":"view_file",
             "arguments":"{\"path\":\"a.rs\",\"start_line\":1,\"end_line\":10}"}}]}}]}"#;
        let (url, h) = mock_http("200 OK", &[body]);
        let d = client(&url, None)
            .decide(&ModelContext::default())
            .await
            .unwrap();
        assert_eq!(
            d,
            ModelDecision::ToolCall(ToolRequest::ViewFile(ViewFile {
                path: "a.rs".into(),
                start_line: 1,
                end_line: 10,
            }))
        );
        h.join().unwrap();
    }

    #[tokio::test]
    async fn parses_a_final_answer() {
        let body = r#"{"choices":[{"message":{"role":"assistant","content":"all done"}}]}"#;
        let (url, h) = mock_http("200 OK", &[body]);
        let d = client(&url, None)
            .decide(&ModelContext::default())
            .await
            .unwrap();
        assert_eq!(d, ModelDecision::Final("all done".into()));
        h.join().unwrap();
    }

    #[tokio::test]
    async fn unknown_tool_is_an_error() {
        let body = r#"{"choices":[{"message":{"tool_calls":[
            {"function":{"name":"drop_database","arguments":"{}"}}]}}]}"#;
        let (url, h) = mock_http("200 OK", &[body]);
        let err = client(&url, None)
            .decide(&ModelContext::default())
            .await
            .unwrap_err();
        assert!(matches!(err, ModelError::BadResponse(_)));
        h.join().unwrap();
    }

    #[tokio::test]
    async fn malformed_json_is_an_error() {
        let (url, h) = mock_http("200 OK", &["this is not json"]);
        let err = client(&url, None)
            .decide(&ModelContext::default())
            .await
            .unwrap_err();
        assert!(matches!(err, ModelError::BadResponse(_)));
        h.join().unwrap();
    }

    #[tokio::test]
    async fn http_error_is_a_transport_error() {
        let (url, h) = mock_http("500 Internal Server Error", &["oops"]);
        let err = client(&url, None)
            .decide(&ModelContext::default())
            .await
            .unwrap_err();
        assert!(matches!(err, ModelError::Transport(_)));
        h.join().unwrap();
    }

    #[tokio::test]
    async fn errors_do_not_leak_the_api_key() {
        const KEY: &str = "SECRETKEY_DO_NOT_LEAK";
        let (url, h) = mock_http("500 Internal Server Error", &["oops"]);
        let err = client(&url, Some(KEY))
            .decide(&ModelContext::default())
            .await
            .unwrap_err();
        assert!(!err.to_string().contains(KEY));
        h.join().unwrap();
    }
}
