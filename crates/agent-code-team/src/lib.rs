//! Heterogeneous Agent team layer: registry, lead, task board and result flow.
//!
//! R1 defines the driver contract and Agent shapes. The scheduler, task board
//! and result flow land in R5.

use async_trait::async_trait;

/// The capability/cost tier of a team Agent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentTier {
    Reasoner,
    Worker,
    Utility,
}

/// Configuration for a team Agent.
#[derive(Debug, Clone)]
pub struct AgentConfig {
    pub id: String,
    pub name: String,
    pub tier: AgentTier,
    pub tags: Vec<String>,
    pub max_concurrency: usize,
}

/// A unit of work delegated to an Agent.
#[derive(Debug, Clone)]
pub struct AgentTask {
    pub id: u64,
    pub objective: String,
    pub context: Vec<String>,
}

/// The result an Agent returns for a task.
#[derive(Debug, Clone)]
pub struct AgentTaskResult {
    pub task_id: u64,
    pub summary: String,
    pub artifacts: Vec<String>,
}

/// Runs a task on some backend. The team layer moves work between Agents; it
/// does not become a workflow engine.
#[async_trait]
pub trait AgentDriver: Send {
    /// Run `task` and return its result.
    async fn run_task(&self, task: AgentTask) -> Result<AgentTaskResult, String>;
}
