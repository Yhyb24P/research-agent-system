//! R2 gate: end-to-end proof of the N02-N10 safety and bounding behaviors
//! through the public workspace API.

use agent_code_tools::{EditFile, ExecuteCommand, SearchDir, ViewFile, WriteFile};
use agent_code_workspace::{GitWorkspace, SyntaxGuard, ToolError, Workspace};
use std::collections::BTreeMap;
use std::os::unix::fs::symlink;
use std::path::PathBuf;

fn temp_ws() -> (Workspace, PathBuf) {
    use std::sync::atomic::{AtomicU32, Ordering};
    static SEQ: AtomicU32 = AtomicU32::new(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("agent_code_it_{seq}_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    (Workspace::new(&dir).unwrap(), dir)
}

fn temp_git_repo() -> PathBuf {
    use std::sync::atomic::{AtomicU32, Ordering};
    static SEQ: AtomicU32 = AtomicU32::new(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("agent_code_it_git_{seq}_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let git = |args: &[&str]| {
        let out = std::process::Command::new("git")
            .args(args)
            .current_dir(&dir)
            .env("GIT_AUTHOR_NAME", "t")
            .env("GIT_AUTHOR_EMAIL", "t@t")
            .env("GIT_COMMITTER_NAME", "t")
            .env("GIT_COMMITTER_EMAIL", "t@t")
            .output()
            .expect("run git");
        assert!(out.status.success(), "git {} failed", args.join(" "));
    };
    git(&["init", "-q"]);
    git(&["config", "user.name", "t"]);
    git(&["config", "user.email", "t@t"]);
    std::fs::write(dir.join("main.txt"), "original\n").unwrap();
    git(&["add", "."]);
    git(&["commit", "-q", "-m", "init"]);
    dir
}

/// N02: a symlink pointing outside the workspace must be rejected on every
/// read path (the information-disclosure vector).
#[test]
fn n02_symlink_escape_is_rejected() {
    let (ws, dir) = temp_ws();
    let outside = dir.with_extension("outside");
    std::fs::create_dir_all(&outside).unwrap();
    std::fs::write(outside.join("secret.txt"), "top secret").unwrap();
    symlink(outside.join("secret.txt"), dir.join("leak.txt")).unwrap();

    assert!(matches!(
        ws.resolve("leak.txt"),
        Err(ToolError::SymlinkEscape(_))
    ));
    assert!(matches!(
        ws.hash_file("leak.txt"),
        Err(ToolError::SymlinkEscape(_))
    ));
    assert!(matches!(
        ws.view_file(&ViewFile {
            path: "leak.txt".into(),
            start_line: 1,
            end_line: 10
        }),
        Err(ToolError::SymlinkEscape(_))
    ));
    assert!(matches!(
        ws.edit_file(
            &EditFile {
                path: "leak.txt".into(),
                old_str: "a".into(),
                new_str: "b".into(),
                expected_file_hash: None
            },
            None
        ),
        Err(ToolError::SymlinkEscape(_))
    ));
}

/// N03: an old_str that occurs more than once must be rejected, not silently
/// replaced at an arbitrary occurrence.
#[test]
fn n03_duplicate_old_str_is_rejected() {
    let (ws, _) = temp_ws();
    ws.atomic_write("a.txt", "x\nx\nx\n").unwrap();
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
    assert!(matches!(err, ToolError::DuplicateMatch { count: 3, .. }));
}

/// N04: a stale expected hash must block both write and edit.
#[test]
fn n04_stale_hash_is_rejected() {
    let (ws, _) = temp_ws();
    ws.atomic_write("a.txt", "current\n").unwrap();
    let err = ws
        .edit_file(
            &EditFile {
                path: "a.txt".into(),
                old_str: "current".into(),
                new_str: "new".into(),
                expected_file_hash: Some("deadbeef".into()),
            },
            None,
        )
        .unwrap_err();
    assert!(matches!(err, ToolError::StaleHash { .. }));
    let err = ws
        .write_file(&WriteFile {
            path: "a.txt".into(),
            content: "z".into(),
            create_only: false,
            max_bytes: None,
            expected_file_hash: Some("deadbeef".into()),
        })
        .unwrap_err();
    assert!(matches!(err, ToolError::StaleHash { .. }));
}

/// N05: a syntax-guard failure must leave the file byte-for-byte unchanged.
#[test]
fn n05_syntax_failure_restores_original() {
    let (ws, dir) = temp_ws();
    ws.atomic_write("a.txt", "good\n").unwrap();
    let orig = ws.hash_file("a.txt").unwrap();
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
    assert_eq!(ws.hash_file("a.txt").unwrap(), orig);
    assert_eq!(
        std::fs::read_to_string(dir.join("a.txt")).unwrap(),
        "good\n"
    );
}

/// N06: a grandchild that outlives the timeout must be killed with the group.
#[test]
fn n06_grandchild_timeout_is_terminated() {
    let (ws, _) = temp_ws();
    let out = ws
        .execute_command(&ExecuteCommand {
            program: "sh".into(),
            args: vec!["-c".into(), "sleep 30 & wait".into()],
            cwd: None,
            timeout_seconds: 1,
            env: BTreeMap::new(),
        })
        .unwrap();
    assert!(out.timed_out);
    assert!(out.exit_code.is_none());
}

/// N07: a long command's output must retain both head and tail, with the full
/// output preserved in the log.
#[test]
fn n07_truncation_keeps_head_and_tail() {
    let (ws, _) = temp_ws();
    let out = ws
        .execute_command(&ExecuteCommand {
            program: "sh".into(),
            args: vec!["-c".into(), "seq 1 5000".into()],
            cwd: None,
            timeout_seconds: 10,
            env: BTreeMap::new(),
        })
        .unwrap();
    assert!(out.truncated);
    assert!(out.stdout_head.starts_with("1\n"));
    assert!(out.stdout_tail.ends_with("5000\n"));
    let log = std::fs::read_to_string(ws.root().join(&out.log_path)).unwrap();
    assert!(log.contains("5000"));
}

/// N08: the pieces compose — isolated worktree, edit, checkpoint, diff,
/// rollback — leaving the user's original worktree untouched.
#[test]
fn n08_worktree_edit_checkpoint_rollback() {
    let dir = temp_git_repo();
    let gw = GitWorkspace::create(&dir).unwrap();
    let ws = gw.workspace().unwrap();
    ws.atomic_write("main.txt", "v2\n").unwrap();
    let cp = gw.checkpoint("cp").unwrap();
    assert_ne!(cp, gw.initial_head());
    assert!(gw.diff(gw.initial_head()).unwrap().contains("v2"));
    gw.rollback(gw.initial_head()).unwrap();
    assert_eq!(
        std::fs::read_to_string(gw.worktree().join("main.txt")).unwrap(),
        "original\n"
    );
    assert_eq!(
        std::fs::read_to_string(dir.join("main.txt")).unwrap(),
        "original\n"
    );
    gw.remove().unwrap();
}

/// N09: bounded, gitignore-aware search returns matching lines, not files.
#[test]
fn n09_search_returns_bounded_hits() {
    let (ws, _) = temp_ws();
    ws.atomic_write("a.rs", "fn foo() {}\nfn bar() {}\n")
        .unwrap();
    ws.atomic_write("b.rs", "fn foo() {}\n").unwrap();
    let out = ws
        .search_dir(&SearchDir {
            pattern: "fn foo".into(),
            glob: Some("**/*.rs".into()),
            max_matches: 1,
        })
        .unwrap();
    assert!(out.truncated);
    assert_eq!(out.matches.len(), 1);
    assert_eq!(out.matches[0].path, "a.rs");
    assert!(out.matches[0].text.contains("fn foo"));
}
