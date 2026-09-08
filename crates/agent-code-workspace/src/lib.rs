//! Workspace containment, bounded file/search tools, and Git checkpoints.

mod error;
mod exec;
mod tools;
mod workspace;
mod write;

pub use error::ToolError;
pub use exec::CommandOutput;
pub use tools::{SearchHit, SearchOutput, ViewOutput, MAX_VIEW_LINES};
pub use workspace::Workspace;
pub use write::{EditOutput, SyntaxGuard, WriteOutput};

/// A Git checkpoint for rollback.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Checkpoint {
    pub git_head: String,
    pub dirty: bool,
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::symlink;

    use super::*;

    fn tmpdir(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("agent_code_ws_{name}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn rejects_absolute_paths() {
        let ws = Workspace::new(tmpdir("abs")).unwrap();
        assert!(matches!(
            ws.resolve("/etc/passwd"),
            Err(ToolError::PathEscape(_))
        ));
    }

    #[test]
    fn rejects_dotdot_escape() {
        let ws = Workspace::new(tmpdir("dd")).unwrap();
        assert!(matches!(
            ws.resolve("../outside"),
            Err(ToolError::PathEscape(_))
        ));
        // A path that rises above root then descends back is still rejected.
        assert!(matches!(
            ws.resolve("a/../../outside"),
            Err(ToolError::PathEscape(_))
        ));
    }

    #[test]
    fn rejects_symlink_escape() {
        let ws_dir = tmpdir("sym");
        let outside = ws_dir.with_extension("out");
        std::fs::create_dir_all(&outside).unwrap();
        let outside_file = outside.join("secret.txt");
        std::fs::write(&outside_file, "secret").unwrap();
        // A symlink inside the workspace pointing outside.
        let link = ws_dir.join("link.txt");
        symlink(&outside_file, &link).unwrap();
        let ws = Workspace::new(&ws_dir).unwrap();
        assert!(matches!(
            ws.resolve("link.txt"),
            Err(ToolError::SymlinkEscape(_))
        ));
    }

    #[test]
    fn resolves_and_hashes_contained_file() {
        let dir = tmpdir("hash");
        std::fs::write(dir.join("a.txt"), "hello").unwrap();
        let ws = Workspace::new(&dir).unwrap();
        assert!(ws.resolve("a.txt").is_ok());
        assert_eq!(ws.hash_file("a.txt").unwrap().len(), 64);
    }

    #[test]
    fn atomic_write_creates_file() {
        let dir = tmpdir("aw");
        let ws = Workspace::new(&dir).unwrap();
        ws.atomic_write("new.txt", "data").unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.join("new.txt")).unwrap(),
            "data"
        );
    }
}
