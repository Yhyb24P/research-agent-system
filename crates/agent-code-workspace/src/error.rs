use std::fmt;

/// The unified error type for the tool and workspace layer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolError {
    /// The resolved path escapes the workspace root.
    PathEscape(String),
    /// A symlink points outside the workspace root.
    SymlinkEscape(String),
    /// The file does not exist.
    NotFound(String),
    /// The file already exists (create-only write).
    AlreadyExists(String),
    /// The content exceeds the size limit.
    TooLarge { path: String, limit: u64 },
    /// The expected file hash did not match.
    StaleHash {
        path: String,
        expected: String,
        actual: String,
    },
    /// The old string matched more than once.
    DuplicateMatch { path: String, count: usize },
    /// The old string was not found.
    MatchNotFound(String),
    /// The syntax guard rejected the edit.
    SyntaxGuardFailed(String),
    /// The command timed out.
    Timeout { program: String, seconds: u64 },
    /// The command exited non-zero.
    NonZeroExit { program: String, code: i32 },
    /// An I/O or subprocess failure.
    Io(String),
}

impl fmt::Display for ToolError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::PathEscape(p) => write!(f, "path escapes the workspace: {p}"),
            Self::SymlinkEscape(p) => write!(f, "symlink escapes the workspace: {p}"),
            Self::NotFound(p) => write!(f, "file not found: {p}"),
            Self::AlreadyExists(p) => write!(f, "file already exists: {p}"),
            Self::TooLarge { path, limit } => {
                write!(f, "file too large ({path}): limit {limit} bytes")
            }
            Self::StaleHash {
                path,
                expected,
                actual,
            } => {
                write!(
                    f,
                    "stale hash for {path}: expected {expected}, got {actual}"
                )
            }
            Self::DuplicateMatch { path, count } => {
                write!(f, "match not unique in {path}: {count} occurrences")
            }
            Self::MatchNotFound(p) => write!(f, "old string not found in {p}"),
            Self::SyntaxGuardFailed(msg) => write!(f, "syntax guard rejected the edit: {msg}"),
            Self::Timeout { program, seconds } => write!(f, "{program} timed out after {seconds}s"),
            Self::NonZeroExit { program, code } => write!(f, "{program} exited with code {code}"),
            Self::Io(msg) => write!(f, "io error: {msg}"),
        }
    }
}

impl std::error::Error for ToolError {}
