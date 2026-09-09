//! Dispatch a typed tool request to the contained workspace and shape the
//! result into a bounded, model-readable outcome plus durable observations.

use agent_code_model::Observation;
use agent_code_tools::ToolRequest;
use agent_code_workspace::Workspace;

/// The bounded outcome of dispatching one tool request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolOutcome {
    /// Whether the tool succeeded (a non-zero command exit is a failure).
    pub ok: bool,
    /// A short, model-readable summary of the result (bounded).
    pub text: String,
    /// For `execute_command`: the exit code, or `None` when killed/timeout.
    pub exit: Option<i32>,
}

/// Run `req` on `ws`. Tool errors become a failed outcome, not a panic.
pub fn dispatch(ws: &Workspace, req: &ToolRequest) -> ToolOutcome {
    match req {
        ToolRequest::ViewFile(r) => match ws.view_file(r) {
            Ok(o) => ToolOutcome {
                ok: true,
                exit: None,
                text: format!("view {}: {} lines", o.path, o.total_lines),
            },
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                text: e.to_string(),
            },
        },
        ToolRequest::EditFile(r) => match ws.edit_file(r, None, None) {
            Ok(o) => ToolOutcome {
                ok: true,
                exit: None,
                text: format!(
                    "edited {} ({} -> {} bytes)",
                    o.path, o.changed.old_len, o.changed.new_len
                ),
            },
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                text: e.to_string(),
            },
        },
        ToolRequest::WriteFile(r) => match ws.write_file(r) {
            Ok(o) => ToolOutcome {
                ok: true,
                exit: None,
                text: format!("wrote {} ({} bytes)", o.path, o.bytes_written),
            },
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                text: e.to_string(),
            },
        },
        ToolRequest::SearchDir(r) => match ws.search_dir(r) {
            Ok(o) => {
                let t = if o.truncated { " (truncated)" } else { "" };
                ToolOutcome {
                    ok: true,
                    exit: None,
                    text: format!("search /{}/: {} matches{}", r.pattern, o.matches.len(), t),
                }
            }
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                text: e.to_string(),
            },
        },
        ToolRequest::ExecuteCommand(r) => match ws.execute_command(r) {
            Ok(o) => {
                let ok = o.exit_code == Some(0);
                let text = if ok {
                    format!("{} -> ok", r.program)
                } else {
                    let code = o
                        .exit_code
                        .map(|c| c.to_string())
                        .unwrap_or_else(|| "killed".into());
                    // Surface the failure detail (stderr, else stdout) so the
                    // model can read why the command failed.
                    let detail = if o.stderr_tail.trim().is_empty() {
                        o.stdout_tail.trim()
                    } else {
                        o.stderr_tail.trim()
                    };
                    format!(
                        "{} -> {}{}",
                        r.program,
                        code,
                        if detail.is_empty() {
                            String::new()
                        } else {
                            format!(": {detail}")
                        }
                    )
                };
                ToolOutcome {
                    ok,
                    exit: o.exit_code,
                    text,
                }
            }
            Err(e) => ToolOutcome {
                ok: false,
                exit: None,
                text: e.to_string(),
            },
        },
    }
}

/// The durable observations a tool request + outcome contribute to history.
/// A failed command additionally records its error so the next model turn sees
/// why it failed (the self-correction signal).
pub fn observations_for(req: &ToolRequest, outcome: &ToolOutcome) -> Vec<Observation> {
    let mut v = Vec::new();
    match req {
        ToolRequest::ExecuteCommand(r) => {
            v.push(Observation::Command {
                program: r.program.clone(),
                argv: r.args.clone(),
                exit: outcome.exit,
            });
            if !outcome.ok {
                v.push(Observation::Error {
                    signature: format!("{} failed: {}", r.program, outcome.text),
                    count: 1,
                });
            }
        }
        ToolRequest::EditFile(r) => {
            if outcome.ok {
                v.push(Observation::File {
                    path: r.path.clone(),
                    status: "edited".into(),
                });
            } else {
                v.push(Observation::Error {
                    signature: format!("edit_file {} failed: {}", r.path, outcome.text),
                    count: 1,
                });
            }
        }
        ToolRequest::WriteFile(r) => {
            if outcome.ok {
                v.push(Observation::File {
                    path: r.path.clone(),
                    status: "written".into(),
                });
            } else {
                v.push(Observation::Error {
                    signature: format!("write_file {} failed: {}", r.path, outcome.text),
                    count: 1,
                });
            }
        }
        ToolRequest::ViewFile(_) => {
            v.push(Observation::Text(outcome.text.clone()));
        }
        ToolRequest::SearchDir(r) => {
            v.push(Observation::Text(outcome.text.clone()));
            if !outcome.ok {
                v.push(Observation::Error {
                    signature: format!("search_dir /{}/ failed: {}", r.pattern, outcome.text),
                    count: 1,
                });
            }
        }
    }
    v
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
    fn view_produces_a_text_observation() {
        let (ws, _) = ws_with(&[("a.txt", "x\ny\n")]);
        let req = ToolRequest::ViewFile(ViewFile {
            path: "a.txt".into(),
            start_line: 1,
            end_line: 10,
        });
        let out = dispatch(&ws, &req);
        assert!(out.ok);
        let obs = observations_for(&req, &out);
        assert!(matches!(&obs[0], Observation::Text(t) if t.starts_with("view a.txt")));
    }

    #[test]
    fn failed_command_records_a_command_and_error_observation() {
        let (ws, _) = ws_with(&[]);
        let req = ToolRequest::ExecuteCommand(ExecuteCommand {
            program: "sh".into(),
            args: vec!["-c".into(), "exit 3".into()],
            cwd: None,
            timeout_seconds: 10,
            env: std::collections::BTreeMap::new(),
        });
        let out = dispatch(&ws, &req);
        assert!(!out.ok);
        assert_eq!(out.exit, Some(3));
        let obs = observations_for(&req, &out);
        assert!(matches!(
            &obs[0],
            Observation::Command { exit: Some(3), .. }
        ));
        assert!(matches!(&obs[1], Observation::Error { .. }));
    }
}
