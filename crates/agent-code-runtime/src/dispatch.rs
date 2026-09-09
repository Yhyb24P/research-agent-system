//! Dispatch a typed tool request to the contained workspace and shape the
//! bounded result into a model-readable outcome plus durable observations.
//!
//! The observations carry the *content* the model needs to act on — file
//! lines, search hits, command output — not just a count. Hashes, the
//! `truncated` flag, and the log-file references are preserved so the model
//! can tell a bounded result apart from a complete one.

use agent_code_model::Observation;
use agent_code_tools::ToolRequest;
use agent_code_workspace::{CommandOutput, Workspace};

/// The bounded outcome of dispatching one tool request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolOutcome {
    /// Whether the tool succeeded (a non-zero command exit is a failure).
    pub ok: bool,
    /// For `execute_command`: the exit code, or `None` when killed/timeout.
    pub exit: Option<i32>,
    /// The bounded, model-readable observations this request produced.
    pub observations: Vec<Observation>,
}

/// Run `req` on `ws`. Tool errors become a failed outcome (with an error
/// observation), never a panic.
pub fn dispatch(ws: &Workspace, req: &ToolRequest) -> ToolOutcome {
    match req {
        ToolRequest::ViewFile(r) => match ws.view_file(r) {
            Ok(o) => {
                let mut text = String::new();
                text.push_str(&format!(
                    "view {} sha256={} total_lines={}{}\n",
                    o.path,
                    o.hash,
                    o.total_lines,
                    if o.truncated { " (truncated)" } else { "" }
                ));
                for line in &o.lines {
                    text.push_str(line);
                    text.push('\n');
                }
                ToolOutcome {
                    ok: true,
                    exit: None,
                    observations: vec![Observation::Text(text)],
                }
            }
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                observations: vec![Observation::Error {
                    signature: format!("view_file {} failed: {}", r.path, e),
                    count: 1,
                }],
            },
        },
        ToolRequest::EditFile(r) => match ws.edit_file(r, None, None) {
            Ok(o) => ToolOutcome {
                ok: true,
                exit: None,
                observations: vec![Observation::File {
                    path: o.path,
                    status: format!(
                        "edited {} -> {} bytes",
                        o.changed.old_len, o.changed.new_len
                    ),
                }],
            },
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                observations: vec![Observation::Error {
                    signature: format!("edit_file {} failed: {}", r.path, e),
                    count: 1,
                }],
            },
        },
        ToolRequest::WriteFile(r) => match ws.write_file(r) {
            Ok(o) => ToolOutcome {
                ok: true,
                exit: None,
                observations: vec![Observation::File {
                    path: o.path,
                    status: format!("written {} bytes", o.bytes_written),
                }],
            },
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                observations: vec![Observation::Error {
                    signature: format!("write_file {} failed: {}", r.path, e),
                    count: 1,
                }],
            },
        },
        ToolRequest::SearchDir(r) => match ws.search_dir(r) {
            Ok(o) => {
                let mut text = String::new();
                text.push_str(&format!(
                    "search /{}/: {} matches{}\n",
                    r.pattern,
                    o.matches.len(),
                    if o.truncated { " (truncated)" } else { "" }
                ));
                for h in &o.matches {
                    text.push_str(&format!("{}:{}: {}\n", h.path, h.line, h.text));
                }
                ToolOutcome {
                    ok: true,
                    exit: None,
                    observations: vec![Observation::Text(text)],
                }
            }
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                observations: vec![Observation::Error {
                    signature: format!("search_dir /{}/ failed: {}", r.pattern, e),
                    count: 1,
                }],
            },
        },
        ToolRequest::ExecuteCommand(r) => match ws.execute_command(r) {
            Ok(o) => command_outcome(r, &o),
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                observations: vec![
                    Observation::Command {
                        program: r.program.clone(),
                        argv: r.args.clone(),
                        exit: None,
                    },
                    Observation::Error {
                        signature: format!("execute_command {} failed: {}", r.program, e),
                        count: 1,
                    },
                ],
            },
        },
    }
}

/// Shape a command's bounded output into its observations. A non-zero exit
/// (or a kill) adds an error observation so the next model turn sees why the
/// command failed and can self-correct.
fn command_outcome(r: &agent_code_tools::ExecuteCommand, o: &CommandOutput) -> ToolOutcome {
    let ok = o.exit_code == Some(0);
    let mut observations = vec![
        Observation::Command {
            program: r.program.clone(),
            argv: r.args.clone(),
            exit: o.exit_code,
        },
        Observation::Text(command_output_text(r, o)),
    ];
    if !ok {
        observations.push(Observation::Error {
            signature: format!("{} failed: {}", r.program, command_output_text(r, o)),
            count: 1,
        });
    }
    ToolOutcome {
        ok,
        exit: o.exit_code,
        observations,
    }
}

/// The model-readable output block for a command: bounded stdout/stderr
/// head+tail, the total byte counts, the truncation flag, and the log-file
/// references for the full output.
fn command_output_text(r: &agent_code_tools::ExecuteCommand, o: &CommandOutput) -> String {
    let exit = o
        .exit_code
        .map(|c| c.to_string())
        .unwrap_or_else(|| "killed".into());
    let mut s = String::new();
    s.push_str(&format!(
        "{} {} -> {}{}\n",
        r.program,
        r.args.join(" "),
        exit,
        if o.timed_out { " (timed out)" } else { "" }
    ));
    s.push_str(&format!(
        "stdout ({} bytes){}:\n",
        o.stdout_total,
        if o.truncated { " [truncated]" } else { "" }
    ));
    s.push_str(&o.stdout_head);
    if o.truncated {
        s.push_str("\n...\n");
        s.push_str(&o.stdout_tail);
    }
    s.push('\n');
    s.push_str(&format!(
        "stderr ({} bytes){}:\n",
        o.stderr_total,
        if o.truncated { " [truncated]" } else { "" }
    ));
    s.push_str(&o.stderr_head);
    if o.truncated {
        s.push_str("\n...\n");
        s.push_str(&o.stderr_tail);
    }
    s.push('\n');
    s.push_str(&format!(
        "logs: stdout={} stderr={}\n",
        o.stdout_log, o.stderr_log
    ));
    s
}

#[cfg(test)]
mod tests {
    use agent_code_tools::{ExecuteCommand, ToolRequest, ViewFile};

    use super::*;

    fn ws_with(files: &[(&str, &str)]) -> (Workspace, std::path::PathBuf) {
        use std::sync::atomic::{AtomicU32, Ordering};
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("agent_code_disp_{}_{seq}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        for (name, body) in files {
            std::fs::write(dir.join(name), body).unwrap();
        }
        (Workspace::new(&dir).unwrap(), dir)
    }

    #[test]
    fn view_observation_carries_content_hash_and_lines() {
        let (ws, _) = ws_with(&[("a.txt", "x\ny\n")]);
        let req = ToolRequest::ViewFile(ViewFile {
            path: "a.txt".into(),
            start_line: 1,
            end_line: 10,
        });
        let out = dispatch(&ws, &req);
        assert!(out.ok);
        let obs = &out.observations;
        let text = match &obs[0] {
            Observation::Text(t) => t,
            other => panic!("expected a Text observation, got {other:?}"),
        };
        assert!(text.starts_with("view a.txt sha256="));
        assert!(text.contains("total_lines=2"));
        // The actual file content reached the model, not just a line count.
        assert!(text.contains("x"));
        assert!(text.contains("y"));
    }

    #[test]
    fn failed_command_records_output_and_error() {
        let (ws, _) = ws_with(&[]);
        let req = ToolRequest::ExecuteCommand(ExecuteCommand {
            program: "sh".into(),
            args: vec!["-c".into(), "echo boom; exit 3".into()],
            cwd: None,
            timeout_seconds: 10,
            env: std::collections::BTreeMap::new(),
        });
        let out = dispatch(&ws, &req);
        assert!(!out.ok);
        assert_eq!(out.exit, Some(3));
        let obs = &out.observations;
        assert!(matches!(
            &obs[0],
            Observation::Command { exit: Some(3), .. }
        ));
        // The command output reached the model so it can read why it failed.
        let text = match &obs[1] {
            Observation::Text(t) => t,
            other => panic!("expected a Text observation, got {other:?}"),
        };
        assert!(text.contains("boom"));
        assert!(matches!(&obs[2], Observation::Error { .. }));
    }
}
