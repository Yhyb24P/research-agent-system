//! The native Coding Agent loop: bounded context, model decision, tool
//! dispatch, and durable recording of every decision and result.

use agent_code_context::{
    build_compact_context, BytesTokenCounter, ContextBudget, HistorySink, HistorySource,
};
use agent_code_core::{AgentState, Journal, JournalError, Session, SessionId, ToolCallId};
use agent_code_model::{ModelClient, ModelContext, ModelDecision, ModelError, Observation};
use agent_code_tools::{ExecuteCommand, ToolRequest};
use agent_code_workspace::{GitWorkspace, ToolError, Workspace};

use crate::dispatch::{dispatch, observations_for};

/// Errors from driving the agent loop.
#[derive(Debug)]
pub enum AgentError {
    /// The durable state machine (journal) failed.
    Journal(JournalError),
    /// The model client failed.
    Model(ModelError),
    /// Building the bounded context failed.
    Context(agent_code_context::ContextError),
    /// A workspace operation at the loop level failed.
    Tool(ToolError),
    /// The model kept acting past the round budget without finishing.
    MaxRounds,
}

impl std::fmt::Display for AgentError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Journal(e) => write!(f, "journal error: {e}"),
            Self::Model(e) => write!(f, "model error: {e}"),
            Self::Context(e) => write!(f, "context error: {e}"),
            Self::Tool(e) => write!(f, "tool error: {e}"),
            Self::MaxRounds => write!(f, "agent exceeded the round budget without finishing"),
        }
    }
}

impl std::error::Error for AgentError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Journal(e) => Some(e),
            Self::Model(e) => Some(e),
            Self::Context(e) => Some(e),
            Self::Tool(e) => Some(e),
            Self::MaxRounds => None,
        }
    }
}

impl From<JournalError> for AgentError {
    fn from(e: JournalError) -> Self {
        Self::Journal(e)
    }
}
impl From<ModelError> for AgentError {
    fn from(e: ModelError) -> Self {
        Self::Model(e)
    }
}
impl From<agent_code_context::ContextError> for AgentError {
    fn from(e: agent_code_context::ContextError) -> Self {
        Self::Context(e)
    }
}
impl From<ToolError> for AgentError {
    fn from(e: ToolError) -> Self {
        Self::Tool(e)
    }
}

/// Configuration for one agent run.
#[derive(Debug, Clone)]
pub struct AgentConfig {
    /// The task objective, billed as the highest-priority context section.
    pub task: String,
    /// Bounded project rules (AGENTS.md and friends).
    pub project_rules: String,
    /// A shallow, bounded repository map.
    pub repository_map: String,
    /// The token budget for the model-facing context.
    pub budget: ContextBudget,
    /// Maximum model rounds before the run is failed. Bounds the loop.
    pub max_rounds: u32,
    /// Optional check run on every final decision. A non-zero exit records the
    /// failure and loops back so the model can self-correct (N15).
    pub verify: Option<ExecuteCommand>,
}

/// What the Agent delivers on success: the summary, the real diff, the files
/// it changed, the checks it ran, and any known failures or artifacts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Delivery {
    pub summary: String,
    pub changed_files: Vec<String>,
    pub diff: String,
    /// The verification checks that ran and passed before delivery.
    pub checks_run: Vec<String>,
    /// Failures the agent acknowledges at delivery time (empty on a clean run).
    pub known_failures: Vec<String>,
    /// Artifact paths the agent produced (empty when none).
    pub artifacts: Vec<String>,
}

/// Drives one Coding Agent session to a terminal state.
///
/// Generic over the journal `J`; the durable observation store is supplied as
/// a [`HistorySource`] (to build each turn's bounded context) and a
/// [`HistorySink`] (to record each decision and tool result). A store that is
/// both keeps the next model turn informed by what the previous one did.
pub struct AgentLoop<J: Journal> {
    session: Session<J>,
    model: Box<dyn ModelClient>,
    git: GitWorkspace,
    ws: Workspace,
    source: Box<dyn HistorySource>,
    sink: Box<dyn HistorySink>,
    cfg: AgentConfig,
    session_id: SessionId,
    next_call: u64,
}

impl<J: Journal> AgentLoop<J> {
    /// Assemble a loop over a (possibly recovered) session. The next tool-call
    /// id resumes past the highest one already recorded, so ids stay
    /// monotonic across a crash/recovery and are never reused.
    pub fn new(
        session: Session<J>,
        model: Box<dyn ModelClient>,
        git: GitWorkspace,
        source: Box<dyn HistorySource>,
        sink: Box<dyn HistorySink>,
        cfg: AgentConfig,
        session_id: SessionId,
    ) -> Result<Self, AgentError> {
        let ws = git.workspace()?;
        let next_call = session
            .journal()
            .highest_call_id()?
            .map(|m| m + 1)
            .unwrap_or(1);
        Ok(Self {
            session,
            model,
            git,
            ws,
            source,
            sink,
            cfg,
            session_id,
            next_call,
        })
    }

    /// The session id this loop drives.
    pub fn session_id(&self) -> &SessionId {
        &self.session_id
    }

    /// Drive the loop to a terminal state. Returns the delivery on success;
    /// fails the session and returns [`AgentError::MaxRounds`] if the model
    /// does not reach a final decision within the budget.
    pub async fn run(&mut self) -> Result<Delivery, AgentError> {
        // Only a fresh session needs the first hop into the Observing hub; a
        // recovered session already sits in a resumable state.
        if matches!(self.session.state(), AgentState::Initializing) {
            self.session.observe()?;
        }
        for _round in 0..self.cfg.max_rounds {
            let ctx = self.build_context()?;
            self.session.wait_model()?;
            let decision = self.model.decide(&ctx).await?;
            match decision {
                ModelDecision::Final(summary) => match self.finalize(summary).await? {
                    Some(delivery) => return Ok(delivery),
                    // A configured check failed: its result is now in the
                    // durable history, so the next model turn can react to it.
                    None => continue,
                },
                ModelDecision::ToolCall(req) => {
                    self.step_tool(req).await?;
                }
            }
        }
        self.session.fail()?;
        Err(AgentError::MaxRounds)
    }

    /// One tool round: assign a monotonic call id, atomically begin it, run
    /// the real tool, record the result durably, and close the call.
    async fn step_tool(&mut self, req: ToolRequest) -> Result<(), AgentError> {
        let call = ToolCallId::new(self.next_call);
        self.next_call += 1;
        self.session.begin_tool(call)?;
        let outcome = dispatch(&self.ws, &req);
        for obs in observations_for(&req, &outcome) {
            self.sink.append(&self.session_id, &obs)?;
        }
        if outcome.ok {
            self.session.tool_succeeded(call)?;
        } else {
            self.session.tool_failed(call)?;
        }
        Ok(())
    }

    /// A final decision: record it, run the configured check, then either
    /// deliver (check passed or none configured) or loop back so the model can
    /// self-correct. Returns `Some(delivery)` on success, `None` when a check
    /// failed and the loop should continue (N15).
    async fn finalize(&mut self, summary: String) -> Result<Option<Delivery>, AgentError> {
        self.sink.append(
            &self.session_id,
            &Observation::Text(format!("final: {summary}")),
        )?;
        self.session.observe()?;
        self.session.verify()?;

        let mut checks_run = Vec::new();
        if let Some(check) = &self.cfg.verify {
            let req = ToolRequest::ExecuteCommand(check.clone());
            let outcome = dispatch(&self.ws, &req);
            if !outcome.ok {
                // The check failed: record it durably and return to Observing so
                // the next model turn sees the failure and can self-correct.
                for obs in observations_for(&req, &outcome) {
                    self.sink.append(&self.session_id, &obs)?;
                }
                self.session.observe()?; // Verifying -> Observing
                return Ok(None);
            }
            checks_run.push(req.describe());
        }

        let delivery = self.build_delivery(&summary, checks_run)?;
        self.session.deliver()?;
        self.session.complete()?;
        Ok(Some(delivery))
    }

    /// Build the next turn's bounded context from the durable history.
    fn build_context(&self) -> Result<ModelContext, AgentError> {
        Ok(build_compact_context(
            self.source.as_ref(),
            &self.session_id,
            &self.cfg.task,
            &self.cfg.project_rules,
            &self.cfg.repository_map,
            &self.cfg.budget,
            &BytesTokenCounter::default(),
        )?)
    }

    /// Assemble the delivery from the isolated worktree's real diff.
    fn build_delivery(
        &self,
        summary: &str,
        checks_run: Vec<String>,
    ) -> Result<Delivery, AgentError> {
        let since = self.git.initial_head();
        let changed_files = self.git.changed_files(since)?;
        let diff = self.git.diff(since)?;
        Ok(Delivery {
            summary: summary.to_string(),
            changed_files,
            diff,
            checks_run,
            known_failures: Vec::new(),
            artifacts: Vec::new(),
        })
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::sync::{Arc, Mutex};

    use agent_code_context::{ContextBudget, ContextError, HistorySink, HistorySource};
    use agent_code_core::{InMemoryJournal, Session, SessionId};
    use agent_code_model::{ModelClient, ModelContext, ModelDecision, ModelError, Observation};
    use agent_code_tools::{ToolRequest, ViewFile};
    use agent_code_workspace::GitWorkspace;
    use async_trait::async_trait;

    use super::*;

    /// A Vec-backed store that is both a source and a sink, sharing one log.
    #[derive(Clone)]
    struct MemStore {
        obs: Arc<Mutex<Vec<Observation>>>,
    }
    impl MemStore {
        fn new() -> Self {
            Self {
                obs: Arc::new(Mutex::new(Vec::new())),
            }
        }
    }
    impl HistorySource for MemStore {
        fn observations(&self, _s: &SessionId) -> Result<Vec<Observation>, ContextError> {
            Ok(self.obs.lock().unwrap().clone())
        }
    }
    impl HistorySink for MemStore {
        fn append(&self, _s: &SessionId, obs: &Observation) -> Result<(), ContextError> {
            self.obs.lock().unwrap().push(obs.clone());
            Ok(())
        }
    }

    /// A deterministic scripted model client.
    struct Scripted {
        decisions: Mutex<VecDeque<ModelDecision>>,
    }
    #[async_trait]
    impl ModelClient for Scripted {
        async fn decide(&self, _ctx: &ModelContext) -> Result<ModelDecision, ModelError> {
            self.decisions
                .lock()
                .unwrap()
                .pop_front()
                .ok_or(ModelError::BadResponse("scripted client exhausted".into()))
        }
    }

    fn temp_git_repo() -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("agent_code_rt_git_{}_{seq}", std::process::id()));
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
        std::fs::write(dir.join("a.txt"), "hello\n").unwrap();
        git(&["add", "."]);
        git(&["commit", "-q", "-m", "init"]);
        dir
    }

    /// A repo whose `answer.txt` holds the wrong value; the check passes only
    /// once the agent edits it to `4`.
    fn temp_answer_repo() -> std::path::PathBuf {
        use std::sync::atomic::{AtomicU32, Ordering};
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("agent_code_rt_answer_{}_{seq}", std::process::id()));
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
    async fn loop_dispatches_tools_and_delivers() {
        let repo = temp_git_repo();
        let git = GitWorkspace::create(&repo).unwrap();
        let sid = SessionId::new("s");
        let session = Session::create(InMemoryJournal::new(sid.clone())).unwrap();
        let store = MemStore::new();
        let model: Box<dyn ModelClient> = Box::new(Scripted {
            decisions: Mutex::new(
                vec![
                    ModelDecision::ToolCall(ToolRequest::ViewFile(ViewFile {
                        path: "a.txt".into(),
                        start_line: 1,
                        end_line: 10,
                    })),
                    ModelDecision::Final("done".into()),
                ]
                .into_iter()
                .collect(),
            ),
        });
        let cfg = AgentConfig {
            task: "inspect a.txt".into(),
            project_rules: String::new(),
            repository_map: String::new(),
            budget: ContextBudget::new(2000, 200),
            max_rounds: 5,
            verify: None,
        };
        let mut loop_ = AgentLoop::new(
            session,
            model,
            git,
            Box::new(store.clone()),
            Box::new(store.clone()),
            cfg,
            sid.clone(),
        )
        .unwrap();
        let delivery = loop_.run().await.unwrap();
        assert_eq!(delivery.summary, "done");
        // The shared store recorded both the view result and the final decision.
        let obs = store.obs.lock().unwrap();
        assert!(obs
            .iter()
            .any(|o| matches!(o, Observation::Text(t) if t.starts_with("view a.txt"))));
        assert!(obs
            .iter()
            .any(|o| matches!(o, Observation::Text(t) if t.starts_with("final:"))));
        let _ = std::fs::remove_dir_all(&repo);
    }

    #[tokio::test]
    async fn verification_failure_loops_back_until_the_check_passes() {
        use agent_code_tools::EditFile;

        let repo = temp_answer_repo();
        let git = GitWorkspace::create(&repo).unwrap();
        let sid = SessionId::new("s");
        let session = Session::create(InMemoryJournal::new(sid.clone())).unwrap();
        let store = MemStore::new();
        // Final (check fails) -> corrective edit -> Final (check passes).
        let model: Box<dyn ModelClient> = Box::new(Scripted {
            decisions: Mutex::new(
                vec![
                    ModelDecision::Final("done".into()),
                    ModelDecision::ToolCall(ToolRequest::EditFile(EditFile {
                        path: "answer.txt".into(),
                        old_str: "2".into(),
                        new_str: "4".into(),
                        expected_file_hash: None,
                    })),
                    ModelDecision::Final("fixed".into()),
                ]
                .into_iter()
                .collect(),
            ),
        });
        let cfg = AgentConfig {
            task: "make the check pass".into(),
            project_rules: String::new(),
            repository_map: String::new(),
            budget: ContextBudget::new(2000, 200),
            max_rounds: 5,
            verify: Some(answer_check()),
        };
        let mut loop_ = AgentLoop::new(
            session,
            model,
            git.clone(),
            Box::new(store.clone()),
            Box::new(store.clone()),
            cfg,
            sid.clone(),
        )
        .unwrap();
        let delivery = loop_.run().await.unwrap();

        // Delivered on the corrected answer, with the check recorded.
        assert_eq!(delivery.summary, "fixed");
        assert_eq!(delivery.checks_run.len(), 1);
        assert!(delivery.checks_run[0].contains("sh -c"));
        assert!(delivery.changed_files.iter().any(|f| f == "answer.txt"));
        assert!(delivery.diff.contains("4"));

        // The failed check entered the durable history (a recorded check error).
        let obs = store.obs.lock().unwrap();
        assert!(
            obs.iter().any(|o| matches!(
                o,
                Observation::Error { signature, .. } if signature.contains("sh failed")
            )),
            "expected a recorded check failure: {obs:?}"
        );
        let _ = git.remove();
        let _ = std::fs::remove_dir_all(&repo);
    }
}
