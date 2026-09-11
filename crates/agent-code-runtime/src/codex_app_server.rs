//! Minimal stdio JSON-RPC transport for the locally probed Codex app-server.
//! Native IDs stay external; callers persist them through the team board.

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CodexBridgeEvent {
    Notification(String),
    ToolCall {
        request_id: Value,
        call_id: String,
        tool: String,
    },
    TurnCompleted {
        thread_id: String,
        turn_id: String,
    },
    McpElicitation {
        request_id: Value,
        server_name: String,
    },
}

#[derive(Debug)]
pub enum CodexBridgeError {
    Io(String),
    Protocol(String),
    Closed,
}

impl std::fmt::Display for CodexBridgeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}

impl std::error::Error for CodexBridgeError {}

/// A bounded client for `codex app-server --stdio`. It deliberately only
/// understands transport facts and the two allowlisted collaboration tools.
pub struct CodexAppServer {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    next_id: u64,
}

impl CodexAppServer {
    pub fn spawn(executable: &str) -> Result<Self, CodexBridgeError> {
        Self::spawn_with_overrides(executable, &[])
    }

    pub fn spawn_with_overrides(
        executable: &str,
        overrides: &[String],
    ) -> Result<Self, CodexBridgeError> {
        let mut command = Command::new(executable);
        command.args(["app-server", "--stdio"]);
        for override_value in overrides {
            command.args(["-c", override_value]);
        }
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| CodexBridgeError::Io(e.to_string()))?;
        Ok(Self {
            stdin: child.stdin.take().ok_or(CodexBridgeError::Closed)?,
            stdout: BufReader::new(child.stdout.take().ok_or(CodexBridgeError::Closed)?),
            child,
            next_id: 1,
        })
    }

    pub fn initialize(&mut self, name: &str, version: &str) -> Result<Value, CodexBridgeError> {
        let result = self.request("initialize", json!({"clientInfo":{"name":name,"version":version},"capabilities":{"experimentalApi":true}}))?;
        self.notify("initialized", json!({}))?;
        Ok(result)
    }

    pub fn start_thread(&mut self, cwd: &str) -> Result<String, CodexBridgeError> {
        self.start_thread_with_developer_instructions(cwd, None)
    }

    pub fn start_thread_with_developer_instructions(
        &mut self,
        cwd: &str,
        developer_instructions: Option<&str>,
    ) -> Result<String, CodexBridgeError> {
        let result = self.request(
            "thread/start",
            json!({
                "cwd":cwd,
                "sandbox":"workspace-write",
                "approvalPolicy":"on-request",
                "developerInstructions":developer_instructions,
            }),
        )?;
        result
            .pointer("/thread/id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| {
                CodexBridgeError::Protocol("thread/start response missing thread.id".into())
            })
    }

    pub fn start_turn(&mut self, thread_id: &str, text: &str) -> Result<String, CodexBridgeError> {
        let result = self.request(
            "turn/start",
            json!({"threadId":thread_id,"input":[{"type":"text","text":text}]}),
        )?;
        result
            .pointer("/turn/id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| CodexBridgeError::Protocol("turn/start response missing turn.id".into()))
    }

    pub fn mcp_status(&mut self, thread_id: &str) -> Result<Value, CodexBridgeError> {
        self.request(
            "mcpServerStatus/list",
            json!({"threadId":thread_id,"detail":"full"}),
        )
    }

    /// Returns protocol items for audit classification only. Callers must not
    /// persist raw item content into ACC state.
    pub fn thread_items(
        &mut self,
        thread_id: &str,
        turn_id: &str,
    ) -> Result<Value, CodexBridgeError> {
        self.request(
            "thread/items/list",
            json!({"threadId":thread_id,"turnId":turn_id,"limit":100}),
        )
    }

    pub fn next_event(&mut self) -> Result<CodexBridgeEvent, CodexBridgeError> {
        let value = self.read_value()?;
        if let Some(method) = value.get("method").and_then(Value::as_str) {
            if method == "mcpServer/elicitation/request" {
                let p = value.get("params").ok_or_else(|| {
                    CodexBridgeError::Protocol("elicitation missing params".into())
                })?;
                return Ok(CodexBridgeEvent::McpElicitation {
                    request_id: value.get("id").cloned().ok_or_else(|| {
                        CodexBridgeError::Protocol("elicitation missing request id".into())
                    })?,
                    server_name: p
                        .get("serverName")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned(),
                });
            }
            if method == "item/tool/call" {
                let p = value
                    .get("params")
                    .ok_or_else(|| CodexBridgeError::Protocol("tool call missing params".into()))?;
                let call_id = p
                    .get("callId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| CodexBridgeError::Protocol("tool call missing callId".into()))?
                    .to_owned();
                let tool = p
                    .get("tool")
                    .and_then(Value::as_str)
                    .ok_or_else(|| CodexBridgeError::Protocol("tool call missing tool".into()))?
                    .to_owned();
                return Ok(CodexBridgeEvent::ToolCall {
                    request_id: value.get("id").cloned().ok_or_else(|| {
                        CodexBridgeError::Protocol("tool call missing request id".into())
                    })?,
                    call_id,
                    tool,
                });
            }
            if method == "turn/completed" {
                let p = value
                    .get("params")
                    .ok_or_else(|| CodexBridgeError::Protocol("completed missing params".into()))?;
                return Ok(CodexBridgeEvent::TurnCompleted {
                    thread_id: p
                        .get("threadId")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned(),
                    turn_id: p
                        .get("turnId")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_owned(),
                });
            }
            return Ok(CodexBridgeEvent::Notification(method.to_owned()));
        }
        Err(CodexBridgeError::Protocol(
            "unexpected response while awaiting event".into(),
        ))
    }

    pub fn respond_tool(
        &mut self,
        request_id: Value,
        success: bool,
        text: &str,
    ) -> Result<(), CodexBridgeError> {
        self.write_value(&json!({"jsonrpc":"2.0","id":request_id,"result":{"success":success,"contentItems":[{"type":"inputText","text":text}]}}))
    }

    /// Responds only to the configured, bounded RAS MCP bridge. This is not
    /// a shell, admin, or general approval path.
    pub fn respond_ras_elicitation(
        &mut self,
        request_id: Value,
        server_name: &str,
    ) -> Result<(), CodexBridgeError> {
        if server_name != "ras" {
            return Err(CodexBridgeError::Protocol(
                "refusing elicitation for a non-RAS MCP server".into(),
            ));
        }
        self.write_value(&json!({"jsonrpc":"2.0","id":request_id,"result":{"action":"accept"}}))
    }

    pub fn interrupt(&mut self, thread_id: &str, turn_id: &str) -> Result<(), CodexBridgeError> {
        self.request(
            "turn/interrupt",
            json!({"threadId":thread_id,"turnId":turn_id}),
        )?;
        Ok(())
    }

    pub fn close(mut self) -> Result<(), CodexBridgeError> {
        let _ = self.child.kill();
        self.child
            .wait()
            .map_err(|e| CodexBridgeError::Io(e.to_string()))?;
        Ok(())
    }

    fn request(&mut self, method: &str, params: Value) -> Result<Value, CodexBridgeError> {
        let id = self.next_id;
        self.next_id += 1;
        self.write_value(&json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}))?;
        loop {
            let value = self.read_value()?;
            if value.get("id") == Some(&json!(id)) {
                return value.get("result").cloned().ok_or_else(|| {
                    CodexBridgeError::Protocol(format!("{method} returned no result"))
                });
            }
            // Notifications before a correlated response are expected and are
            // deliberately dropped here; they contain no durable payload.
        }
    }

    fn notify(&mut self, method: &str, params: Value) -> Result<(), CodexBridgeError> {
        self.write_value(&json!({"jsonrpc":"2.0","method":method,"params":params}))
    }
    fn write_value(&mut self, value: &Value) -> Result<(), CodexBridgeError> {
        writeln!(self.stdin, "{value}")
            .and_then(|_| self.stdin.flush())
            .map_err(|e| CodexBridgeError::Io(e.to_string()))
    }
    fn read_value(&mut self) -> Result<Value, CodexBridgeError> {
        let mut line = String::new();
        if self
            .stdout
            .read_line(&mut line)
            .map_err(|e| CodexBridgeError::Io(e.to_string()))?
            == 0
        {
            return Err(CodexBridgeError::Closed);
        }
        serde_json::from_str(&line)
            .map_err(|_| CodexBridgeError::Protocol("malformed JSON-RPC message".into()))
    }
}
