//! The five atomic tool contracts for the native Coding Agent.
//!
//! R1 defines the request/result shapes only. The concrete implementations
//! (filesystem access, command execution, search) land in R2.

/// Request for `view_file`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ViewFile {
    pub path: String,
    pub start_line: u32,
    pub end_line: u32,
}

/// Request for `edit_file`. Exact unique match with an optional expected hash.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EditFile {
    pub path: String,
    pub old_str: String,
    pub new_str: String,
    pub expected_file_hash: Option<String>,
}

/// Request for `write_file`. New files or explicit short-file replacement.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteFile {
    pub path: String,
    pub content: String,
    pub create_only: bool,
    pub max_bytes: Option<u64>,
}

/// Request for `search_dir`. Bounded ripgrep-style traversal.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SearchDir {
    pub pattern: String,
    pub glob: Option<String>,
    pub max_matches: u32,
}

/// Request for `execute_command`. Structured argv by default.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecuteCommand {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: Option<String>,
    pub timeout_seconds: u64,
}

/// The bounded result of any tool call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolResult {
    pub ok: bool,
    pub head: String,
    pub tail: String,
    pub truncated: bool,
}
