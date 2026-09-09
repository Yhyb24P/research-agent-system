/// A request to the model: the compact context the Agent wants a decision on.
///
/// Sections are billed by the context layer in priority order
/// (task, rules, map, recent observations, compact summary). An empty string
/// means that section is absent.
#[derive(Debug, Clone, Default)]
pub struct ModelContext {
    /// The current task objective.
    pub task: String,
    /// Bounded project rules (AGENTS.md and friends).
    pub project_rules: String,
    /// A shallow, bounded repository map.
    pub repository_map: String,
    /// A deterministic compaction of older observations.
    pub compact_summary: String,
    /// Recent observations as the exact bounded text that was billed into the
    /// context (head/tail truncated), so the serialized context can never
    /// exceed the budget because of uncounted re-rendering.
    pub observations: Vec<String>,
}

/// One structured thing the Agent observed. Plain data.
///
/// The durable journal stores these verbatim and never compacts them; the
/// context layer projects a bounded view of them for the model.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum Observation {
    /// A command ran. `exit` is `None` when the process was killed (timeout).
    Command {
        program: String,
        argv: Vec<String>,
        exit: Option<i32>,
    },
    /// A file was created or edited.
    File { path: String, status: String },
    /// A tool call outcome.
    Tool { name: String, ok: bool },
    /// An error, deduplicated by signature with an occurrence count.
    Error { signature: String, count: u32 },
    /// Free-form text: search hits, snippets, notes.
    Text(String),
}

impl Observation {
    /// Stable discriminator, used as the durable `kind` column.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Command { .. } => "cmd",
            Self::File { .. } => "file",
            Self::Tool { .. } => "tool",
            Self::Error { .. } => "err",
            Self::Text(_) => "text",
        }
    }

    /// Canonical model-facing text for this observation.
    pub fn render(&self) -> String {
        match self {
            Self::Command {
                program,
                argv,
                exit,
            } => {
                let exit = match exit {
                    Some(code) => code.to_string(),
                    None => "killed".to_string(),
                };
                if argv.is_empty() {
                    format!("cmd {program} -> {exit}")
                } else {
                    format!("cmd {program} {} -> {exit}", argv.join(" "))
                }
            }
            Self::File { path, status } => format!("file {path} ({status})"),
            Self::Tool { name, ok } => format!("tool {name}: {}", if *ok { "ok" } else { "fail" }),
            Self::Error { signature, count } => format!("error x{count}: {signature}"),
            Self::Text(t) => t.clone(),
        }
    }

    /// Lossless durable form: a JSON document. Round-trips every field exactly,
    /// including embedded control characters, empty strings, and Unicode.
    pub fn encode(&self) -> String {
        serde_json::to_string(self).expect("Observation is JSON-serializable")
    }

    /// Parse the durable form produced by [`encode`](Self::encode).
    pub fn decode(s: &str) -> Result<Self, String> {
        serde_json::from_str(s).map_err(|e| e.to_string())
    }
}

/// The model's decision: either a final answer or a tool call to run next.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModelDecision {
    /// The task is complete; the inner value is the final summary.
    Final(String),
    /// Run a tool next; the inner value is a typed tool request.
    ToolCall(agent_code_tools::ToolRequest),
}

/// Errors returned by a [`ModelClient`](crate::ModelClient).
#[derive(Debug)]
pub enum ModelError {
    /// The model endpoint could not be reached.
    Transport(String),
    /// The model returned a response the Agent cannot use.
    BadResponse(String),
}

impl std::fmt::Display for ModelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Transport(msg) => write!(f, "model transport error: {msg}"),
            Self::BadResponse(msg) => write!(f, "model returned an unusable response: {msg}"),
        }
    }
}

impl std::error::Error for ModelError {}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(o: &Observation) {
        let decoded = Observation::decode(&o.encode()).expect("decode own encoding");
        assert_eq!(&decoded, o);
    }

    #[test]
    fn command_roundtrip() {
        let o = Observation::Command {
            program: "cargo".into(),
            argv: vec!["test".into(), "--release".into()],
            exit: Some(0),
        };
        roundtrip(&o);
        assert!(o.render().contains("cargo test --release"));
    }

    #[test]
    fn killed_command_encodes_missing_exit() {
        let o = Observation::Command {
            program: "sh".into(),
            argv: vec![],
            exit: None,
        };
        let decoded = Observation::decode(&o.encode()).unwrap();
        assert_eq!(decoded, o);
        assert!(o.render().contains("killed"));
    }

    #[test]
    fn all_variants_roundtrip() {
        roundtrip(&Observation::File {
            path: "a.rs".into(),
            status: "edited".into(),
        });
        roundtrip(&Observation::Tool {
            name: "edit_file".into(),
            ok: false,
        });
        roundtrip(&Observation::Error {
            signature: "E0308 mismatched types".into(),
            count: 3,
        });
        roundtrip(&Observation::Text("a note".into()));
    }

    #[test]
    fn hostile_fields_roundtrip_losslessly() {
        // Both control characters (\u{1f}, \u{1d}), empty strings, and Unicode
        // in every variant must round-trip exactly — the durable-history
        // invariant. The old hand-rolled codec broke on these.
        roundtrip(&Observation::Command {
            program: "pr\u{1f}og\u{1d}ram".into(),
            argv: vec![
                "a\u{1d}b".into(),
                "\u{1f}".into(),
                String::new(),
                "中文".into(),
            ],
            exit: Some(-1),
        });
        roundtrip(&Observation::File {
            path: "p\u{1f}ath".into(),
            status: String::new(),
        });
        roundtrip(&Observation::Tool {
            name: "t\u{1d}ool".into(),
            ok: true,
        });
        roundtrip(&Observation::Error {
            signature: "sig\u{1f}中文".into(),
            count: 0,
        });
        roundtrip(&Observation::Text("x\u{1f}y\u{1d}z 中文".into()));
    }

    #[test]
    fn decode_rejects_non_json() {
        assert!(Observation::decode("not json").is_err());
    }
}
