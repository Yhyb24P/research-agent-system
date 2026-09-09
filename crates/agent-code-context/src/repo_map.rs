//! A shallow, bounded repository map for the model context.

use std::path::{Path, PathBuf};

use crate::budget::ContextError;

/// Directories never mapped: VCS metadata and build/dependency trees.
const SKIP_DIRS: [&str; 4] = [".git", "target", "node_modules", "__pycache__"];

/// Caps for a repository map. Depth and entry counts are enforced here; the
/// final rendered token cap is enforced by the context builder's map section.
#[derive(Debug, Clone, Copy)]
pub struct RepoMapOptions {
    /// Maximum directory nesting below the root.
    pub max_depth: usize,
    /// Maximum number of path entries.
    pub max_entries: usize,
}

impl RepoMapOptions {
    pub fn new(max_depth: usize, max_entries: usize) -> Self {
        Self {
            max_depth,
            max_entries,
        }
    }
}

/// A bounded, deterministically-ordered map of a repository's paths.
pub struct RepoMap {
    root: String,
    entries: Vec<String>,
    truncated: bool,
    errors: Vec<String>,
    /// Number of directories the traversal actually opened. Stays bounded by
    /// the entry cap: once the cap is reached the walk stops, so this proves
    /// the traversal itself is bounded, not just the result.
    dirs_scanned: usize,
}

impl RepoMap {
    /// Map the tree under `root` (canonicalized). Symlinks are recorded as
    /// leaves but never followed. Traversal errors are collected, not
    /// silently ignored.
    pub fn from_path(root: &Path, opts: &RepoMapOptions) -> Result<RepoMap, ContextError> {
        let canon = std::fs::canonicalize(root).map_err(|e| ContextError::Io(e.to_string()))?;
        let mut entries: Vec<PathBuf> = Vec::new();
        let mut errors: Vec<String> = Vec::new();
        let mut dirs_scanned = 0usize;
        let mut truncated = false;
        walk(
            &canon,
            0,
            opts,
            &mut entries,
            &mut errors,
            &mut dirs_scanned,
            &mut truncated,
        )?;
        let mut rel: Vec<String> = entries
            .iter()
            .map(|p| p.strip_prefix(&canon).unwrap().display().to_string())
            .collect();
        rel.sort();
        Ok(RepoMap {
            root: canon.display().to_string(),
            entries: rel,
            truncated,
            errors,
            dirs_scanned,
        })
    }

    /// The map as compact text for the model.
    pub fn render(&self) -> String {
        let mut out = format!("# repo map: {}\n", self.root);
        for e in &self.entries {
            out.push_str(e);
            out.push('\n');
        }
        if self.truncated {
            out.push_str("[truncated]\n");
        }
        for err in &self.errors {
            out.push_str(&format!("[warn] {err}\n"));
        }
        out
    }

    pub fn entries(&self) -> &[String] {
        &self.entries
    }

    pub fn truncated(&self) -> bool {
        self.truncated
    }

    /// Traversal errors that were encountered and surfaced.
    pub fn errors(&self) -> &[String] {
        &self.errors
    }

    /// How many directories the traversal opened before stopping.
    pub fn dirs_scanned(&self) -> usize {
        self.dirs_scanned
    }
}

fn walk(
    dir: &Path,
    depth: usize,
    opts: &RepoMapOptions,
    entries: &mut Vec<PathBuf>,
    errors: &mut Vec<String>,
    dirs_scanned: &mut usize,
    truncated: &mut bool,
) -> Result<(), ContextError> {
    // Stop descending once the entry cap is met: the traversal itself is
    // bounded, not just the final result.
    if depth > opts.max_depth {
        return Ok(());
    }
    if entries.len() >= opts.max_entries {
        // The cap was already met before entering this directory, so its
        // contents were not mapped: truncated.
        *truncated = true;
        return Ok(());
    }
    *dirs_scanned += 1;
    let rd = std::fs::read_dir(dir).map_err(|e| ContextError::Io(e.to_string()))?;
    // Visit children in sorted (by name) order so that which entries survive
    // the cap is deterministic across filesystems, not dependent on the
    // arbitrary read_dir enumeration order.
    let mut children: Vec<std::fs::DirEntry> = Vec::new();
    for entry in rd {
        match entry {
            Ok(e) => children.push(e),
            Err(e) => errors.push(format!("{}: {e}", dir.display())),
        }
    }
    children.sort_by_key(|e| e.file_name());
    for entry in children {
        if entries.len() >= opts.max_entries {
            // Eligible entries remain but the cap is met: the map is
            // truncated. Set only when we actually stop early — not when the
            // repo has exactly the cap, or when the cap is zero.
            *truncated = true;
            break;
        }
        let ft = match entry.file_type() {
            Ok(ft) => ft,
            Err(e) => {
                errors.push(format!("{}: {e}", entry.path().display()));
                continue;
            }
        };
        let path = entry.path();
        if ft.is_dir() {
            // file_type() does not follow symlinks, so a symlinked dir is not
            // descended into. Skip well-known noise directories.
            let name = entry.file_name().to_string_lossy().to_string();
            if SKIP_DIRS.contains(&name.as_str()) {
                continue;
            }
            entries.push(path.clone());
            walk(
                &path,
                depth + 1,
                opts,
                entries,
                errors,
                dirs_scanned,
                truncated,
            )?;
        } else {
            entries.push(path);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU32, Ordering};

    use super::*;

    fn temp_dir() -> PathBuf {
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("agent_code_repopm_{}_{seq}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn opts(depth: usize, entries: usize) -> RepoMapOptions {
        RepoMapOptions::new(depth, entries)
    }

    #[test]
    fn map_is_bounded_and_deterministic() {
        let dir = temp_dir();
        std::fs::create_dir_all(dir.join("src/deep")).unwrap();
        std::fs::write(dir.join("main.rs"), "fn main() {}").unwrap();
        std::fs::write(dir.join("src/a.rs"), "a").unwrap();
        std::fs::write(dir.join("src/deep/b.rs"), "b").unwrap();

        let map = RepoMap::from_path(&dir, &opts(1, 100)).unwrap();
        // Depth 1 excludes the nested src/deep/b.rs.
        assert!(map.entries().iter().any(|e| e == "main.rs"));
        assert!(map.entries().iter().any(|e| e == "src/a.rs"));
        assert!(!map.entries().iter().any(|e| e == "src/deep/b.rs"));
        // Deterministic: sorted, and a second call agrees.
        let again = RepoMap::from_path(&dir, &opts(1, 100)).unwrap();
        assert_eq!(map.entries(), again.entries());
        let mut sorted = map.entries().to_vec();
        sorted.sort();
        assert_eq!(map.entries(), &sorted);

        // Entry cap truncates and flags it.
        let capped = RepoMap::from_path(&dir, &opts(5, 2)).unwrap();
        assert_eq!(capped.entries().len(), 2);
        assert!(capped.truncated());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn map_skips_noise_directories() {
        let dir = temp_dir();
        std::fs::create_dir_all(dir.join(".git")).unwrap();
        std::fs::write(dir.join(".git/config"), "x").unwrap();
        std::fs::create_dir_all(dir.join("target")).unwrap();
        std::fs::write(dir.join("target/out.bin"), "x").unwrap();
        std::fs::write(dir.join("keep.rs"), "k").unwrap();

        let map = RepoMap::from_path(&dir, &opts(5, 100)).unwrap();
        assert!(!map.entries().iter().any(|e| e.contains(".git")));
        assert!(!map.entries().iter().any(|e| e.contains("target")));
        assert!(map.entries().iter().any(|e| e == "keep.rs"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn map_does_not_follow_symlinks() {
        let dir = temp_dir();
        let outside = temp_dir();
        std::fs::create_dir_all(outside.join("secret")).unwrap();
        std::fs::write(outside.join("secret/top.txt"), "x").unwrap();
        // A symlink pointing at the outside tree must not be descended into.
        #[cfg(unix)]
        std::os::unix::fs::symlink(&outside, dir.join("link")).unwrap();
        std::fs::write(dir.join("real.rs"), "r").unwrap();

        let map = RepoMap::from_path(&dir, &opts(5, 100)).unwrap();
        assert!(!map.entries().iter().any(|e| e.contains("top.txt")));
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_dir_all(&outside);
    }

    #[test]
    fn map_missing_root_is_an_error() {
        let err = RepoMap::from_path(
            &std::path::PathBuf::from("/nonexistent/agent_code_xyz"),
            &opts(1, 10),
        );
        assert!(matches!(err, Err(ContextError::Io(_))));
    }

    #[test]
    fn map_read_root_must_be_a_directory() {
        let dir = temp_dir();
        let file = dir.join("afile");
        std::fs::write(&file, "x").unwrap();
        // read_dir on a plain file fails; the error is surfaced, not ignored.
        let err = RepoMap::from_path(&file, &opts(1, 10));
        assert!(matches!(err, Err(ContextError::Io(_))));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn traversal_stops_at_the_entry_cap() {
        // A tree far larger than the cap: many subdirs, each with files. The
        // traversal must stop opening directories once the cap is met, not
        // scan the whole tree and truncate afterwards.
        let dir = temp_dir();
        const SUBDIRS: usize = 40;
        for i in 0..SUBDIRS {
            let sub = dir.join(format!("d{i:02}"));
            std::fs::create_dir_all(&sub).unwrap();
            for j in 0..3 {
                std::fs::write(sub.join(format!("f{j}.txt")), "x").unwrap();
            }
        }
        let cap = 15;
        let map = RepoMap::from_path(&dir, &opts(5, cap)).unwrap();
        assert_eq!(map.entries().len(), cap);
        assert!(map.truncated());
        // Only a handful of the 40 subdirs were opened before the cap was hit;
        // a full scan would open all 40 (plus root).
        assert!(map.dirs_scanned() < 20);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn result_set_is_deterministic_regardless_of_creation_order() {
        // Create files in reverse alphabetical order; the map must still
        // select the alphabetically-first entries (deterministic), not the
        // filesystem's enumeration order.
        let dir = temp_dir();
        for c in "abcdefghijklmnopqrstuvwxyz".chars().rev() {
            std::fs::write(dir.join(format!("{c}.txt")), "x").unwrap();
        }
        let map = RepoMap::from_path(&dir, &opts(5, 5)).unwrap();
        // The five alphabetically-smallest files, in sorted order.
        assert_eq!(
            map.entries(),
            &["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
        );
        assert!(map.truncated());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn exact_cap_is_not_flagged_truncated() {
        // A repo with exactly `cap` entries is complete, not truncated.
        let dir = temp_dir();
        for c in ['a', 'b', 'c'] {
            std::fs::write(dir.join(format!("{c}.txt")), "x").unwrap();
        }
        let map = RepoMap::from_path(&dir, &opts(5, 3)).unwrap();
        assert_eq!(map.entries().len(), 3);
        assert!(!map.truncated());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
