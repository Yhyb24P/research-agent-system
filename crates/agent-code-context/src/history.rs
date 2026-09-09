//! Deterministic compaction of a durable observation history into a bounded
//! model-facing context. The durable source is only read, never mutated.

use agent_code_core::SessionId;
use agent_code_model::{ModelContext, Observation};

use crate::budget::{ContextBudget, ContextError, TokenCounter};
use crate::build::{build_context, ContextSpec};

/// A read-only source of a session's durable observations. Implementations own
/// the durable store; the context layer only reads from it.
pub trait HistorySource {
    fn observations(&self, session: &SessionId) -> Result<Vec<Observation>, ContextError>;
}

/// The write side of a durable observation history. The runtime appends
/// decisions and tool results through it; the storage layer implements it.
/// Paired with [`HistorySource`]: a store that is both can feed the next
/// model turn what it just recorded.
pub trait HistorySink {
    fn append(&self, session: &SessionId, obs: &Observation) -> Result<(), ContextError>;
}

/// Collapse older observations into a bounded, deterministic summary that
/// retains commands (program/argv/exit), changed files, tool outcomes, error
/// signatures (deduplicated, counts summed), and deduplicated log lines, plus
/// an explicit count of how many observations were compacted.
pub fn compact_older(older: &[Observation]) -> String {
    if older.is_empty() {
        return String::new();
    }
    let n = older.len();
    let mut cmds: Vec<(String, u32)> = Vec::new();
    let mut files: Vec<(String, u32)> = Vec::new();
    let mut tools: Vec<(String, u32)> = Vec::new();
    let mut errs: Vec<(String, u32)> = Vec::new();
    let mut logs: Vec<(String, u32)> = Vec::new();

    for o in older {
        match o {
            Observation::Command {
                program,
                argv,
                exit,
            } => {
                let exit = exit
                    .map(|c| c.to_string())
                    .unwrap_or_else(|| "killed".into());
                bump(&mut cmds, format!("{program} {} -> {exit}", argv.join(" ")));
            }
            Observation::File { path, status } => {
                bump(&mut files, format!("{path}({status})"));
            }
            Observation::Tool { name, ok } => {
                bump(
                    &mut tools,
                    format!("{name} {}", if *ok { "ok" } else { "fail" }),
                );
            }
            Observation::Error { signature, count } => {
                if let Some(e) = errs.iter_mut().find(|e| e.0 == *signature) {
                    e.1 += count;
                } else {
                    errs.push((signature.clone(), *count));
                }
            }
            Observation::Text(t) => {
                bump(&mut logs, t.clone());
            }
        }
    }

    let mut out = format!("[compacted {n} older observations]");
    for (label, items) in [
        ("cmd", &cmds),
        ("file", &files),
        ("tool", &tools),
        ("err", &errs),
        ("log", &logs),
    ] {
        if !items.is_empty() {
            out.push_str(&format!("\n{label}: {}", render_list(items)));
        }
    }
    out
}

/// Increment a deduplicated counter, preserving first-seen order.
fn bump(list: &mut Vec<(String, u32)>, key: String) {
    if let Some(e) = list.iter_mut().find(|(k, _)| *k == key) {
        e.1 += 1;
    } else {
        list.push((key, 1));
    }
}

/// Render a deduplicated list as `key` / `key xN`, capped with a `+k more`.
fn render_list(items: &[(String, u32)]) -> String {
    const CAP: usize = 6;
    let shown: Vec<String> = items
        .iter()
        .take(CAP)
        .map(|(k, c)| {
            if *c > 1 {
                format!("{k} x{c}")
            } else {
                k.clone()
            }
        })
        .collect();
    let mut s = shown.join("; ");
    if items.len() > CAP {
        s.push_str(&format!(" (+{} more)", items.len() - CAP));
    }
    s
}

/// Read a session's durable observations and project them into a bounded
/// [`ModelContext`]. Recent observations are kept verbatim; older ones are
/// compacted into a summary. The source is never mutated.
pub fn build_compact_context(
    source: &dyn HistorySource,
    session: &SessionId,
    task: &str,
    project_rules: &str,
    repository_map: &str,
    budget: &ContextBudget,
    counter: &dyn TokenCounter,
) -> Result<ModelContext, ContextError> {
    let durable = source.observations(session)?;
    // Probe how many recent observations fit when nothing is compacted.
    let probe = ContextSpec {
        task,
        project_rules,
        repository_map,
        compact_summary: "",
        observations: &durable,
    };
    let keep = build_context(&probe, budget, counter)?.observations.len();
    let recent_start = durable.len() - keep;
    let older = &durable[..recent_start];
    let recent = &durable[recent_start..];
    let compact_summary = compact_older(older);
    let spec = ContextSpec {
        task,
        project_rules,
        repository_map,
        compact_summary: &compact_summary,
        observations: recent,
    };
    build_context(&spec, budget, counter)
}

#[cfg(test)]
mod tests {
    use agent_code_core::SessionId;

    use super::*;
    use crate::budget::{BytesTokenCounter, ContextBudget};

    fn obs(n: usize) -> Vec<Observation> {
        (0..n)
            .map(|i| Observation::Command {
                program: "cargo".into(),
                argv: vec![format!("step-{i}")],
                exit: Some(0),
            })
            .collect()
    }

    /// A source that serves a fixed slice, proving the context layer never
    /// mutates the durable history.
    struct Fixed(Vec<Observation>);
    impl HistorySource for Fixed {
        fn observations(&self, _session: &SessionId) -> Result<Vec<Observation>, ContextError> {
            Ok(self.0.clone())
        }
    }

    #[test]
    fn compact_retains_required_fields_and_counts() {
        let older = vec![
            Observation::Command {
                program: "cargo".into(),
                argv: vec!["test".into()],
                exit: Some(0),
            },
            Observation::Command {
                program: "cargo".into(),
                argv: vec!["test".into()],
                exit: Some(0),
            },
            Observation::File {
                path: "a.rs".into(),
                status: "edited".into(),
            },
            Observation::Error {
                signature: "E0308".into(),
                count: 2,
            },
            Observation::Error {
                signature: "E0308".into(),
                count: 3,
            },
            Observation::Tool {
                name: "edit_file".into(),
                ok: false,
            },
            Observation::Text("matched 3 files".into()),
            Observation::Text("matched 3 files".into()),
        ];
        let s = compact_older(&older);
        assert!(s.contains("[compacted 8 older observations]"));
        assert!(s.contains("cargo test -> 0 x2"));
        assert!(s.contains("a.rs(edited)"));
        // Same error signature is summed: 2 + 3 = 5.
        assert!(s.contains("E0308 x5"));
        assert!(s.contains("edit_file fail"));
        // Repeated log lines are deduplicated with a count.
        assert!(s.contains("matched 3 files x2"));
    }

    #[test]
    fn compact_empty_is_empty() {
        assert_eq!(compact_older(&[]), "");
    }

    #[test]
    fn compact_context_keeps_recent_and_compacts_older() {
        let source = Fixed(obs(30));
        let session = SessionId::new("s");
        let budget = ContextBudget::new(100, 0);
        let counter = BytesTokenCounter::default();
        let ctx = build_compact_context(&source, &session, "the task", "", "", &budget, &counter)
            .unwrap();
        // Not all 30 fit: some are kept recent, the rest are older.
        assert!(!ctx.observations.is_empty());
        assert!(ctx.observations.len() < 30);
        // The kept set is the newest suffix; the older prefix is compacted.
        let older = &source.0[..source.0.len() - ctx.observations.len()];
        assert!(!older.is_empty());
        let summary = compact_older(older);
        assert!(summary.contains("older observations"));
        assert!(summary.contains("cargo"));
        // The durable source is untouched.
        assert_eq!(source.0.len(), 30);
    }

    #[test]
    fn compact_context_fits_budget() {
        let source = Fixed(obs(50));
        let session = SessionId::new("s");
        let budget = ContextBudget::new(100, 0);
        let counter = BytesTokenCounter::default();
        let ctx = build_compact_context(&source, &session, "the task", "", "", &budget, &counter)
            .unwrap();
        assert!(counter.count(&crate::build::serialize_context(&ctx)) <= budget.free_tokens());
    }
}
