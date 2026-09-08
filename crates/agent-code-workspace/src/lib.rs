//! Project rules, Git worktree/checkpoint, path handling, diff and rollback.
//!
//! R1 provides the checkpoint shape only; the Git integration lands in R2.

/// A Git checkpoint captured before a write-bearing batch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Checkpoint {
    pub git_head: String,
    pub dirty: bool,
}
