//! The Lead plan-follow-up-synthesis loop.
//!
//! The Lead plans (delegating at least two structured subtasks), follows up
//! on worker results, and synthesizes a final answer grounded in the actual
//! results of completed tasks. It is bounded by a maximum number of rounds,
//! tasks, and (via the scheduler) attempts, so it cannot loop without bound.

use crate::board::{AgentMessage, ArtifactMeta, TaskBoard, TaskStatus};
use crate::registry::AgentTaskResult;
use crate::scheduler::{ScheduleError, Scheduler, TaskSpec};

/// The Lead's structured output boundary.
#[derive(Debug, Clone)]
pub enum LeadDecision {
    /// Create structured subtasks (the first round).
    Delegate(Vec<TaskSpec>),
    /// Create follow-up tasks based on worker results.
    FollowUp(Vec<TaskSpec>),
    /// Finish with a final result grounded in completed tasks.
    Complete(TeamResult),
}

/// The final team result.
#[derive(Debug, Clone)]
pub struct TeamResult {
    pub answer: String,
    /// The completed tasks whose actual results ground this answer.
    pub task_refs: Vec<u64>,
}

/// The context handed to the Lead brain each round.
#[derive(Debug, Clone)]
pub struct LeadContext {
    pub objective: String,
    pub round: u32,
    /// Completed task results, as (task id, result).
    pub results: Vec<(u64, AgentTaskResult)>,
    pub artifacts: Vec<ArtifactMeta>,
    /// Messages addressed to the Lead.
    pub messages: Vec<AgentMessage>,
}

/// Produces the Lead's next decision from the current context.
pub trait LeadBrain: Send + Sync {
    /// Decide the next step given `ctx`.
    fn decide(&self, ctx: &LeadContext) -> LeadDecision;
}

/// An error from the Lead loop.
#[derive(Debug, Clone)]
pub enum LeadError {
    /// A scheduling operation failed.
    Schedule(ScheduleError),
    /// The maximum number of rounds was reached without completion.
    MaxRounds,
    /// The maximum number of tasks was exceeded.
    TooManyTasks,
    /// The first round must delegate at least two subtasks.
    InvalidFirstDecision,
    /// A follow-up was requested before any worker result existed.
    FollowUpWithoutResult,
    /// The final result does not reference completed tasks.
    CompletionNotGrounded,
}

impl From<ScheduleError> for LeadError {
    fn from(e: ScheduleError) -> Self {
        Self::Schedule(e)
    }
}

/// The Lead: plans, follows up on worker results, and synthesizes the final
/// answer. Bounded by a maximum number of rounds and tasks, and (through the
/// scheduler) a maximum number of attempts per task.
pub struct Lead<B: TaskBoard> {
    brain: Box<dyn LeadBrain>,
    scheduler: Scheduler<B>,
    max_rounds: u32,
    max_tasks: usize,
    task_ids: Vec<u64>,
}

impl<B: TaskBoard> Lead<B> {
    /// Build a Lead. The scheduler's `max_retries` is the per-task attempt cap.
    pub fn new(
        brain: Box<dyn LeadBrain>,
        scheduler: Scheduler<B>,
        max_rounds: u32,
        max_tasks: usize,
    ) -> Self {
        Self {
            brain,
            scheduler,
            max_rounds,
            max_tasks,
            task_ids: Vec::new(),
        }
    }

    /// Run the plan-follow-up-synthesis loop for `objective`.
    pub async fn run(&mut self, objective: &str) -> Result<TeamResult, LeadError> {
        let mut round = 0u32;
        loop {
            if round >= self.max_rounds {
                return Err(LeadError::MaxRounds);
            }
            let ctx = self.build_context(objective, round);
            match self.brain.decide(&ctx) {
                LeadDecision::Delegate(specs) => {
                    if round == 0 && specs.len() < 2 {
                        return Err(LeadError::InvalidFirstDecision);
                    }
                    self.extend_tasks(&specs).await?;
                    round += 1;
                }
                LeadDecision::FollowUp(specs) => {
                    if ctx.results.is_empty() {
                        return Err(LeadError::FollowUpWithoutResult);
                    }
                    self.extend_tasks(&specs).await?;
                    round += 1;
                }
                LeadDecision::Complete(result) => {
                    if round == 0 {
                        return Err(LeadError::CompletionNotGrounded);
                    }
                    self.verify_completion(&result)?;
                    return Ok(result);
                }
            }
        }
    }

    /// Build the Lead's context from the durable board: completed task
    /// results, artifacts, and messages addressed to the Lead. This is how a
    /// worker's result, artifact, and message reach the Lead's next round
    /// (T07/T08/T09) without a human copying anything.
    fn build_context(&self, objective: &str, round: u32) -> LeadContext {
        let board = self.scheduler.board();
        let mut results = Vec::new();
        let mut artifacts = Vec::new();
        for &id in &self.task_ids {
            if let Ok(Some(record)) = board.task(id) {
                if record.status == TaskStatus::Succeeded {
                    if let Ok(attempts) = board.attempts(id) {
                        if let Some(last) = attempts
                            .iter()
                            .rev()
                            .find(|a| a.status == TaskStatus::Succeeded)
                        {
                            if let Some(summary) = &last.result {
                                results.push((
                                    id,
                                    AgentTaskResult {
                                        task_id: id,
                                        summary: summary.clone(),
                                        artifacts: Vec::new(),
                                    },
                                ));
                            }
                        }
                    }
                }
                if let Ok(arts) = board.artifacts(id) {
                    artifacts.extend(arts);
                }
            }
        }
        let messages = board.messages_to("lead").unwrap_or_default();
        LeadContext {
            objective: objective.to_string(),
            round,
            results,
            artifacts,
            messages,
        }
    }

    /// Schedule a batch of subtasks and remember their ids.
    async fn extend_tasks(&mut self, specs: &[TaskSpec]) -> Result<(), LeadError> {
        if self.task_ids.len() + specs.len() > self.max_tasks {
            return Err(LeadError::TooManyTasks);
        }
        let results = self.scheduler.schedule(specs).await?;
        self.task_ids.extend(results.iter().map(|r| r.task_id));
        Ok(())
    }

    /// The final result must reference real, completed tasks (T16): no test
    /// fixture may splice in an ungrounded answer.
    fn verify_completion(&self, result: &TeamResult) -> Result<(), LeadError> {
        if result.task_refs.is_empty() {
            return Err(LeadError::CompletionNotGrounded);
        }
        let board = self.scheduler.board();
        for &task_id in &result.task_refs {
            let record = board
                .task(task_id)
                .map_err(|_| LeadError::CompletionNotGrounded)?
                .ok_or(LeadError::CompletionNotGrounded)?;
            if record.status != TaskStatus::Succeeded {
                return Err(LeadError::CompletionNotGrounded);
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use crate::lead::{Lead, LeadBrain, LeadContext, LeadDecision, TeamResult};
    use crate::registry::TaskKind;
    use crate::scheduler::{Scheduler, TaskSpec};
    use crate::testutil::{err_driver, ok_driver, trio_registry, MemBoard};

    /// A deterministic Lead brain: delegate two tasks, follow up on a result,
    /// then complete with an answer grounded in the actual results.
    struct ScriptedBrain;

    impl LeadBrain for ScriptedBrain {
        fn decide(&self, ctx: &LeadContext) -> LeadDecision {
            match ctx.round {
                0 => LeadDecision::Delegate(vec![
                    TaskSpec {
                        objective: "summarize data".into(),
                        kind: TaskKind::Bulk,
                        target: Some("worker-a".into()),
                        parent: None,
                        context: Vec::new(),
                    },
                    TaskSpec {
                        objective: "fetch utility".into(),
                        kind: TaskKind::Utility,
                        target: Some("utility-a".into()),
                        parent: None,
                        context: Vec::new(),
                    },
                ]),
                1 => {
                    // T10: a follow-up grounded in a worker result.
                    let parent = ctx.results.first().map(|(id, _)| *id);
                    LeadDecision::FollowUp(vec![TaskSpec {
                        objective: "refine the summary".into(),
                        kind: TaskKind::Reasoning,
                        target: Some("reasoner-a".into()),
                        parent,
                        context: Vec::new(),
                    }])
                }
                _ => {
                    // T16: the answer is synthesized from the actual results.
                    let task_refs: Vec<u64> = ctx.results.iter().map(|(id, _)| *id).collect();
                    let answer = ctx
                        .results
                        .iter()
                        .map(|(_, r)| r.summary.clone())
                        .collect::<Vec<_>>()
                        .join("; ");
                    LeadDecision::Complete(TeamResult { answer, task_refs })
                }
            }
        }
    }

    fn lead() -> Lead<MemBoard> {
        let drivers = BTreeMap::from([
            ("worker-a".to_string(), ok_driver("data summary")),
            ("worker-b".to_string(), ok_driver("worker-b")),
            ("utility-a".to_string(), ok_driver("utility output")),
            ("reasoner-a".to_string(), ok_driver("refined insight")),
        ]);
        let sched = Scheduler::new(trio_registry(), drivers, MemBoard::default(), 1);
        Lead::new(Box::new(ScriptedBrain), sched, 5, 10)
    }

    // T03/T07/T08/T10/T16: the Lead delegates >=2 tasks, follows up on a
    // result, and synthesizes an answer grounded in the actual results.
    #[tokio::test]
    async fn lead_delegates_follows_up_and_synthesizes() {
        let mut lead = lead();
        let result = lead
            .run("analyze the dataset")
            .await
            .expect("lead completes");
        // The answer is grounded in the actual worker results, not a fixture.
        assert!(result.answer.contains("data summary"));
        assert!(result.answer.contains("utility output"));
        assert!(result.task_refs.len() >= 2);
    }

    // The first round must delegate at least two subtasks.
    #[tokio::test]
    async fn first_round_requires_two_subtasks() {
        struct OneTaskBrain;
        impl LeadBrain for OneTaskBrain {
            fn decide(&self, _ctx: &LeadContext) -> LeadDecision {
                LeadDecision::Delegate(vec![TaskSpec {
                    objective: "only one".into(),
                    kind: TaskKind::Bulk,
                    target: Some("worker-a".into()),
                    parent: None,
                    context: Vec::new(),
                }])
            }
        }
        let drivers = BTreeMap::from([("worker-a".to_string(), ok_driver("ok"))]);
        let sched = Scheduler::new(trio_registry(), drivers, MemBoard::default(), 1);
        let mut lead = Lead::new(Box::new(OneTaskBrain), sched, 5, 10);
        let err = lead.run("x").await.expect_err("one subtask is rejected");
        assert!(matches!(err, crate::lead::LeadError::InvalidFirstDecision));
    }

    // A completion that references no completed task is rejected.
    #[tokio::test]
    async fn ungrounded_completion_is_rejected() {
        struct UngroundedBrain;
        impl LeadBrain for UngroundedBrain {
            fn decide(&self, _ctx: &LeadContext) -> LeadDecision {
                LeadDecision::Complete(TeamResult {
                    answer: "made up".into(),
                    task_refs: Vec::new(),
                })
            }
        }
        let drivers = BTreeMap::from([("worker-a".to_string(), err_driver("down"))]);
        let sched = Scheduler::new(trio_registry(), drivers, MemBoard::default(), 1);
        let mut lead = Lead::new(Box::new(UngroundedBrain), sched, 5, 10);
        let err = lead.run("x").await.expect_err("ungrounded");
        assert!(matches!(err, crate::lead::LeadError::CompletionNotGrounded));
    }
}
