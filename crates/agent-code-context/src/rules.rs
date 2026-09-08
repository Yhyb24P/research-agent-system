//! Bounded project-rule loading for the model context.

use std::path::Path;

use crate::budget::truncate_chars;

/// Known root-level rule files, in priority order. All that exist are
/// combined (not stop-at-first); the current project's AGENTS.md is first.
const RULE_FILES: [&str; 3] = ["AGENTS.md", "CLAUDE.md", "QWEN.md"];

/// Combine the existing root-level rule files, each under a source header,
/// bounded to `cap` characters. Missing or unreadable files are skipped.
///
/// Per-directory (nested) rules are deferred to R4.
pub fn load_project_rules(root: &Path, cap: usize) -> String {
    let mut out = String::new();
    for name in RULE_FILES {
        if let Ok(body) = std::fs::read_to_string(root.join(name)) {
            if !out.is_empty() {
                out.push('\n');
            }
            out.push_str(&format!("## {name}\n{body}"));
        }
    }
    truncate_chars(&out, cap)
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU32, Ordering};

    use super::*;

    fn temp_dir() -> std::path::PathBuf {
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("agent_code_rules_{}_{seq}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn rules_combine_in_priority_order_with_headers() {
        let dir = temp_dir();
        std::fs::write(dir.join("AGENTS.md"), "agent rules").unwrap();
        std::fs::write(dir.join("CLAUDE.md"), "claude rules").unwrap();
        let rules = load_project_rules(&dir, 1000);
        assert!(rules.contains("## AGENTS.md\nagent rules"));
        assert!(rules.contains("## CLAUDE.md\nclaude rules"));
        // AGENTS.md (priority) comes first.
        assert!(rules.find("AGENTS.md").unwrap() < rules.find("CLAUDE.md").unwrap());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn rules_respect_the_char_cap() {
        let dir = temp_dir();
        std::fs::write(dir.join("AGENTS.md"), "x".repeat(500)).unwrap();
        let rules = load_project_rules(&dir, 100);
        assert_eq!(rules.chars().count(), 100);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn rules_absent_is_empty() {
        let dir = temp_dir();
        assert_eq!(load_project_rules(&dir, 100), "");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
