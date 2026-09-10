//! Shared test helpers for the team crate: an in-memory board and fixed
//! drivers. Kept out of the public API; used by the scheduler and Lead tests.

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;

use crate::board::{
    AgentMessage, ArtifactMeta, BoardError, TaskAttempt, TaskBoard, TaskRecord, TaskStatus,
};
use crate::registry::{
    AgentConfig, AgentDriver, AgentRegistry, AgentTask, AgentTaskResult, AgentTier, TaskKind,
};

/// One agent per tier, plus a second worker for reassignment tests.
pub fn trio_registry() -> AgentRegistry {
    AgentRegistry::new(vec![
        cfg("reasoner-a", AgentTier::Reasoner),
        cfg("worker-a", AgentTier::Worker),
        cfg("worker-b", AgentTier::Worker),
        cfg("utility-a", AgentTier::Utility),
    ])
    .expect("a full tier set is valid")
}

fn cfg(id: &str, tier: AgentTier) -> AgentConfig {
    AgentConfig {
        id: id.into(),
        name: id.into(),
        tier,
        tags: Vec::new(),
        max_concurrency: 2,
    }
}

/// A driver with a fixed outcome.
pub struct FixedDriver {
    pub summary: Option<String>,
    pub error: Option<String>,
}

#[async_trait]
impl AgentDriver for FixedDriver {
    async fn run_task(&self, task: AgentTask) -> Result<AgentTaskResult, String> {
        if let Some(e) = &self.error {
            return Err(e.clone());
        }
        Ok(AgentTaskResult {
            task_id: task.id,
            summary: self.summary.clone().unwrap_or_else(|| "ok".into()),
            artifacts: Vec::new(),
        })
    }
}

/// A driver that always succeeds with `summary`.
pub fn ok_driver(summary: &str) -> Arc<dyn AgentDriver> {
    Arc::new(FixedDriver {
        summary: Some(summary.into()),
        error: None,
    })
}

/// A driver that always fails with `error`.
pub fn err_driver(error: &str) -> Arc<dyn AgentDriver> {
    Arc::new(FixedDriver {
        summary: None,
        error: Some(error.into()),
    })
}

/// A minimal in-memory board, so the team crate's tests need no storage
/// dependency (which would be a cycle).
#[derive(Default)]
pub struct MemBoard {
    pub tasks: BTreeMap<u64, TaskRecord>,
    pub attempts: BTreeMap<u64, Vec<TaskAttempt>>,
    pub messages: Vec<AgentMessage>,
    pub artifacts: BTreeMap<u64, Vec<ArtifactMeta>>,
    next_id: u64,
}

impl TaskBoard for MemBoard {
    fn create_task(
        &mut self,
        objective: &str,
        parent: Option<u64>,
        kind: TaskKind,
        target: Option<String>,
    ) -> Result<u64, BoardError> {
        self.next_id += 1;
        let id = self.next_id;
        self.tasks.insert(
            id,
            TaskRecord {
                id,
                objective: objective.to_string(),
                parent_task: parent,
                kind,
                target,
                assignee: None,
                status: TaskStatus::Pending,
            },
        );
        Ok(id)
    }
    fn assign(&mut self, task: u64, agent: &str) -> Result<(), BoardError> {
        let t = self
            .tasks
            .get_mut(&task)
            .ok_or(BoardError::UnknownTask(task))?;
        t.assignee = Some(agent.to_string());
        t.status = TaskStatus::Assigned;
        Ok(())
    }
    fn set_status(&mut self, task: u64, status: TaskStatus) -> Result<(), BoardError> {
        let t = self
            .tasks
            .get_mut(&task)
            .ok_or(BoardError::UnknownTask(task))?;
        t.status = status;
        Ok(())
    }
    fn record_attempt(&mut self, attempt: &TaskAttempt) -> Result<(), BoardError> {
        self.attempts
            .entry(attempt.task_id)
            .or_default()
            .push(attempt.clone());
        Ok(())
    }
    fn record_message(&mut self, message: &AgentMessage) -> Result<(), BoardError> {
        self.messages.push(message.clone());
        Ok(())
    }
    fn record_artifact(&mut self, task: u64, artifact: &ArtifactMeta) -> Result<(), BoardError> {
        self.artifacts
            .entry(task)
            .or_default()
            .push(artifact.clone());
        Ok(())
    }
    fn task(&self, id: u64) -> Result<Option<TaskRecord>, BoardError> {
        Ok(self.tasks.get(&id).cloned())
    }
    fn attempts(&self, task: u64) -> Result<Vec<TaskAttempt>, BoardError> {
        Ok(self.attempts.get(&task).cloned().unwrap_or_default())
    }
    fn messages_to(&self, agent: &str) -> Result<Vec<AgentMessage>, BoardError> {
        Ok(self
            .messages
            .iter()
            .filter(|m| m.to_agent == agent)
            .cloned()
            .collect())
    }
    fn artifacts(&self, task: u64) -> Result<Vec<ArtifactMeta>, BoardError> {
        Ok(self.artifacts.get(&task).cloned().unwrap_or_default())
    }
}
