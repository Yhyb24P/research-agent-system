use agent_code_tools::{EditFile, WriteFile};

use crate::error::ToolError;
use crate::workspace::Workspace;

/// A pluggable post-edit check run on the candidate content before the write.
/// A failure leaves the file untouched (the guard runs pre-write).
pub type SyntaxGuard = Box<dyn Fn(&str) -> Result<(), String> + Send + Sync>;

/// The result of a write_file call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteOutput {
    pub path: String,
    pub bytes_written: u64,
    /// SHA-256 of the file after the write.
    pub hash: String,
}

/// The result of an edit_file call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EditOutput {
    pub path: String,
    /// SHA-256 of the file after the edit.
    pub hash: String,
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
        if let Some(limit) = req.max_bytes {
            if req.content.len() as u64 > limit {
                return Err(ToolError::TooLarge {
                    path: req.path.clone(),
                    limit,
                });
            }
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
    /// Line endings are normalized to LF for matching (limited normalization);
    /// a stale-hash guard and an optional syntax guard protect the write.
    pub fn edit_file(
        &self,
        req: &EditFile,
        guard: Option<&SyntaxGuard>,
    ) -> Result<EditOutput, ToolError> {
        if req.old_str.is_empty() {
            return Err(ToolError::MatchNotFound(req.path.clone()));
        }
        let raw = self.read_file(&req.path)?;
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
        let content = normalize_newlines(&raw);
        let old = normalize_newlines(&req.old_str);
        let new = normalize_newlines(&req.new_str);
        let count = content.matches(&old).count();
        if count == 0 {
            return Err(ToolError::MatchNotFound(req.path.clone()));
        }
        if count > 1 {
            return Err(ToolError::DuplicateMatch {
                path: req.path.clone(),
                count,
            });
        }
        let updated = content.replacen(&old, &new, 1);
        if let Some(g) = guard {
            g(&updated).map_err(ToolError::SyntaxGuardFailed)?;
        }
        self.atomic_write(&req.path, &updated)?;
        let hash = self.hash_file(&req.path)?;
        Ok(EditOutput {
            path: req.path.clone(),
            hash,
        })
    }
}

/// Limited normalization: unify CRLF and lone CR to LF so matching is
/// robust to line-ending differences. Trailing whitespace is left intact to
/// keep the match exact and predictable.
fn normalize_newlines(s: &str) -> String {
    s.replace("\r\n", "\n").replace('\r', "\n")
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
            )
            .unwrap_err();
        assert!(matches!(err, ToolError::DuplicateMatch { count: 2, .. }));
    }

    #[test]
    fn edit_applies_unique_match() {
        let (ws, dir) = ws_with(&[("a.txt", "hello world\n")]);
        ws.edit_file(
            &EditFile {
                path: "a.txt".into(),
                old_str: "world".into(),
                new_str: "rust".into(),
                expected_file_hash: None,
            },
            None,
        )
        .unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.join("a.txt")).unwrap(),
            "hello rust\n"
        );
    }

    #[test]
    fn edit_syntax_guard_failure_leaves_file_intact() {
        let (ws, dir) = ws_with(&[("a.txt", "good\n")]);
        let guard: SyntaxGuard = Box::new(|c| {
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
                Some(&guard),
            )
            .unwrap_err();
        assert!(matches!(err, ToolError::SyntaxGuardFailed(_)));
        assert_eq!(
            std::fs::read_to_string(dir.join("a.txt")).unwrap(),
            "good\n"
        );
    }
}
