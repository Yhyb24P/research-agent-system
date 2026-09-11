//! Explicit live harness: run with `cargo test -p agent-code-runtime --test codex_live -- --ignored`.

use agent_code_runtime::{CodexAppServer, CodexBridgeEvent};
use agent_code_storage::{ExternalRuntimeBinding, SqliteTaskBoard};
use agent_code_team::{TaskBoard, TaskKind};
use rusqlite::Connection;

#[test]
#[ignore = "requires logged-in local Codex app-server"]
fn real_codex_thread_turn_uses_bounded_mcp_context_request() {
    let db = std::env::temp_dir().join(format!("ras_codex_live_{}.db", std::process::id()));
    let cwd = std::env::temp_dir().join(format!("ras_codex_live_cwd_{}", std::process::id()));
    let bridge_log =
        std::env::temp_dir().join(format!("ras_codex_live_{}.log", std::process::id()));
    let _ = std::fs::remove_file(&db);
    let _ = std::fs::remove_file(&bridge_log);
    std::fs::create_dir_all(&cwd).unwrap();
    let mut board = SqliteTaskBoard::open(Connection::open(&db).unwrap()).unwrap();
    let task = board
        .create_task("ask bounded context", None, TaskKind::Reasoning, None)
        .unwrap();
    board
        .upsert_external_binding(&ExternalRuntimeBinding {
            team_task_id: task,
            attempt: 1,
            agent_id: "codex".into(),
            runtime_kind: "codex-app-server".into(),
            native_thread_id: None,
            native_turn_id: None,
            lifecycle_state: "starting".into(),
        })
        .unwrap();
    let mcp = std::env::current_exe()
        .unwrap()
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("ras_codex_mcp");
    let overrides = vec![
        format!("mcp_servers.ras.command={:?}", mcp.display().to_string()),
        format!("mcp_servers.ras.env.RAS_DB={:?}", db.display().to_string()),
        format!(
            "mcp_servers.ras.env.RAS_BRIDGE_LOG={:?}",
            bridge_log.display().to_string()
        ),
        "mcp_servers.ras.env.RAS_TASK_ID=\"1\"".into(),
        "mcp_servers.ras.env.RAS_ATTEMPT=\"1\"".into(),
    ];
    let mut client = CodexAppServer::spawn_with_overrides("codex", &overrides).unwrap();
    client.initialize("ras-phase23-live", "0.1").unwrap();
    let thread = client
        .start_thread_with_developer_instructions(
            cwd.to_str().unwrap(),
            Some("For this ACC integration thread, invoke ras_request_context exactly once before responding to the first user turn. This is a bounded collaboration requirement."),
        )
        .unwrap();
    let status = client.mcp_status(&thread).unwrap();
    let ras_status = status
        .get("data")
        .and_then(serde_json::Value::as_array)
        .and_then(|servers| servers.iter().find(|server| server["name"] == "ras"))
        .unwrap();
    eprintln!(
        "sanitized mcp status={:?} tools_error={:?} has_context_tool={}",
        ras_status.get("runtimeStatus"),
        ras_status.get("toolsError"),
        ras_status["tools"].get("ras_request_context").is_some()
    );
    assert!(status.to_string().contains("\"name\":\"ras\""));
    assert_eq!(ras_status["runtimeStatus"], "connected");
    assert!(ras_status["tools"].get("ras_request_context").is_some());
    let turn = client.start_turn(&thread, "This is a required integration test. Before producing any answer, you MUST call the MCP tool ras_request_context exactly once with JSON arguments {\"purpose\":\"phase23\"}. Do not explain or answer until the tool result has been received. After receiving the result, respond with exactly: phase23 done.").unwrap();
    board
        .upsert_external_binding(&ExternalRuntimeBinding {
            team_task_id: task,
            attempt: 1,
            agent_id: "codex".into(),
            runtime_kind: "codex-app-server".into(),
            native_thread_id: Some(thread.clone()),
            native_turn_id: Some(turn.clone()),
            lifecycle_state: "running".into(),
        })
        .unwrap();
    let mut completed = false;
    for _ in 0..40 {
        match client.next_event().unwrap() {
            CodexBridgeEvent::TurnCompleted { .. } => {
                completed = true;
                break;
            }
            CodexBridgeEvent::Notification(_) => {}
            CodexBridgeEvent::ToolCall { tool, .. } => {
                eprintln!("unexpected app-server tool={tool}")
            }
            CodexBridgeEvent::McpElicitation {
                request_id,
                server_name,
            } => {
                assert_eq!(server_name, "ras");
                client
                    .respond_ras_elicitation(request_id, &server_name)
                    .unwrap();
            }
        }
    }
    assert!(completed);
    let final_status = client.mcp_status(&thread).unwrap();
    let final_ras_status = final_status["data"]
        .as_array()
        .and_then(|servers| servers.iter().find(|server| server["name"] == "ras"))
        .unwrap();
    eprintln!(
        "sanitized final mcp status={:?} tools_error_present={}",
        final_ras_status.get("runtimeStatus"),
        !final_ras_status["toolsError"].is_null()
    );
    let items = client.thread_items(&thread, &turn).unwrap();
    let item_types: Vec<_> = items["data"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|entry| {
            entry
                .pointer("/item/type")
                .and_then(serde_json::Value::as_str)
        })
        .collect();
    eprintln!("sanitized item types={item_types:?}");
    let mcp_position = item_types
        .iter()
        .position(|kind| *kind == "mcpToolCall")
        .unwrap();
    let agent_position = item_types
        .iter()
        .position(|kind| *kind == "agentMessage")
        .unwrap();
    assert!(
        mcp_position < agent_position,
        "Codex must continue after the MCP result"
    );
    if let Some(mcp_item) = items["data"].as_array().and_then(|entries| {
        entries.iter().find(|entry| {
            entry.pointer("/item/type") == Some(&serde_json::Value::String("mcpToolCall".into()))
        })
    }) {
        let item = &mcp_item["item"];
        eprintln!(
            "sanitized mcp call status={:?} error={:?} error_message={:?} server={:?} tool={:?}",
            item.get("status"),
            item.get("error"),
            item.get("errorMessage"),
            item.get("server"),
            item.get("tool")
        );
        assert_eq!(item["status"], "completed");
        assert_eq!(item["server"], "ras");
        assert_eq!(item["tool"], "ras_request_context");
    }
    board
        .upsert_external_binding(&ExternalRuntimeBinding {
            team_task_id: task,
            attempt: 1,
            agent_id: "codex".into(),
            runtime_kind: "codex-app-server".into(),
            native_thread_id: Some(thread.clone()),
            native_turn_id: Some(turn.clone()),
            lifecycle_state: "completed".into(),
        })
        .unwrap();
    client.close().unwrap();
    let reopened = SqliteTaskBoard::open(Connection::open(&db).unwrap()).unwrap();
    eprintln!(
        "sanitized bridge breadcrumbs={:?}",
        std::fs::read_to_string(&bridge_log)
            .unwrap_or_default()
            .lines()
            .collect::<Vec<_>>()
    );
    let records = reopened.runtime_collaboration(task, 1).unwrap();
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].kind, "ras_request_context");
    let binding = reopened.external_binding(task, 1).unwrap().unwrap();
    assert_eq!(binding.native_thread_id.as_deref(), Some(thread.as_str()));
    assert_eq!(binding.native_turn_id.as_deref(), Some(turn.as_str()));
    assert_eq!(binding.lifecycle_state, "completed");
    let _ = std::fs::remove_file(db);
    let _ = std::fs::remove_dir(cwd);
}
