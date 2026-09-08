use agent_code_tools::{EditFile, WriteFile};

use crate::error::ToolError;
use crate::workspace::Workspace;

/// Pre-write check on the candidate content (e.g. an in-memory parser). A
/// failure leaves the file untouched.
pub type CandidateGuard = Box<dyn Fn(&str) -> Result<(), String> + Send + Sync>;

/// Post-write check with access to the real path/workspace (e.g. a compiler or
/// project toolchain). On failure the original bytes are restored atomically.
pub type PostWriteGuard = Box<dyn Fn(&Workspace, &str) -> Result<(), String> + Send + Sync>;

/// The result of a write_file call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteOutput {
    pub path: String,
    pub bytes_written: u64,
    /// SHA-256 of the file after the write.
    pub hash: String,
}

/// The byte region an edit replaced.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChangedRange {
    /// Byte offset in the file where the replacement began.
    pub start: usize,
    /// Number of bytes removed.
    pub old_len: usize,
    /// Number of bytes inserted.
    pub new_len: usize,
}

/// The result of an edit_file call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EditOutput {
    pub path: String,
    /// SHA-256 of the file before the edit.
    pub before_hash: String,
    /// SHA-256 of the file after the edit.
    pub after_hash: String,
    /// The byte region that changed.
    pub changed: ChangedRange,
}

impl Workspace {
    /// Create or replace a contained file, guarded by create-only, size, and
    /// stale-hash checks. Writes atomically.
    pub fn write_file(&self, req: &WriteFile) -> Result<WriteOutput, ToolError> {
        let target = self.resolve_target(&req.path)?;
        let exists = target.exists();
        if req.create_only && exists {
            return Err(ToolError::AlreadyExists(req.path.clone()));
        }
        // Effective cap: the workspace hard ceiling, tightened by the request.
        let limit = self
            .max_write_bytes()
            .min(req.max_bytes.unwrap_or(u64::MAX));
        if req.content.len() as u64 > limit {
            return Err(ToolError::TooLarge {
                path: req.path.clone(),
                limit,
            });
        }
        if exists {
            if let Some(expected) = &req.expected_file_hash {
                let actual = self.hash_file(&req.path)?;
                if actual != *expected {
                    return Err(ToolError::StaleHash {
                        path: req.path.clone(),
                        expected: expected.clone(),
                        actual,
                    });
                }
            }
        }
        let bytes_written = req.content.len() as u64;
        self.atomic_write(&req.path, &req.content)?;
        let hash = self.hash_file(&req.path)?;
        Ok(WriteOutput {
            path: req.path.clone(),
            bytes_written,
            hash,
        })
    }

    /// Replace a single, exact, unique occurrence of old_str with new_str.
    ///
    /// The raw text is searched exactly first. Only on zero matches does a
    /// limited line-ending normalization kick in (matching the file's own
    /// convention), so unrelated bytes are never rewritten. A candidate
    /// guard runs pre-write; a post-write guard runs after and, on failure,
    /// restores the original bytes atomically.
    pub fn edit_file(
        &self,
        req: &EditFile,
        candidate_guard: Option<&CandidateGuard>,
        post_guard: Option<&PostWriteGuard>,
    ) -> Result<EditOutput, ToolError> {
        if req.old_str.is_empty() {
            return Err(ToolError::MatchNotFound(req.path.clone()));
        }
        // A single byte snapshot supplies both the content and the before-hash,
        // so the stale-hash check can never race the content read.
        let bytes = self.read_bytes(&req.path)?;
        let before_hash = crate::workspace::sha256_hex(&bytes);
        let raw = String::from_utf8(bytes).map_err(|_| ToolError::Io("not valid UTF-8".into()))?;
        if let Some(expected) = &req.expected_file_hash {
            if before_hash != *expected {
                return Err(ToolError::StaleHash {
                    path: req.path.clone(),
                    expected: expected.clone(),
                    actual: before_hash,
                });
            }
        }

        let raw_count = raw.matches(&req.old_str).count();
        let (count, old_adj, new_adj) = if raw_count == 1 {
            (1, req.old_str.clone(), req.new_str.clone())
        } else if raw_count > 1 {
            return Err(ToolError::DuplicateMatch {
                path: req.path.clone(),
                count: raw_count,
            });
        } else {
            let ending = line_ending(&raw);
            let old_adj = to_line_ending(&req.old_str, ending);
            let new_adj = to_line_ending(&req.new_str, ending);
            let c = raw.matches(&old_adj).count();
            if c > 1 {
                return Err(ToolError::DuplicateMatch {
                    path: req.path.clone(),
                    count: c,
                });
            }
            (c, old_adj, new_adj)
        };
        if count == 0 {
            return Err(ToolError::MatchNotFound(req.path.clone()));
        }
        let start = raw.find(old_adj.as_str()).expect("unique match located");
        let updated = raw.replacen(&old_adj, &new_adj, 1);

        if let Some(g) = candidate_guard {
            g(&updated).map_err(ToolError::SyntaxGuardFailed)?;
        }
        self.atomic_write(&req.path, &updated)?;
        if let Some(pg) = post_guard {
            if let Err(msg) = pg(self, &req.path) {
                // Post-write check failed: atomically restore the original bytes.
                self.atomic_write(&req.path, &raw)?;
                return Err(ToolError::SyntaxGuardFailed(msg));
            }
        }
        let after_hash = self.hash_file(&req.path)?;
        Ok(EditOutput {
            path: req.path.clone(),
            before_hash,
            after_hash,
            changed: ChangedRange {
                start,
                old_len: old_adj.len(),
                new_len: new_adj.len(),
            },
        })
    }
}

/// The file's line-ending convention: CRLF if present, otherwise LF.
fn line_ending(raw: &str) -> &'static str {
    if raw.contains("\r\n") {
        "\r\n"
    } else {
        "\n"
    }
}

/// Rewrite a string's line endings to `ending`, normalizing CRLF/CR to LF first.
fn to_line_ending(s: &str, ending: &str) -> String {
    let lf = s.replace("\r\n", "\n").replace('\r', "\n");
    if ending == "\n" {
        lf
    } else {
        lf.replace('\n', ending)
    }
}

#[cfg(test)]
mod tests {
    use agent_code_tools::{EditFile, WriteFile};

    use super::*;

    fn ws_with(files: &[(&str, &str)]) -> (Workspace, std::path::PathBuf) {
        use std::sync::atomic::{AtomicU32, Ordering};
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("agent_code_write_{}_{seq}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        for (name, body) in files {
            std::fs::write(dir.join(name), body).unwrap();
        }
        (Workspace::new(&dir).unwrap(), dir)
    }

    #[test]
    fn write_create_only_rejects_existing() {
        let (ws, dir) = ws_with(&[("a.txt", "old")]);
        let err = ws
            .write_file(&WriteFile {
                path: "a.txt".into(),
                content: "new".into(),
                create_only: true,
                max_bytes: None,
                expected_file_hash: None,
            })
            .unwrap_err();
        assert!(matches!(err, ToolError::AlreadyExists(_)));
        assert_eq!(std::fs::read_to_string(dir.join("a.txt")).unwrap(), "old");
    }

    #[test]
    fn write_enforces_max_bytes() {
        let (ws, _) = ws_with(&[]);
        let err = ws
            .write_file(&WriteFile {
                path: "big.txt".into(),
                content: "0123456789".into(),
                create_only: false,
                max_bytes: Some(5),
                expected_file_hash: None,
            })
            .unwrap_err();
        assert!(matches!(err, ToolError::TooLarge { .. }));
    }

    #[test]
    fn write_enforces_default_cap_when_unspecified() {
        let (ws, _) = ws_with(&[]);
        // A payload over the workspace default cap is rejected even with no
        // explicit max_bytes.
        let big = "0".repeat(ws.max_write_bytes() as usize + 1);
        let err = ws
            .write_file(&WriteFile {
                path: "big.txt".into(),
                content: big,
                create_only: false,
                max_bytes: None,
                expected_file_hash: None,
            })
            .unwrap_err();
        assert!(matches!(err, ToolError::TooLarge { .. }));
    }

    #[test]
    fn write_detects_stale_hash() {
        let (ws, _) = ws_with(&[("a.txt", "current")]);
        let err = ws
            .write_file(&WriteFile {
                path: "a.txt".into(),
                content: "new".into(),
                create_only: false,
                max_bytes: None,
                expected_file_hash: Some("bogus-hash".into()),
            })
            .unwrap_err();
        assert!(matches!(err, ToolError::StaleHash { .. }));
    }

    #[test]
    fn edit_duplicate_old_str_is_rejected() {
        let (ws, _) = ws_with(&[("a.txt", "x\nx\n")]);
        let err = ws
            .edit_file(
                &EditFile {
                    path: "a.txt".into(),
                    old_str: "x".into(),
                    new_str: "y".into(),
                    expected_file_hash: None,
                },
                None,
                None,
            )
            .unwrap_err();
        assert!(matches!(err, ToolError::DuplicateMatch { count: 2, .. }));
    }

    #[test]
    fn edit_applies_unique_match_and_reports_range() {
        let (ws, dir) = ws_with(&[("a.txt", "hello world\n")]);
        let out = ws
            .edit_file(
                &EditFile {
                    path: "a.txt".into(),
                    old_str: "world".into(),
                    new_str: "rust".into(),
                    expected_file_hash: None,
                },
                None,
                None,
            )
            .unwrap();
        assert_eq!(out.changed.start, 6);
        assert_eq!(out.changed.old_len, 5);
        assert_eq!(out.changed.new_len, 4);
        assert_ne!(out.before_hash, out.after_hash);
        assert_eq!(
            std::fs::read_to_string(dir.join("a.txt")).unwrap(),
            "hello rust\n"
        );
    }

    #[test]
    fn edit_normalizes_only_on_zero_exact_matches() {
        // A CRLF file with an LF cross-line anchor: the exact search finds zero
        // (the file uses CRLF), so the fallback matches the file's CRLF
        // convention and rewrites only the target fragment — unrelated bytes
        // keep their original CRLF endings.
        let (ws, dir) = ws_with(&[("a.txt", "line1\r\nline2\r\nline3\r\n")]);
        ws.edit_file(
            &EditFile {
                path: "a.txt".into(),
                old_str: "line1\nline2".into(),
                new_str: "line1\nLINE2".into(),
                expected_file_hash: None,
            },
            None,
            None,
        )
        .unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.join("a.txt")).unwrap(),
            "line1\r\nLINE2\r\nline3\r\n"
        );
    }

    #[test]
    fn edit_candidate_guard_failure_leaves_file_intact() {
        let (ws, dir) = ws_with(&[("a.txt", "good\n")]);
        let cand: CandidateGuard = Box::new(|c| {
            if c.contains("BAD") {
                Err("introduced BAD".into())
            } else {
                Ok(())
            }
        });
        let err = ws
            .edit_file(
                &EditFile {
                    path: "a.txt".into(),
                    old_str: "good".into(),
                    new_str: "BAD".into(),
                    expected_file_hash: None,
                },
                Some(&cand),
                None,
            )
            .unwrap_err();
        assert!(matches!(err, ToolError::SyntaxGuardFailed(_)));
        assert_eq!(
            std::fs::read_to_string(dir.join("a.txt")).unwrap(),
            "good\n"
        );
    }

    #[test]
    fn edit_post_write_guard_restores_original_bytes() {
        let (ws, dir) = ws_with(&[("a.txt", "good\n")]);
        let before = ws.hash_file("a.txt").unwrap();
        // The post-write guard inspects the real file and rejects the change.
        let post: PostWriteGuard = Box::new(|ws, path| {
            let body = ws.read_file(path).map_err(|e| e.to_string())?;
            if body.contains("BAD") {
                Err("file now contains BAD".into())
            } else {
                Ok(())
            }
        });
        let err = ws
            .edit_file(
                &EditFile {
                    path: "a.txt".into(),
                    old_str: "good".into(),
                    new_str: "BAD".into(),
                    expected_file_hash: None,
                },
                None,
                Some(&post),
            )
            .unwrap_err();
        assert!(matches!(err, ToolError::SyntaxGuardFailed(_)));
        // Original bytes restored exactly.
        assert_eq!(ws.hash_file("a.txt").unwrap(), before);
        assert_eq!(
            std::fs::read_to_string(dir.join("a.txt")).unwrap(),
            "good\n"
        );
    }
}
