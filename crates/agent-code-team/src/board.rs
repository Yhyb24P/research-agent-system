//! The durable task board: tasks, attempts, messages, and artifacts.
//!
//! The board is the team's memory. Results, artifacts, and directed messages
//! are persisted here so they flow between Agents without a human copying
//! anything (T16). The trait is pure (no storage dependency); the SQLite
//! implementation lives in the storage crate, which avoids a dependency cycle.

use crate::registry::TaskKind;

/// The lifecycle state of a team task.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskStatus {
    Pending,
    Assigned,
    Running,
    Succeeded,
    Failed,
}

impl TaskStatus {
    /// The durable string form.
    pub fn as_str(self) -> &'static str {
        match self {
            TaskStatus::Pending => "pending",
            TaskStatus::Assigned => "assigned",
            TaskStatus::Running => "running",
            TaskStatus::Succeeded => "succeeded",
            TaskStatus::Failed => "failed",
        }
    }

    /// Restore a status from its string form.
    pub fn restore(s: &str) -> Option<Self> {
        Some(match s {
            "pending" => TaskStatus::Pending,
            "assigned" => TaskStatus::Assigned,
            "running" => TaskStatus::Running,
            "succeeded" => TaskStatus::Succeeded,
            "failed" => TaskStatus::Failed,
            _ => return None,
        })
    }
}

/// A durable team task.
#[derive(Debug, Clone)]
pub struct TaskRecord {
    pub id: u64,
    pub objective: String,
    pub parent_task: Option<u64>,
    pub kind: TaskKind,
    /// The explicit user target, if any (T13).
    pub target: Option<String>,
    /// The agent that was actually assigned.
    pub assignee: Option<String>,
    pub status: TaskStatus,
}

/// A single attempt/run of a task. Each attempt is persisted independently, so
/// a failure is never overwritten by a later retry (T11).
#[derive(Debug, Clone)]
pub struct TaskAttempt {
    pub task_id: u64,
    pub attempt: u32,
    pub agent_id: String,
    pub status: TaskStatus,
    pub result: Option<String>,
    pub error: Option<String>,
}

/// A directed message between two Agents. It reaches only the target's
/// context (T09).
#[derive(Debug, Clone)]
pub struct AgentMessage {
    pub from_agent: String,
    pub to_agent: String,
    pub body: String,
}

/// Artifact metadata: a path and its content hash.
#[derive(Debug, Clone)]
pub struct ArtifactMeta {
    pub path: String,
    pub sha256: String,
}

/// An error from the task board.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BoardError {
    /// An operation named a task that does not exist.
    UnknownTask(u64),
    /// A storage failure.
    Storage(String),
}

/// The durable task board.
///
/// Implementations persist tasks, attempts, messages, and artifacts so that a
/// worker's result, artifact, and directed message reach the right Agent's
/// next context (T07/T08/T09) and survive a restart.
pub trait TaskBoard {
    /// Create a task and return its id.
    fn create_task(
        &mut self,
        objective: &str,
        parent: Option<u64>,
        kind: TaskKind,
        target: Option<String>,
    ) -> Result<u64, BoardError>;
    /// Assign a task to `agent` (Pending -> Assigned).
    fn assign(&mut self, task: u64, agent: &str) -> Result<(), BoardError>;
    /// Move a task to a new lifecycle status.
    fn set_status(&mut self, task: u64, status: TaskStatus) -> Result<(), BoardError>;
    /// Persist one attempt (independently; failures are kept).
    fn record_attempt(&mut self, attempt: &TaskAttempt) -> Result<(), BoardError>;
    /// Move a running attempt to its terminal status, updating the row that
    /// was persisted as Running. This is what makes the lifecycle durable: the
    /// attempt is observable as Running before the driver runs, then settles to
    /// Succeeded/Failed without being overwritten by a later retry.
    fn complete_attempt(&mut self, attempt: &TaskAttempt) -> Result<(), BoardError>;
    /// Persist a directed message.
    fn record_message(&mut self, message: &AgentMessage) -> Result<(), BoardError>;
    /// Persist artifact metadata for a task.
    fn record_artifact(&mut self, task: u64, artifact: &ArtifactMeta) -> Result<(), BoardError>;
    /// Read a task.
    fn task(&self, id: u64) -> Result<Option<TaskRecord>, BoardError>;
    /// Read all attempts for a task, in attempt order.
    fn attempts(&self, task: u64) -> Result<Vec<TaskAttempt>, BoardError>;
    /// Read the messages addressed to `agent` (T09).
    fn messages_to(&self, agent: &str) -> Result<Vec<AgentMessage>, BoardError>;
    /// Read the artifact metadata for a task (T08).
    fn artifacts(&self, task: u64) -> Result<Vec<ArtifactMeta>, BoardError>;
    /// List every task id, in creation order. Used to reconstruct the task
    /// tree (and thus the final result) from the durable board.
    fn task_ids(&self) -> Result<Vec<u64>, BoardError>;
}
