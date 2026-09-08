//! R2 gate: end-to-end proof of the N02-N10 acceptance matrix through the
//! public workspace API.

use agent_code_tools::{EditFile, ExecuteCommand, SearchDir, ViewFile, WriteFile};
use agent_code_workspace::{GitWorkspace, PostWriteGuard, ToolError, Workspace, MAX_VIEW_LINES};
use std::collections::BTreeMap;
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

/// N02: an isolated worktree lets the agent edit freely while the user's
/// original worktree stays untouched; checkpoint/rollback round-trips.
#[test]
fn n02_worktree_isolation() {
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
    // The user's original worktree was never touched.
    assert_eq!(
        std::fs::read_to_string(dir.join("main.txt")).unwrap(),
        "original\n"
    );
    gw.remove().unwrap();
}

/// N03: view_file returns bounded, numbered lines plus a whole-file hash.
#[test]
fn n03_view_bounded() {
    let (ws, _) = temp_ws();
    let body: String = (1..=500)
        .map(|i| format!("line {i}"))
        .collect::<Vec<_>>()
        .join("\n");
    ws.atomic_write("big.txt", &body).unwrap();
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

/// N04: edit_file applies a unique match (reporting before/after/range) and
/// rejects duplicate or missing old_str.
#[test]
fn n04_edit_exact_unique() {
    let (ws, dir) = temp_ws();
    ws.atomic_write("a.txt", "hello world\n").unwrap();
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

    ws.atomic_write("dup.txt", "x\nx\n").unwrap();
    let err = ws
        .edit_file(
            &EditFile {
                path: "dup.txt".into(),
                old_str: "x".into(),
                new_str: "y".into(),
                expected_file_hash: None,
            },
            None,
            None,
        )
        .unwrap_err();
    assert!(matches!(err, ToolError::DuplicateMatch { count: 2, .. }));

    let err = ws
        .edit_file(
            &EditFile {
                path: "a.txt".into(),
                old_str: "absent".into(),
                new_str: "x".into(),
                expected_file_hash: None,
            },
            None,
            None,
        )
        .unwrap_err();
    assert!(matches!(err, ToolError::MatchNotFound(_)));
}

/// N05: a post-write syntax-guard failure atomically restores the original
/// bytes (before/after hashes agree after the restore).
#[test]
fn n05_syntax_rollback() {
    let (ws, dir) = temp_ws();
    ws.atomic_write("a.txt", "good\n").unwrap();
    let before = ws.hash_file("a.txt").unwrap();
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
    assert_eq!(ws.hash_file("a.txt").unwrap(), before);
    assert_eq!(
        std::fs::read_to_string(dir.join("a.txt")).unwrap(),
        "good\n"
    );
}

/// N06: write_file enforces create-only, stale-hash, and the default hard cap.
#[test]
fn n06_write_guards() {
    let (ws, _) = temp_ws();
    ws.atomic_write("a.txt", "current\n").unwrap();

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

    let err = ws
        .write_file(&WriteFile {
            path: "a.txt".into(),
            content: "new".into(),
            create_only: false,
            max_bytes: None,
            expected_file_hash: Some("stale".into()),
        })
        .unwrap_err();
    assert!(matches!(err, ToolError::StaleHash { .. }));

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

/// N07: search_dir returns bounded matching lines; max_matches == 0 is empty.
#[test]
fn n07_search_bounded() {
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

    let empty = ws
        .search_dir(&SearchDir {
            pattern: "fn foo".into(),
            glob: None,
            max_matches: 0,
        })
        .unwrap();
    assert!(empty.matches.is_empty());
    assert!(!empty.truncated);
}

/// N08: commands run as argv with no shell, so metacharacters are passed
/// verbatim rather than expanded.
#[test]
fn n08_structured_command_argv() {
    let (ws, _) = temp_ws();
    let out = ws
        .execute_command(&ExecuteCommand {
            program: "printf".into(),
            args: vec![
                "%s\n".into(),
                "a*b".into(),
                "$(echo hi)".into(),
                "x y".into(),
            ],
            cwd: None,
            timeout_seconds: 10,
            env: BTreeMap::new(),
        })
        .unwrap();
    assert_eq!(out.exit_code, Some(0));
    assert!(out.stdout_head.contains("a*b"));
    assert!(out.stdout_head.contains("$(echo hi)"));
    assert!(out.stdout_head.contains("x y"));
}

/// N09: a grandchild that outlives the timeout is killed with the group.
#[test]
fn n09_process_group_timeout() {
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

/// N10: long output is truncated to head+tail in memory, with the full output
/// preserved in the log.
#[test]
fn n10_output_truncation() {
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
    assert!(out.stdout_total > 4000);
    let log = std::fs::read_to_string(ws.root().join(&out.stdout_log)).unwrap();
    assert!(log.contains("5000"));
}
