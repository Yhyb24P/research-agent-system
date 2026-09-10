//! Agent registry and deterministic routing.

use std::collections::{BTreeMap, BTreeSet};

use async_trait::async_trait;

/// The capability/cost tier of a team Agent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentTier {
    Reasoner,
    Worker,
    Utility,
}

/// The kind of work a task represents. Each kind routes to a capability tier.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskKind {
    Reasoning,
    Review,
    Bulk,
    Tool,
    Utility,
}

impl TaskKind {
    /// The capability tier this kind of work needs.
    pub fn tier(self) -> AgentTier {
        match self {
            TaskKind::Reasoning | TaskKind::Review => AgentTier::Reasoner,
            TaskKind::Bulk | TaskKind::Tool => AgentTier::Worker,
            TaskKind::Utility => AgentTier::Utility,
        }
    }

    /// The durable string form.
    pub fn as_str(self) -> &'static str {
        match self {
            TaskKind::Reasoning => "reasoning",
            TaskKind::Review => "review",
            TaskKind::Bulk => "bulk",
            TaskKind::Tool => "tool",
            TaskKind::Utility => "utility",
        }
    }

    /// Restore a kind from its string form.
    pub fn restore(s: &str) -> Option<Self> {
        Some(match s {
            "reasoning" => TaskKind::Reasoning,
            "review" => TaskKind::Review,
            "bulk" => TaskKind::Bulk,
            "tool" => TaskKind::Tool,
            "utility" => TaskKind::Utility,
            _ => return None,
        })
    }
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
    pub kind: TaskKind,
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
/// does not become a workflow engine. `Send + Sync` so a driver can be shared
/// across concurrently scheduled tasks.
#[async_trait]
pub trait AgentDriver: Send + Sync {
    /// Run `task` and return its result.
    async fn run_task(&self, task: AgentTask) -> Result<AgentTaskResult, String>;
}

/// A validation error from building an [`AgentRegistry`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RegistryError {
    /// Two agents share an id.
    DuplicateId(String),
    /// An agent has `max_concurrency == 0`.
    ZeroConcurrency(String),
    /// No agent exists for a required tier.
    MissingTier(AgentTier),
}

/// A validated set of team Agents with deterministic, reproducible routing.
///
/// Agents are keyed by id in a `BTreeMap`, so iteration is stably sorted by
/// id. Routing depends only on the registry and its inputs, never on transient
/// completion order, so the same inputs always yield the same assignment.
#[derive(Debug, Clone)]
pub struct AgentRegistry {
    agents: BTreeMap<String, AgentConfig>,
    default: Option<String>,
}

impl AgentRegistry {
    /// Build a registry from `agents`, validating the invariants: unique ids,
    /// `max_concurrency > 0`, and at least one agent per tier
    /// (Reasoner/Worker/Utility).
    pub fn new(agents: Vec<AgentConfig>) -> Result<Self, RegistryError> {
        let mut seen = BTreeSet::new();
        for a in &agents {
            if !seen.insert(a.id.as_str()) {
                return Err(RegistryError::DuplicateId(a.id.clone()));
            }
        }
        let mut map = BTreeMap::new();
        for a in agents {
            if a.max_concurrency == 0 {
                return Err(RegistryError::ZeroConcurrency(a.id.clone()));
            }
            map.insert(a.id.clone(), a);
        }
        for tier in [AgentTier::Reasoner, AgentTier::Worker, AgentTier::Utility] {
            if !map.values().any(|c| c.tier == tier) {
                return Err(RegistryError::MissingTier(tier));
            }
        }
        Ok(Self {
            agents: map,
            default: None,
        })
    }

    /// Designate the configured default agent id (routing rule 3).
    pub fn with_default(mut self, id: impl Into<String>) -> Self {
        self.default = Some(id.into());
        self
    }

    /// The registered agent ids, stably sorted.
    pub fn agent_ids(&self) -> Vec<&str> {
        self.agents.keys().map(|s| s.as_str()).collect()
    }

    /// The config for `id`, if registered.
    pub fn get(&self, id: &str) -> Option<&AgentConfig> {
        self.agents.get(id)
    }

    /// Route a task to an agent id by fixed, reproducible priority:
    /// 1. an explicit target that names a registered agent;
    /// 2. the lowest-id agent of the task kind's tier;
    /// 3. the configured default;
    /// 4. the lowest-id agent overall.
    pub fn route(&self, kind: TaskKind, target: Option<&str>) -> Option<&str> {
        // Rule 1: an explicit target that names a registered agent wins.
        // Return the registry's stored id so every path borrows from `self`.
        if let Some(t) = target {
            if let Some(cfg) = self.agents.get(t) {
                return Some(cfg.id.as_str());
            }
        }
        // Rule 2: the lowest-id agent of the task kind's tier (stable sort).
        let tier = kind.tier();
        if let Some(id) = self
            .agents
            .iter()
            .filter(|(_, c)| c.tier == tier)
            .map(|(id, _)| id.as_str())
            .next()
        {
            return Some(id);
        }
        // Rule 3: the configured default, if it names a registered agent.
        if let Some(d) = self
            .default
            .as_deref()
            .filter(|d| self.agents.contains_key(*d))
        {
            return Some(d);
        }
        // Rule 4: the lowest-id agent overall (stable sort).
        self.agents.keys().next().map(|s| s.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(id: &str, tier: AgentTier) -> AgentConfig {
        AgentConfig {
            id: id.into(),
            name: id.into(),
            tier,
            tags: Vec::new(),
            max_concurrency: 2,
        }
    }

    /// One agent per tier (T01: at least one Reasoner and one local Worker).
    fn trio() -> AgentRegistry {
        AgentRegistry::new(vec![
            cfg("reasoner-a", AgentTier::Reasoner),
            cfg("worker-a", AgentTier::Worker),
            cfg("utility-a", AgentTier::Utility),
        ])
        .expect("a full tier set is valid")
    }

    // T01: a full tier set validates; a missing tier is rejected.
    #[test]
    fn registry_validates_a_full_tier_set() {
        let reg = trio();
        assert_eq!(reg.agent_ids(), vec!["reasoner-a", "utility-a", "worker-a"]);
        let err = AgentRegistry::new(vec![
            cfg("reasoner-a", AgentTier::Reasoner),
            cfg("worker-a", AgentTier::Worker),
        ]);
        assert!(matches!(
            err,
            Err(RegistryError::MissingTier(AgentTier::Utility))
        ));
    }

    #[test]
    fn registry_rejects_duplicate_ids() {
        let err = AgentRegistry::new(vec![
            cfg("a", AgentTier::Reasoner),
            cfg("a", AgentTier::Worker),
            cfg("u", AgentTier::Utility),
        ]);
        assert!(matches!(err, Err(RegistryError::DuplicateId(_))));
    }

    #[test]
    fn registry_rejects_zero_concurrency() {
        let mut a = cfg("reasoner-a", AgentTier::Reasoner);
        a.max_concurrency = 0;
        let err = AgentRegistry::new(vec![
            a,
            cfg("worker-a", AgentTier::Worker),
            cfg("utility-a", AgentTier::Utility),
        ]);
        assert!(matches!(err, Err(RegistryError::ZeroConcurrency(_))));
    }

    // T04: reasoning (and review) route to a Reasoner.
    #[test]
    fn reasoning_routes_to_reasoner() {
        let reg = trio();
        assert_eq!(reg.route(TaskKind::Reasoning, None), Some("reasoner-a"));
        assert_eq!(reg.route(TaskKind::Review, None), Some("reasoner-a"));
    }

    // T05: bulk/tool work routes to a Worker; utility work to a Utility.
    #[test]
    fn bulk_tool_route_to_worker_and_utility() {
        let reg = trio();
        assert_eq!(reg.route(TaskKind::Bulk, None), Some("worker-a"));
        assert_eq!(reg.route(TaskKind::Tool, None), Some("worker-a"));
        assert_eq!(reg.route(TaskKind::Utility, None), Some("utility-a"));
    }

    // T13: an explicit user target overrides the tier routing.
    #[test]
    fn explicit_target_overrides_tier_routing() {
        let reg = trio();
        // A Reasoning task normally routes to the Reasoner, but an explicit
        // target wins.
        assert_eq!(
            reg.route(TaskKind::Reasoning, Some("worker-a")),
            Some("worker-a")
        );
        // An unknown target falls through to the tier routing.
        assert_eq!(
            reg.route(TaskKind::Reasoning, Some("ghost")),
            Some("reasoner-a")
        );
    }

    // Rule 4: with two agents of the same tier, the lowest id wins,
    // reproducibly.
    #[test]
    fn routing_is_stable_by_id() {
        let reg = AgentRegistry::new(vec![
            cfg("reasoner-b", AgentTier::Reasoner),
            cfg("reasoner-a", AgentTier::Reasoner),
            cfg("worker-a", AgentTier::Worker),
            cfg("utility-a", AgentTier::Utility),
        ])
        .expect("two reasoners are valid");
        assert_eq!(reg.route(TaskKind::Reasoning, None), Some("reasoner-a"));
    }
}
