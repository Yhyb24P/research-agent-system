//! Heterogeneous Agent team layer: registry, task board, scheduling, and the
//! Lead loop.
//!
//! R1 defined the driver contract and Agent shapes. R5 adds the registry and
//! deterministic routing (commit 1), the durable task board and result flow
//! (commit 2), concurrent scheduling with retry/reassignment (commit 3), and
//! the Lead plan-follow-up-synthesis loop (commit 4).

mod board;
mod registry;

pub use board::{
    AgentMessage, ArtifactMeta, BoardError, TaskAttempt, TaskBoard, TaskRecord, TaskStatus,
};
pub use registry::{
    AgentConfig, AgentDriver, AgentRegistry, AgentTask, AgentTaskResult, AgentTier, RegistryError,
    TaskKind,
};
