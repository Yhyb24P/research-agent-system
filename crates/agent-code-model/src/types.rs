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
    /// Recent, individually bounded observations.
    pub observations: Vec<Observation>,
}

/// One structured thing the Agent observed. Plain data, no dependencies.
///
/// The durable journal stores these verbatim and never compacts them; the
/// context layer projects a bounded view of them for the model.
#[derive(Debug, Clone, PartialEq, Eq)]
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

    /// Compact durable form. Field separator `\u{1f}`, argv sub-separator
    /// `\u{1d}`. Owned by this type; not a public interchange format.
    pub fn encode(&self) -> String {
        match self {
            Self::Command {
                program,
                argv,
                exit,
            } => {
                let args = argv.join("\u{1d}");
                let exit = match exit {
                    Some(c) => c.to_string(),
                    None => "~".to_string(),
                };
                format!("cmd\u{1f}{program}\u{1f}{args}\u{1f}{exit}")
            }
            Self::File { path, status } => format!("file\u{1f}{path}\u{1f}{status}"),
            Self::Tool { name, ok } => {
                format!("tool\u{1f}{name}\u{1f}{}", if *ok { "1" } else { "0" })
            }
            Self::Error { signature, count } => format!("err\u{1f}{signature}\u{1f}{count}"),
            Self::Text(t) => format!("text\u{1f}{t}"),
        }
    }

    /// Parse the durable form produced by [`encode`](Self::encode).
    pub fn decode(s: &str) -> Result<Self, String> {
        let parts: Vec<&str> = s.split('\u{1f}').collect();
        match parts.first().copied() {
            Some("cmd") => {
                let program = parts.get(1).ok_or("cmd: missing program")?.to_string();
                let argv = match parts.get(2) {
                    Some(a) if !a.is_empty() => a.split('\u{1d}').map(str::to_string).collect(),
                    _ => Vec::new(),
                };
                let exit = match parts.get(3) {
                    Some(&"~") => None,
                    Some(v) => Some(v.parse().map_err(|_| "cmd: bad exit".to_string())?),
                    None => None,
                };
                Ok(Self::Command {
                    program,
                    argv,
                    exit,
                })
            }
            Some("file") => Ok(Self::File {
                path: parts.get(1).ok_or("file: missing path")?.to_string(),
                status: parts.get(2).ok_or("file: missing status")?.to_string(),
            }),
            Some("tool") => Ok(Self::Tool {
                name: parts.get(1).ok_or("tool: missing name")?.to_string(),
                ok: matches!(parts.get(2), Some(&"1")),
            }),
            Some("err") => Ok(Self::Error {
                signature: parts.get(1).ok_or("err: missing signature")?.to_string(),
                count: parts.get(2).and_then(|c| c.parse().ok()).unwrap_or(1),
            }),
            Some("text") => {
                // Rejoin the tail in case the text itself contains the field sep.
                Ok(Self::Text(parts[1..].join("\u{1f}")))
            }
            _ => Err(format!("unknown observation kind: {s}")),
        }
    }
}

/// The model's decision: either a final answer or a tool call to run next.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModelDecision {
    /// The task is complete; the inner value is the final summary.
    Final(String),
    /// Run a tool next; the inner value is an opaque request the caller assigns.
    ToolCall(String),
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
        // Text containing the field separator must survive via tail rejoin.
        roundtrip(&Observation::Text("a\u{1f}b".into()));
    }

    #[test]
    fn decode_rejects_unknown_kind() {
        assert!(Observation::decode("wat\u{1f}x").is_err());
    }
}
