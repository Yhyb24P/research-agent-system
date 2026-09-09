//! End-to-end proof of native self-correction that survives a lost model
//! request. One Coding Agent inspects a minimal code repo, reads the value to
//! fix from a `view_file` result, makes a wrong first edit, sees the failed
//! check's output in its next bounded context, corrects the edit, and
//! delivers. Midway, a model request is lost; the SQLite journal is closed
//! and reopened, the session is recovered, and the run continues to a clean
//! delivery. Every turn, tool request, and result is asserted durably, with
//! the original worktree left untouched.

use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use agent_code_context::{ContextBudget, ContextError, HistorySink, HistorySource};
use agent_code_core::{AgentState, Journal, Session, SessionId};
use agent_code_model::{ModelClient, ModelContext, ModelDecision, ModelError, Observation};
use agent_code_runtime::{AgentConfig, AgentError, AgentLoop};
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

/// A content-driven model. It reads the value to fix from the `view_file`
/// result and the target from the failed check's output — it never hardcodes
/// either. On the first time it reaches the corrective step it simulates a
/// lost model request (once), so the recovery path is exercised.
struct LostOnceModel {
    fail: Arc<AtomicBool>,
}

/// Pull the integer `N` out of an `echo N` line in a view observation.
fn echo_value(view_text: &str) -> Option<i32> {
    for line in view_text.lines() {
        if let Some(idx) = line.find("echo ") {
            let num: String = line[idx + 5..]
                .chars()
                .take_while(|c| c.is_ascii_digit())
                .collect();
            if let Ok(n) = num.parse::<i32>() {
                return Some(n);
            }
        }
    }
    None
}

/// Pull the target `N` out of a `want N` in the observations (the failed
/// check's output).
fn wanted(observations: &[String]) -> Option<i32> {
    for o in observations {
        if let Some(idx) = o.find("want ") {
            let num: String = o[idx + 5..]
                .chars()
                .take_while(|c| c.is_ascii_digit())
                .collect();
            if let Ok(n) = num.parse::<i32>() {
                return Some(n);
            }
        }
    }
    None
}

#[async_trait]
impl ModelClient for LostOnceModel {
    async fn decide(&self, ctx: &ModelContext) -> Result<ModelDecision, ModelError> {
        let obs = &ctx.observations;
        let views = obs
            .iter()
            .filter(|s| s.starts_with("view solve.sh"))
            .count();
        let edits = obs
            .iter()
            .filter(|s| s.starts_with("file solve.sh"))
            .count();
        let check_failed = obs.iter().any(|s| s.contains("FAIL: got"));

        // Simulate a lost model request the first time we reach the corrective
        // step (right after the first check failed). Recovery re-issues it.
        if edits == 1 && check_failed && self.fail.load(Ordering::SeqCst) {
            self.fail.store(false, Ordering::SeqCst);
            return Err(ModelError::Transport("simulated lost request".into()));
        }

        let edit = |old: String, new: String| {
            ModelDecision::ToolCall(ToolRequest::EditFile(EditFile {
                path: "solve.sh".into(),
                old_str: old,
                new_str: new,
                expected_file_hash: None,
            }))
        };
        Ok(match (views, edits, check_failed) {
            (0, _, _) => ModelDecision::ToolCall(ToolRequest::ViewFile(ViewFile {
                path: "solve.sh".into(),
                start_line: 1,
                end_line: 10,
            })),
            (_, 0, _) => {
                // Read the current value from the view content and bump it by
                // one (a deliberate wrong first attempt).
                let view = obs
                    .iter()
                    .find(|s| s.starts_with("view solve.sh"))
                    .expect("solve.sh was viewed");
                let cur = echo_value(view).expect("view content carries the echo value");
                edit(format!("echo {cur}"), format!("echo {}", cur + 1))
            }
            (_, 1, false) => ModelDecision::Final("attempt 1".into()),
            (_, 1, true) => {
                // Decide the corrective value from the failed check's output.
                let want = wanted(obs).expect("the failed check states the wanted value");
                let view = obs
                    .iter()
                    .find(|s| s.starts_with("view solve.sh"))
                    .expect("solve.sh was viewed");
                let cur = echo_value(view).expect("view content carries the echo value");
                edit(format!("echo {}", cur + 1), format!("echo {want}"))
            }
            (_, 2, _) => ModelDecision::Final("done".into()),
            _ => ModelDecision::Final("stuck".into()),
        })
    }
}

/// A minimal code repo: `solve.sh` prints the wrong answer; `check.sh` passes
/// only once `solve.sh` prints `4`.
fn temp_code_repo() -> std::path::PathBuf {
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
    std::fs::write(dir.join("solve.sh"), "#!/bin/sh\necho 2\n").unwrap();
    std::fs::write(
        dir.join("check.sh"),
        "#!/bin/sh\nout=$(sh solve.sh)\nif [ \"$out\" = \"4\" ]; then\n  echo \"PASS: answer is 4\"\nelse\n  echo \"FAIL: got $out, want 4\"\n  exit 1\nfi\n",
    )
    .unwrap();
    git(&["add", "."]);
    git(&["commit", "-q", "-m", "init"]);
    dir
}

/// The configured check: runs `check.sh`, which passes only when the answer is `4`.
fn check_cmd() -> ExecuteCommand {
    ExecuteCommand {
        program: "sh".into(),
        args: vec!["check.sh".into()],
        cwd: None,
        timeout_seconds: 10,
        env: std::collections::BTreeMap::new(),
    }
}

#[tokio::test]
async fn self_correction_survives_a_lost_model_request() {
    let repo = temp_code_repo();
    let db = std::env::temp_dir().join(format!(
        "agent_code_e2e_{}_{}.db",
        std::process::id(),
        std::thread::current().name().unwrap_or("t")
    ));
    let _ = std::fs::remove_file(&db);

    let sid = SessionId::new("e2e");
    let git = GitWorkspace::create(&repo).unwrap();
    let fail_flag = Arc::new(AtomicBool::new(true));
    let cfg = AgentConfig {
        task: "make the check pass".into(),
        project_rules: String::new(),
        repository_map: String::new(),
        budget: ContextBudget::new(2000, 200),
        max_rounds: 8,
        verify: Some(check_cmd()),
    };

    // Phase 1: run until the simulated lost request. The session is left in
    // `WaitingModel`; dropping the loop closes its journal connection.
    {
        let j_sess = SqliteJournal::open(Connection::open(&db).unwrap(), sid.clone()).unwrap();
        let j_io = SqliteJournal::open(Connection::open(&db).unwrap(), sid.clone()).unwrap();
        let session = Session::create(j_sess).unwrap();
        let io = Rc::new(j_io);
        let model = Box::new(LostOnceModel {
            fail: Arc::clone(&fail_flag),
        });
        let mut loop_ = AgentLoop::new(
            session,
            model,
            git.clone(),
            Box::new(SharedStore(Rc::clone(&io))),
            Box::new(SharedStore(io)),
            cfg.clone(),
            sid.clone(),
        )
        .unwrap();
        let result = loop_.run().await;
        assert!(
            matches!(result, Err(AgentError::Model(_))),
            "the simulated lost request must surface as a model error: {result:?}"
        );
    }

    // Phase 2: reopen the DB, recover the session, and continue to a delivery.
    let delivery = {
        let j_sess = SqliteJournal::open(Connection::open(&db).unwrap(), sid.clone()).unwrap();
        let j_io = SqliteJournal::open(Connection::open(&db).unwrap(), sid.clone()).unwrap();
        let session = Session::recover(j_sess).unwrap();
        // The lost request left the session in WaitingModel; recovery resumes
        // to Observing so the next turn can re-issue it.
        assert_eq!(session.state(), AgentState::Observing);
        let io = Rc::new(j_io);
        let model = Box::new(LostOnceModel { fail: fail_flag });
        let mut loop_ = AgentLoop::new(
            session,
            model,
            git.clone(),
            Box::new(SharedStore(Rc::clone(&io))),
            Box::new(SharedStore(io)),
            cfg,
            sid.clone(),
        )
        .unwrap();
        loop_.run().await.unwrap()
    };

    // The agent self-corrected and delivered the corrected answer.
    assert_eq!(delivery.summary, "done");
    assert!(delivery.checks_run.iter().any(|c| c.contains("check.sh")));
    assert!(delivery.diff.contains("echo 4"));
    assert!(delivery.changed_files.iter().any(|f| f == "solve.sh"));

    // The original worktree was never touched.
    assert_eq!(
        std::fs::read_to_string(repo.join("solve.sh")).unwrap(),
        "#!/bin/sh\necho 2\n"
    );

    // Reopen the DB: the full durable record is still there.
    let j = SqliteJournal::open(Connection::open(&db).unwrap(), sid.clone()).unwrap();
    assert_eq!(j.current_state().unwrap(), Some(AgentState::Completed));
    // view + first edit + failed check + corrective edit + passing check.
    assert_eq!(j.highest_call_id().unwrap(), Some(5));
    let obs = j.read_observations().unwrap();

    // Both checks are durably recorded: the failure that triggered the
    // correction, and the pass that closed it.
    assert!(
        obs.iter()
            .any(|o| matches!(o, Observation::Text(t) if t.contains("FAIL: got 3, want 4"))),
        "the failed check output must be recorded: {obs:?}"
    );
    assert!(
        obs.iter()
            .any(|o| matches!(o, Observation::Text(t) if t.contains("PASS: answer is 4"))),
        "the passing check output must be recorded: {obs:?}"
    );

    // Every model turn is durable, including the one lost request.
    let conn = Connection::open(&db).unwrap();
    let turns: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM agent_turns WHERE session_id = ?1",
            [sid.as_str()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        turns, 6,
        "five good turns plus the one lost request: {obs:?}"
    );
    let errored: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM agent_turns WHERE session_id = ?1 AND error IS NOT NULL",
            [sid.as_str()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(errored, 1, "exactly one turn is the lost request");

    // Every tool call has a durable typed request payload, in monotonic order.
    let calls: Vec<(i64, i64, String)> = {
        let mut stmt = conn
            .prepare(
                "SELECT call_id, request IS NOT NULL, state FROM tool_calls WHERE session_id = ?1 ORDER BY call_id",
            )
            .unwrap();
        let rows = stmt
            .query_map([sid.as_str()], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })
            .unwrap();
        rows.map(|r| r.unwrap()).collect()
    };
    assert_eq!(
        calls.iter().map(|(id, _, _)| *id).collect::<Vec<_>>(),
        vec![1, 2, 3, 4, 5],
        "call ids must be monotonic"
    );
    assert!(
        calls.iter().all(|(_, has_req, _)| *has_req == 1),
        "every tool call must carry a durable request payload"
    );
    // The failed check (call 3) and the passing check (call 5) are both in
    // the tool log, alongside the two edits.
    let states: Vec<&str> = calls.iter().map(|c| c.2.as_str()).collect();
    assert_eq!(
        states,
        vec!["Succeeded", "Succeeded", "Failed", "Succeeded", "Succeeded"]
    );

    let _ = git.remove();
    let _ = std::fs::remove_dir_all(&repo);
    let _ = std::fs::remove_file(&db);
}
