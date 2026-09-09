//! End-to-end proof of native self-correction. One Coding Agent inspects a
//! real Git repo, makes a wrong edit, runs a check that fails, sees that
//! failure in its next bounded context, corrects the edit, and delivers — all
//! recorded durably in SQLite, with the original worktree left untouched.

use std::rc::Rc;

use agent_code_context::{ContextBudget, ContextError, HistorySink, HistorySource};
use agent_code_core::{AgentState, Journal, Session, SessionId};
use agent_code_model::{ModelClient, ModelContext, ModelDecision, ModelError, Observation};
use agent_code_runtime::{AgentConfig, AgentLoop};
use agent_code_storage::SqliteJournal;
use agent_code_tools::{EditFile, ExecuteCommand, ToolRequest, ViewFile};
use agent_code_workspace::GitWorkspace;
use async_trait::async_trait;
use rusqlite::Connection;

/// One SQLite connection shared as both the observation source and the sink.
struct SharedStore(Rc<SqliteJournal>);
impl HistorySource for SharedStore {
    fn observations(&self, s: &SessionId) -> Result<Vec<Observation>, ContextError> {
        self.0.observations(s)
    }
}
impl HistorySink for SharedStore {
    fn append(&self, s: &SessionId, o: &Observation) -> Result<(), ContextError> {
        self.0.append(s, o)
    }
}

/// A deterministic model that reacts only to what is actually in its bounded
/// context. It issues the corrective edit only after it has observed the
/// failed check — which is exactly what proves a failure result enters the
/// next model turn.
struct SelfCorrectingModel;
#[async_trait]
impl ModelClient for SelfCorrectingModel {
    async fn decide(&self, ctx: &ModelContext) -> Result<ModelDecision, ModelError> {
        let obs = &ctx.observations;
        let views = obs
            .iter()
            .filter(|s| s.starts_with("view answer.txt"))
            .count();
        let edits = obs
            .iter()
            .filter(|s| s.starts_with("file answer.txt"))
            .count();
        let check_failed = obs.iter().any(|s| s.contains("sh failed"));
        let edit = |old: &str, new: &str| {
            ModelDecision::ToolCall(ToolRequest::EditFile(EditFile {
                path: "answer.txt".into(),
                old_str: old.into(),
                new_str: new.into(),
                expected_file_hash: None,
            }))
        };
        Ok(match (views, edits, check_failed) {
            (0, _, _) => ModelDecision::ToolCall(ToolRequest::ViewFile(ViewFile {
                path: "answer.txt".into(),
                start_line: 1,
                end_line: 10,
            })),
            (1, 0, false) => edit("2", "3"),
            (1, 1, false) => ModelDecision::Final("first attempt".into()),
            (1, 1, true) => edit("3", "4"),
            (1, 2, _) => ModelDecision::Final("fixed".into()),
            _ => ModelDecision::Final("stuck".into()),
        })
    }
}

/// A repo whose `answer.txt` holds the wrong value; the check passes only
/// once the agent edits it to `4`.
fn temp_answer_repo() -> std::path::PathBuf {
    use std::sync::atomic::{AtomicU32, Ordering};
    static SEQ: AtomicU32 = AtomicU32::new(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    let dir =
        std::env::temp_dir().join(format!("agent_code_e2e_repo_{}_{seq}", std::process::id()));
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
    std::fs::write(dir.join("answer.txt"), "2\n").unwrap();
    git(&["add", "."]);
    git(&["commit", "-q", "-m", "init"]);
    dir
}

/// The check: passes only when `answer.txt` is `4`.
fn answer_check() -> ExecuteCommand {
    ExecuteCommand {
        program: "sh".into(),
        args: vec!["-c".into(), "test \"$(cat answer.txt)\" = \"4\"".into()],
        cwd: None,
        timeout_seconds: 10,
        env: std::collections::BTreeMap::new(),
    }
}

#[tokio::test]
async fn self_correction_is_durable_and_keeps_the_original_worktree() {
    let repo = temp_answer_repo();
    let db = std::env::temp_dir().join(format!("agent_code_e2e_{}.db", std::process::id()));
    let _ = std::fs::remove_file(&db);

    // Two connections to the same DB file: one for the session journal, one
    // shared as the observation source and sink, both bound to the same session.
    let sid = SessionId::new("e2e");
    let j_sess = SqliteJournal::open(Connection::open(&db).unwrap(), sid.clone()).unwrap();
    let j_io = SqliteJournal::open(Connection::open(&db).unwrap(), sid.clone()).unwrap();
    let session = Session::create(j_sess).unwrap();
    let io = Rc::new(j_io);

    let git = GitWorkspace::create(&repo).unwrap();
    let cfg = AgentConfig {
        task: "make the check pass".into(),
        project_rules: String::new(),
        repository_map: String::new(),
        budget: ContextBudget::new(2000, 200),
        max_rounds: 8,
        verify: Some(answer_check()),
    };
    let mut loop_ = AgentLoop::new(
        session,
        Box::new(SelfCorrectingModel),
        git.clone(),
        Box::new(SharedStore(Rc::clone(&io))),
        Box::new(SharedStore(io)),
        cfg,
        sid.clone(),
    )
    .unwrap();

    let delivery = loop_.run().await.unwrap();

    // The agent self-corrected and delivered the corrected answer.
    assert_eq!(delivery.summary, "fixed");
    assert!(delivery.checks_run.iter().any(|c| c.contains("sh -c")));
    assert!(delivery.diff.contains("4"));
    assert!(delivery.changed_files.iter().any(|f| f == "answer.txt"));

    // The original worktree was never touched.
    assert_eq!(
        std::fs::read_to_string(repo.join("answer.txt")).unwrap(),
        "2\n"
    );

    // Reopen the same DB file: the full durable record is still there.
    let j = SqliteJournal::open(Connection::open(&db).unwrap(), sid.clone()).unwrap();
    assert_eq!(j.current_state().unwrap(), Some(AgentState::Completed));
    assert_eq!(j.highest_call_id().unwrap(), Some(3)); // view + the two edits
    let obs = j.read_observations().unwrap();

    // The failed check is durably recorded, and only the first edit precedes it.
    let fail_idx = obs
        .iter()
        .position(|o| matches!(o, Observation::Error { signature, .. } if signature.contains("sh failed")))
        .expect("the failed check must be recorded");
    let edits_before = obs[..fail_idx]
        .iter()
        .filter(|o| matches!(o, Observation::File { .. }))
        .count();
    assert_eq!(
        edits_before, 1,
        "only the first edit may precede the failed check: {obs:?}"
    );
    assert_eq!(
        obs.iter()
            .filter(|o| matches!(o, Observation::File { .. }))
            .count(),
        2,
        "the corrective edit follows the failure: {obs:?}"
    );

    // The state machine walked the verify -> observe loop-back.
    let loopback: i64 = Connection::open(&db)
        .unwrap()
        .query_row(
            "SELECT COUNT(*) FROM transitions
             WHERE from_state = 'Verifying' AND to_state = 'Observing'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(
        loopback >= 1,
        "a failed check must loop back from Verifying to Observing"
    );

    let _ = git.remove();
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_file(&db);
}
