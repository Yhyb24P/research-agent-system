//! Narrow stdio MCP bridge used only by the Phase 2.3 Codex live harness.
//! It persists bounded collaboration requests through the existing team board.

use std::io::{self, BufRead, Write};

use agent_code_storage::{RuntimeCollaborationRecord, SqliteTaskBoard};
use serde_json::{json, Value};

fn main() {
    let db = std::env::var("RAS_DB").expect("RAS_DB required");
    let task = std::env::var("RAS_TASK_ID")
        .expect("RAS_TASK_ID")
        .parse()
        .expect("task id");
    let attempt = std::env::var("RAS_ATTEMPT")
        .expect("RAS_ATTEMPT")
        .parse()
        .expect("attempt");
    let board =
        SqliteTaskBoard::open(rusqlite::Connection::open(db).expect("open board")).expect("board");
    audit("bridge-started");
    for line in io::stdin().lock().lines() {
        let Ok(line) = line else { break };
        let Ok(msg) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        let id = msg.get("id").cloned();
        let method = msg
            .get("method")
            .and_then(Value::as_str)
            .unwrap_or_default();
        audit(&format!("rpc-{method}"));
        let result = match method {
            "initialize" => {
                json!({"protocolVersion":"2025-03-26","capabilities":{"tools":{},"resources":{},"prompts":{}},"serverInfo":{"name":"ras-codex-bridge","version":"0.1"}})
            }
            "tools/list" => json!({"tools":[
                {"name":"ras_request_context","description":"Request bounded ACC/team context","inputSchema":{"type":"object","properties":{"purpose":{"type":"string"}},"required":["purpose"]}},
                {"name":"ras_request_help","description":"Request bounded team help","inputSchema":{"type":"object","properties":{"question":{"type":"string"}},"required":["question"]}}
            ]}),
            "resources/list" => json!({"resources":[]}),
            "resources/templates/list" => json!({"resourceTemplates":[]}),
            "prompts/list" => json!({"prompts":[]}),
            "tools/call" => {
                audit("tools-call-received");
                let p = msg.get("params").unwrap_or(&Value::Null);
                let name = p.get("name").and_then(Value::as_str).unwrap_or_default();
                // MCP JSON-RPC request ids are supplied by the live client and
                // are the only native-call identity accepted by this bridge.
                // Tool arguments are deliberately not trusted for identity.
                let call_id = id
                    .as_ref()
                    .map(Value::to_string)
                    .unwrap_or_else(|| format!("unidentified-{name}"));
                let summary = p
                    .get("arguments")
                    .and_then(|a| a.get("purpose").or_else(|| a.get("question")))
                    .and_then(Value::as_str)
                    .unwrap_or("bounded request");
                let allowed = matches!(name, "ras_request_context" | "ras_request_help");
                let persisted = if allowed {
                    board.record_runtime_collaboration(&RuntimeCollaborationRecord {
                        team_task_id: task,
                        attempt,
                        runtime_kind: "codex-app-server".into(),
                        native_call_id: call_id,
                        kind: name.into(),
                        payload_summary: summary.chars().take(512).collect(),
                        response_summary: Some("team bridge acknowledged bounded request".into()),
                    })
                } else {
                    Ok(false)
                };
                match persisted {
                    Ok(inserted) if allowed => {
                        audit(if inserted {
                            "collaboration-persisted"
                        } else {
                            "collaboration-duplicate"
                        });
                        json!({"content":[{"type":"text","text":if inserted { "bounded team response persisted" } else { "duplicate bounded request" }}],"isError":false})
                    }
                    Ok(_) => {
                        json!({"content":[{"type":"text","text":"unsupported tool"}],"isError":true})
                    }
                    Err(error) => {
                        audit("collaboration-persistence-error");
                        json!({"content":[{"type":"text","text":format!("bridge persistence failed: {error}")}],"isError":true})
                    }
                }
            }
            _ => json!({}),
        };
        if let Some(id) = id {
            let _ = writeln!(
                io::stdout(),
                "{}",
                json!({"jsonrpc":"2.0","id":id,"result":result})
            );
            let _ = io::stdout().flush();
        }
    }
}

/// Optional operational breadcrumbs. They never include tool arguments,
/// model text, credentials, prompts, or protocol bodies.
fn audit(event: &str) {
    let Ok(path) = std::env::var("RAS_BRIDGE_LOG") else {
        return;
    };
    let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    else {
        return;
    };
    let _ = writeln!(file, "{event}");
}
