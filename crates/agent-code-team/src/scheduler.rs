//! Concurrent task scheduling with per-agent concurrency, retry, and
//! deterministic reassignment.
//!
//! Each agent has its own concurrency quota (`max_concurrency`), so two
//! independent tasks on different agents truly overlap rather than merely
//! being joined. A failed task retries the same agent up to a limit, then
//! excludes that agent and reassigns to the next deterministic candidate.
//! Every attempt is persisted independently, so a failure is never overwritten
//! by a later retry.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use tokio::sync::Semaphore;

use crate::board::{BoardError, TaskAttempt, TaskBoard, TaskStatus};
use crate::registry::{AgentDriver, AgentRegistry, AgentTask, AgentTaskResult, TaskKind};

/// A structured subtask to create and run.
#[derive(Debug, Clone)]
pub struct TaskSpec {
    pub objective: String,
    pub kind: TaskKind,
    /// An explicit user target (T13); it applies to not-yet-started and
    /// rescheduled tasks.
    pub target: Option<String>,
    /// The parent task, when this is a follow-up.
    pub parent: Option<u64>,
    /// Context handed to the driver.
    pub context: Vec<String>,
}

/// The outcome of scheduling one task.
#[derive(Debug, Clone)]
pub struct ScheduledResult {
    pub task_id: u64,
    pub result: Result<AgentTaskResult, String>,
    /// Every attempt, in order. Failures are kept, never overwritten.
    pub attempts: Vec<TaskAttempt>,
}

/// An error from the scheduler.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScheduleError {
    /// A task-board operation failed.
    Board(BoardError),
    /// A spawned task panicked or was cancelled.
    JoinFailed,
}

impl From<BoardError> for ScheduleError {
    fn from(e: BoardError) -> Self {
        Self::Board(e)
    }
}

/// Schedules tasks across a team, honoring each agent's concurrency quota,
/// retrying failures, and reassigning deterministically.
pub struct Scheduler<B: TaskBoard> {
    registry: AgentRegistry,
    drivers: BTreeMap<String, Arc<dyn AgentDriver>>,
    board: B,
    max_retries: u32,
}

impl<B: TaskBoard> Scheduler<B> {
    /// Build a scheduler. `drivers` maps agent id to its driver; `max_retries`
    /// is the per-agent retry limit before reassignment.
    pub fn new(
        registry: AgentRegistry,
        drivers: BTreeMap<String, Arc<dyn AgentDriver>>,
        board: B,
        max_retries: u32,
    ) -> Self {
        Self {
            registry,
            drivers,
            board,
            max_retries,
        }
    }

    /// The durable board this scheduler persists to.
    pub fn board(&self) -> &B {
        &self.board
    }

    /// Create and run `specs` concurrently. Each task is bounded by its agent's
    /// concurrency quota; on failure it retries the same agent up to
    /// `max_retries`, then reassigns to the next candidate. Every attempt is
    /// persisted to the board independently.
    pub async fn schedule(
        &mut self,
        specs: &[TaskSpec],
    ) -> Result<Vec<ScheduledResult>, ScheduleError> {
        // 1. Create the tasks on the board (durable, T02).
        let mut task_ids = Vec::new();
        for spec in specs {
            let id = self.board.create_task(
                &spec.objective,
                spec.parent,
                spec.kind,
                spec.target.clone(),
            )?;
            task_ids.push(id);
        }

        // 2. Run them concurrently, each bounded by its agent's quota.
        let mut handles = Vec::new();
        for (i, spec) in specs.iter().enumerate() {
            let task = AgentTask {
                id: task_ids[i],
                objective: spec.objective.clone(),
                kind: spec.kind,
                context: spec.context.clone(),
            };
            let candidates = self.candidate_order(spec.kind, spec.target.as_deref());
            let drivers = self.drivers.clone();
            let semaphores = self.semaphores();
            let max_retries = self.max_retries;
            handles.push(tokio::spawn(async move {
                run_one(&drivers, &semaphores, max_retries, task, candidates).await
            }));
        }

        // 3. Collect results and persist each attempt. The runs were concurrent;
        //    persistence is serial, which is fine because the board is `&mut`.
        let mut out = Vec::new();
        for (i, handle) in handles.into_iter().enumerate() {
            let (attempts, result) = handle.await.map_err(|_| ScheduleError::JoinFailed)?;
            let task_id = task_ids[i];
            for a in &attempts {
                self.board.record_attempt(a)?;
            }
            // Record the actual assignee (the agent of the last attempt).
            if let Some(last) = attempts.last() {
                self.board.assign(task_id, &last.agent_id)?;
            }
            let status = match &result {
                Ok(_) => TaskStatus::Succeeded,
                Err(_) => TaskStatus::Failed,
            };
            self.board.set_status(task_id, status)?;
            // Flow the result back: the directed message and artifacts.
            if let Ok(res) = &result {
                if let Some(msg) = &res.message {
                    self.board.record_message(msg)?;
                }
                for art in &res.artifacts {
                    self.board.record_artifact(task_id, art)?;
                }
            }
            out.push(ScheduledResult {
                task_id,
                result,
                attempts,
            });
        }
        Ok(out)
    }

    /// The candidate agent order for a task: the explicit target first (if it
    /// names a registered agent), then the task tier's agents in stable id
    /// order. Deterministic and independent of transient completion order.
    fn candidate_order(&self, kind: TaskKind, target: Option<&str>) -> Vec<String> {
        let mut order: Vec<String> = Vec::new();
        if let Some(t) = target {
            if self.registry.get(t).is_some() {
                order.push(t.to_string());
            }
        }
        let tier = kind.tier();
        for id in self.registry.agent_ids() {
            if self
                .registry
                .get(id)
                .map(|c| c.tier == tier)
                .unwrap_or(false)
            {
                order.push(id.to_string());
            }
        }
        let mut seen = BTreeSet::new();
        order.retain(|id| seen.insert(id.clone()));
        order
    }

    /// One concurrency semaphore per agent, sized by its `max_concurrency`.
    fn semaphores(&self) -> BTreeMap<String, Arc<Semaphore>> {
        let mut m = BTreeMap::new();
        for id in self.registry.agent_ids() {
            let mc = self
                .registry
                .get(id)
                .map(|c| c.max_concurrency)
                .unwrap_or(1);
            m.insert(id.to_string(), Arc::new(Semaphore::new(mc)));
        }
        m
    }
}

/// Run one task: walk the candidate agents, retrying each up to `max_retries`
/// under its concurrency quota, then reassigning deterministically. Returns the
/// attempts (in a global sequence) and the final result.
async fn run_one(
    drivers: &BTreeMap<String, Arc<dyn AgentDriver>>,
    semaphores: &BTreeMap<String, Arc<Semaphore>>,
    max_retries: u32,
    task: AgentTask,
    candidates: Vec<String>,
) -> (Vec<TaskAttempt>, Result<AgentTaskResult, String>) {
    let mut attempts = Vec::new();
    let mut last_result: Option<AgentTaskResult> = None;
    let mut attempt_seq = 0u32;
    for agent in &candidates {
        let driver = match drivers.get(agent) {
            Some(d) => d,
            None => continue,
        };
        let sem = match semaphores.get(agent) {
            Some(s) => s,
            None => continue,
        };
        let mut succeeded = false;
        for _ in 0..max_retries {
            attempt_seq += 1;
            let permit = sem.acquire().await;
            let outcome = driver.run_task(task.clone()).await;
            drop(permit);
            match outcome {
                Ok(result) => {
                    attempts.push(TaskAttempt {
                        task_id: task.id,
                        attempt: attempt_seq,
                        agent_id: agent.clone(),
                        status: TaskStatus::Succeeded,
                        result: Some(result.summary.clone()),
                        error: None,
                    });
                    last_result = Some(result);
                    succeeded = true;
                    break;
                }
                Err(err) => {
                    attempts.push(TaskAttempt {
                        task_id: task.id,
                        attempt: attempt_seq,
                        agent_id: agent.clone(),
                        status: TaskStatus::Failed,
                        result: None,
                        error: Some(err.clone()),
                    });
                }
            }
        }
        if succeeded {
            break;
        }
        // Exhausted this agent's retries: it is excluded and the next
        // candidate is tried (deterministic reassignment).
    }
    match last_result {
        Some(r) => (attempts, Ok(r)),
        None => {
            let last_err = attempts
                .last()
                .and_then(|a| a.error.clone())
                .unwrap_or_else(|| "no candidate agent".to_string());
            (attempts, Err(last_err))
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    use async_trait::async_trait;

    use super::*;
    use crate::testutil::{err_driver, ok_driver, trio_registry, MemBoard};

    /// Records the peak number of in-flight tasks and only proceeds once two
    /// have been in-flight together, proving real overlap.
    struct OverlapDriver {
        in_flight: Arc<AtomicUsize>,
        peak: Arc<AtomicUsize>,
        summary: String,
    }

    #[async_trait]
    impl AgentDriver for OverlapDriver {
        async fn run_task(&self, task: AgentTask) -> Result<AgentTaskResult, String> {
            let cur = self.in_flight.fetch_add(1, Ordering::SeqCst) + 1;
            self.peak.fetch_max(cur, Ordering::SeqCst);
            while self.peak.load(Ordering::SeqCst) < 2 {
                tokio::task::yield_now().await;
            }
            self.in_flight.fetch_sub(1, Ordering::SeqCst);
            Ok(AgentTaskResult {
                task_id: task.id,
                summary: self.summary.clone(),
                artifacts: Vec::new(),
                message: None,
            })
        }
    }

    // T06: two independent tasks on two agents truly overlap.
    #[tokio::test]
    async fn independent_tasks_run_concurrently() {
        let in_flight = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        let drivers = BTreeMap::from([
            (
                "worker-a".to_string(),
                Arc::new(OverlapDriver {
                    in_flight: in_flight.clone(),
                    peak: peak.clone(),
                    summary: "a".into(),
                }) as Arc<dyn AgentDriver>,
            ),
            (
                "worker-b".to_string(),
                Arc::new(OverlapDriver {
                    in_flight: in_flight.clone(),
                    peak: peak.clone(),
                    summary: "b".into(),
                }) as Arc<dyn AgentDriver>,
            ),
        ]);
        let mut sched = Scheduler::new(trio_registry(), drivers, MemBoard::default(), 1);
        let specs = vec![
            TaskSpec {
                objective: "t1".into(),
                kind: TaskKind::Bulk,
                target: Some("worker-a".into()),
                parent: None,
                context: Vec::new(),
            },
            TaskSpec {
                objective: "t2".into(),
                kind: TaskKind::Bulk,
                target: Some("worker-b".into()),
                parent: None,
                context: Vec::new(),
            },
        ];
        let results = sched.schedule(&specs).await.expect("schedule");
        assert!(results[0].result.is_ok());
        assert!(results[1].result.is_ok());
        // Both tasks were in-flight at the same time.
        assert_eq!(peak.load(Ordering::SeqCst), 2);
    }

    // T11/T12: a task that fails on worker-a retries, then reassigns to
    // worker-b. The failure history is kept.
    #[tokio::test]
    async fn failed_task_retries_then_reassigns() {
        let drivers = BTreeMap::from([
            ("worker-a".to_string(), err_driver("worker-a is down")),
            ("worker-b".to_string(), ok_driver("worker-b")),
        ]);
        let mut sched = Scheduler::new(trio_registry(), drivers, MemBoard::default(), 2);
        let specs = vec![TaskSpec {
            objective: "job".into(),
            kind: TaskKind::Bulk,
            target: None,
            parent: None,
            context: Vec::new(),
        }];
        let results = sched.schedule(&specs).await.expect("schedule");
        let r = &results[0];
        assert!(r.result.is_ok());
        assert_eq!(r.attempts.len(), 3);
        assert_eq!(r.attempts[0].agent_id, "worker-a");
        assert_eq!(r.attempts[0].status, TaskStatus::Failed);
        assert_eq!(r.attempts[1].agent_id, "worker-a");
        assert_eq!(r.attempts[1].status, TaskStatus::Failed);
        assert_eq!(r.attempts[2].agent_id, "worker-b");
        assert_eq!(r.attempts[2].status, TaskStatus::Succeeded);
    }

    // T13: an explicit target overrides the tier routing.
    #[tokio::test]
    async fn user_override_targets_a_specific_agent() {
        let drivers = BTreeMap::from([
            ("worker-a".to_string(), ok_driver("worker-a")),
            ("worker-b".to_string(), ok_driver("worker-b")),
        ]);
        let mut sched = Scheduler::new(trio_registry(), drivers, MemBoard::default(), 1);
        let specs = vec![TaskSpec {
            objective: "job".into(),
            kind: TaskKind::Bulk,
            target: Some("worker-b".into()),
            parent: None,
            context: Vec::new(),
        }];
        let results = sched.schedule(&specs).await.expect("schedule");
        // The override wins: the first (and only) attempt is on worker-b.
        assert_eq!(results[0].attempts[0].agent_id, "worker-b");
        assert_eq!(results[0].attempts[0].status, TaskStatus::Succeeded);
    }
}
