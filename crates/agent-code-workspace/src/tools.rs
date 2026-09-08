use agent_code_tools::{SearchDir, ViewFile};
use glob::Pattern;
use ignore::WalkBuilder;
use regex::Regex;

use crate::error::ToolError;
use crate::workspace::Workspace;

/// Maximum number of lines a single view_file call returns.
pub const MAX_VIEW_LINES: u32 = 300;
/// Maximum characters kept per search hit (line text is capped, not the file).
const MAX_HIT_CHARS: usize = 500;

/// The bounded result of a view_file call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ViewOutput {
    pub path: String,
    /// SHA-256 of the whole file, usable as an expected_file_hash guard.
    pub hash: String,
    pub total_lines: usize,
    /// Requested lines, each formatted as `{line_no:>5}  {text}`.
    pub lines: Vec<String>,
    /// True when the requested range exceeded the line cap.
    pub truncated: bool,
}

/// A single search hit: a matching line, never the whole file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SearchHit {
    /// Path relative to the workspace root.
    pub path: String,
    pub line: u32,
    pub text: String,
}

/// The bounded result of a search_dir call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SearchOutput {
    pub matches: Vec<SearchHit>,
    /// True when the result hit max_matches and more may exist.
    pub truncated: bool,
}

impl Workspace {
    /// Return up to MAX_VIEW_LINES numbered lines of a contained file, plus
    /// its SHA-256 hash for use as an expected_file_hash guard.
    pub fn view_file(&self, req: &ViewFile) -> Result<ViewOutput, ToolError> {
        let content = self.read_file(&req.path)?;
        let hash = self.hash_file(&req.path)?;
        let all: Vec<&str> = content.lines().collect();
        let total = all.len();
        let start = req.start_line.max(1) as usize;
        let end = req.end_line.min(total as u32) as usize;
        if start > total || start > end {
            return Ok(ViewOutput {
                path: req.path.clone(),
                hash,
                total_lines: total,
                lines: Vec::new(),
                truncated: false,
            });
        }
        let stop = end.min((start - 1) + MAX_VIEW_LINES as usize);
        let lines = (start..=stop)
            .map(|n| format!("{:>5}  {}", n, all[n - 1]))
            .collect();
        Ok(ViewOutput {
            path: req.path.clone(),
            hash,
            total_lines: total,
            lines,
            truncated: stop < end,
        })
    }

    /// Bounded, gitignore-aware search across the workspace. Returns matching
    /// lines (never whole files), capped at req.max_matches.
    pub fn search_dir(&self, req: &SearchDir) -> Result<SearchOutput, ToolError> {
        let re =
            Regex::new(&req.pattern).map_err(|e| ToolError::Io(format!("invalid pattern: {e}")))?;
        let glob_pat = req
            .glob
            .as_deref()
            .map(Pattern::new)
            .transpose()
            .map_err(|e| ToolError::Io(format!("invalid glob: {e}")))?;
        let walker = WalkBuilder::new(self.root())
            .git_ignore(true)
            .hidden(true)
            .build();
        let mut matches = Vec::new();
        for entry in walker {
            let entry = entry.map_err(|e| ToolError::Io(e.to_string()))?;
            if !entry.file_type().is_some_and(|t| t.is_file()) {
                continue;
            }
            let abs = entry.path();
            let rel = abs
                .strip_prefix(self.root())
                .unwrap_or(abs)
                .to_string_lossy()
                .into_owned();
            if let Some(pat) = &glob_pat {
                if !pat.matches(&rel) {
                    continue;
                }
            }
            let Ok(content) = std::fs::read_to_string(abs) else {
                continue; // skip binary / non-UTF-8 files
            };
            for (idx, line) in content.lines().enumerate() {
                if re.is_match(line) {
                    matches.push(SearchHit {
                        path: rel.clone(),
                        line: (idx + 1) as u32,
                        text: cap_chars(line.trim_end(), MAX_HIT_CHARS),
                    });
                    if (matches.len() as u32) >= req.max_matches {
                        return Ok(SearchOutput {
                            matches,
                            truncated: true,
                        });
                    }
                }
            }
        }
        Ok(SearchOutput {
            matches,
            truncated: false,
        })
    }
}

/// Char-safe truncation of a search hit to `max` characters.
fn cap_chars(s: &str, max: usize) -> String {
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i >= max {
            out.push('…');
            break;
        }
        out.push(c);
    }
    out
}

#[cfg(test)]
mod tests {
    use agent_code_tools::{SearchDir, ViewFile};

    use super::*;

    fn ws_with(files: &[(&str, &str)]) -> (Workspace, std::path::PathBuf) {
        use std::sync::atomic::{AtomicU32, Ordering};
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("agent_code_tools_{}_{seq}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        for (name, body) in files {
            let p = dir.join(name);
            if let Some(parent) = p.parent() {
                std::fs::create_dir_all(parent).unwrap();
            }
            std::fs::write(&p, body).unwrap();
        }
        (Workspace::new(&dir).unwrap(), dir)
    }

    #[test]
    fn view_numbers_lines_and_caps_at_300() {
        let body: String = (1..=500)
            .map(|i| format!("line {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        let (ws, _) = ws_with(&[("big.txt", &body)]);
        let out = ws
            .view_file(&ViewFile {
                path: "big.txt".into(),
                start_line: 1,
                end_line: 500,
            })
            .unwrap();
        assert_eq!(out.lines.len(), MAX_VIEW_LINES as usize);
        assert!(out.truncated);
        assert_eq!(out.total_lines, 500);
        assert_eq!(out.lines[0], "    1  line 1");
        assert_eq!(out.hash.len(), 64);
    }

    #[test]
    fn view_respects_requested_range() {
        let (ws, _) = ws_with(&[("f.txt", "a\nb\nc\nd\n")]);
        let out = ws
            .view_file(&ViewFile {
                path: "f.txt".into(),
                start_line: 2,
                end_line: 3,
            })
            .unwrap();
        assert_eq!(out.lines, vec!["    2  b", "    3  c"]);
        assert!(!out.truncated);
    }

    #[test]
    fn search_finds_matching_lines_bounded() {
        let (ws, _) = ws_with(&[
            ("a.rs", "fn alpha() {}\nfn beta() {}\n"),
            ("b.rs", "fn alpha() {}\n"),
        ]);
        let out = ws
            .search_dir(&SearchDir {
                pattern: "alpha".into(),
                glob: None,
                max_matches: 1,
            })
            .unwrap();
        assert!(out.truncated);
        assert_eq!(out.matches.len(), 1);
        assert_eq!(out.matches[0].line, 1);
    }

    #[test]
    fn search_glob_filters_files() {
        let (ws, _) = ws_with(&[("a.rs", "needle\n"), ("b.txt", "needle\n")]);
        let out = ws
            .search_dir(&SearchDir {
                pattern: "needle".into(),
                glob: Some("**/*.rs".into()),
                max_matches: 10,
            })
            .unwrap();
        assert_eq!(out.matches.len(), 1);
        assert_eq!(out.matches[0].path, "a.rs");
    }
}
