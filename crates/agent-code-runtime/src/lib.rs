//! The native Coding Agent runtime: a recoverable loop that turns model
//! decisions into contained tool calls and a durable, bounded history.
//!
//! This crate is the composition root: it drives the core state machine,
//! consults a model client, dispatches the five tools on a contained
//! workspace, and records every decision and result to a durable store. It
//! composes the other crates; it adds no second tool loop and no control
//! plane.

mod agent;
mod dispatch;

pub use agent::{AgentConfig, AgentError, AgentLoop, Delivery};
pub use dispatch::{dispatch, observations_for, ToolOutcome};
