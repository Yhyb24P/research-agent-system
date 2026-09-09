use std::path::{Path, PathBuf};

use crate::error::ToolError;
use crate::workspace::Workspace;

/// An isolated Git worktree the agent operates in, so the user's original
/// worktree is never touched.
#[derive(Debug, Clone)]
pub struct GitWorkspace {
    worktree: PathBuf,
    repo_root: PathBuf,
    initial_head: String,
    initial_dirty: bool,
}

impl GitWorkspace {
    /// Create a detached worktree for `repo_root` under a temp dir and record
    /// the repo's initial HEAD and dirty state.
    pub fn create(repo_root: impl AsRef<Path>) -> Result<Self, ToolError> {
        let toplevel = PathBuf::from(
            run_git(repo_root.as_ref(), &["rev-parse", "--show-toplevel"])?
                .trim()
                .to_string(),
        );
        let initial_head = run_git(&toplevel, &["rev-parse", "HEAD"])?
            .trim()
            .to_string();
        let initial_dirty = !run_git(&toplevel, &["status", "--porcelain"])?
            .trim()
            .is_empty();

        use std::sync::atomic::{AtomicU32, Ordering};
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let parent =
            std::env::temp_dir().join(format!("agent_code_wt_{}_{seq}", std::process::id()));
        std::fs::create_dir_all(&parent).map_err(|e| ToolError::Io(e.to_string()))?;
        let worktree = parent.join("worktree");
        run_git(
            &toplevel,
            &[
                "worktree",
                "add",
                "--detach",
                &worktree.to_string_lossy(),
                "HEAD",
            ],
        )?;

        Ok(Self {
            worktree,
            repo_root: toplevel,
            initial_head,
            initial_dirty,
        })
    }

    /// The isolated worktree path (root the agent's tools to this).
    pub fn worktree(&self) -> &Path {
        &self.worktree
    }

    /// A contained workspace rooted at the isolated worktree.
    pub fn workspace(&self) -> Result<Workspace, ToolError> {
        Workspace::new(&self.worktree)
    }

    /// The repo HEAD before the worktree was created.
    pub fn initial_head(&self) -> &str {
        &self.initial_head
    }

    /// Whether the repo had uncommitted changes before the worktree was created.
    pub fn initial_dirty(&self) -> bool {
        self.initial_dirty
    }

    /// Commit the current worktree state as a checkpoint and return its SHA.
    /// A clean worktree returns the current HEAD unchanged.
    pub fn checkpoint(&self, message: &str) -> Result<String, ToolError> {
        run_git(&self.worktree, &["add", "-A"])?;
        let dirty = !run_git(&self.worktree, &["status", "--porcelain"])?
            .trim()
            .is_empty();
        if dirty {
            run_git(
                &self.worktree,
                &["commit", "--no-verify", "--allow-empty", "-m", message],
            )?;
        }
        Ok(run_git(&self.worktree, &["rev-parse", "HEAD"])?
            .trim()
            .to_string())
    }

    /// Unified diff from `since` to the current worktree, including new files.
    pub fn diff(&self, since: &str) -> Result<String, ToolError> {
        run_git(&self.worktree, &["add", "-A"])?;
        run_git(&self.worktree, &["diff", "--cached", since])
    }

    /// Paths changed (added/modified/deleted) from `since` to the current
    /// worktree, sorted. Includes new files.
    pub fn changed_files(&self, since: &str) -> Result<Vec<String>, ToolError> {
        run_git(&self.worktree, &["add", "-A"])?;
        let out = run_git(&self.worktree, &["diff", "--cached", "--name-only", since])?;
        Ok(out
            .lines()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .map(str::to_string)
            .collect())
    }

    /// Roll the worktree back to `sha`: hard-reset tracked files and remove
    /// untracked ones.
    pub fn rollback(&self, sha: &str) -> Result<(), ToolError> {
        run_git(&self.worktree, &["reset", "--hard", sha])?;
        run_git(&self.worktree, &["clean", "-fd"])?;
        Ok(())
    }

    /// Remove the isolated worktree.
    pub fn remove(&self) -> Result<(), ToolError> {
        run_git(
            &self.repo_root,
            &[
                "worktree",
                "remove",
                "--force",
                &self.worktree.to_string_lossy(),
            ],
        )?;
        Ok(())
    }
}

/// Run `git <args>` in `dir`, returning stdout; error on a non-zero exit.
fn run_git(dir: &Path, args: &[&str]) -> Result<String, ToolError> {
    let out = std::process::Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .map_err(|e| ToolError::Io(format!("run git: {e}")))?;
    if !out.status.success() {
        return Err(ToolError::Io(format!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr)
        )));
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_git_repo() -> PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("agent_code_git_{}_{seq}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let git = |args: &[&str]| {
            std::process::Command::new("git")
                .args(args)
                .current_dir(&dir)
                .env("GIT_AUTHOR_NAME", "t")
                .env("GIT_AUTHOR_EMAIL", "t@t")
                .env("GIT_COMMITTER_NAME", "t")
                .env("GIT_COMMITTER_EMAIL", "t@t")
                .output()
                .expect("run git")
        };
        git(&["init", "-q"]);
        git(&["config", "user.name", "t"]);
        git(&["config", "user.email", "t@t"]);
        std::fs::write(dir.join("main.txt"), "original\n").unwrap();
        git(&["add", "."]);
        git(&["commit", "-q", "-m", "init"]);
        dir
    }

    #[test]
    fn original_worktree_is_untouched() {
        let dir = temp_git_repo();
        let gw = GitWorkspace::create(&dir).unwrap();
        gw.workspace()
            .unwrap()
            .atomic_write("main.txt", "mutated\n")
            .unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.join("main.txt")).unwrap(),
            "original\n"
        );
        gw.remove().unwrap();
    }

    #[test]
    fn checkpoint_diff_rollback() {
        let dir = temp_git_repo();
        let gw = GitWorkspace::create(&dir).unwrap();
        assert!(!gw.initial_dirty());

        gw.workspace()
            .unwrap()
            .atomic_write("main.txt", "changed\n")
            .unwrap();
        let cp = gw.checkpoint("cp1").unwrap();
        assert_ne!(cp, gw.initial_head());

        let d = gw.diff(gw.initial_head()).unwrap();
        assert!(d.contains("changed"));

        gw.rollback(gw.initial_head()).unwrap();
        assert_eq!(
            std::fs::read_to_string(gw.worktree().join("main.txt")).unwrap(),
            "original\n"
        );
        gw.remove().unwrap();
    }
}
