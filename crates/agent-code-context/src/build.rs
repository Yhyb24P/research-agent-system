//! Budget-bounded assembly of a model-facing context.

use agent_code_model::{ModelContext, Observation};

use crate::budget::{truncate_chars, ContextBudget, ContextError, TokenCounter};

/// Section labels. Every label and separator is billed, so the assembled
/// context can never exceed the budget because of uncounted framing.
const TASK: &str = "[task]\n";
const RULES: &str = "[rules]\n";
const MAP: &str = "[repo-map]\n";
const COMPACT: &str = "[compacted]\n";
const OBS: &str = "[observations]\n";

/// The inputs to a model-facing context. `observations` are the recent,
/// already-selected ones; `compact_summary` is a pre-computed compaction of
/// older ones. Empty strings mark an absent section.
pub struct ContextSpec<'a> {
    pub task: &'a str,
    pub project_rules: &'a str,
    pub repository_map: &'a str,
    pub compact_summary: &'a str,
    pub observations: &'a [Observation],
}

/// The exact text the model would receive.
pub fn serialize_context(ctx: &ModelContext) -> String {
    let mut out = String::new();
    out.push_str(&format!("{TASK}{task}\n", task = ctx.task));
    if !ctx.project_rules.is_empty() {
        out.push_str(&format!("{RULES}{rules}\n", rules = ctx.project_rules));
    }
    if !ctx.repository_map.is_empty() {
        out.push_str(&format!("{MAP}{map}\n", map = ctx.repository_map));
    }
    if !ctx.compact_summary.is_empty() {
        out.push_str(&format!(
            "{COMPACT}{compact}\n",
            compact = ctx.compact_summary
        ));
    }
    if !ctx.observations.is_empty() {
        out.push_str(OBS);
        for o in &ctx.observations {
            out.push_str(&o.render());
            out.push('\n');
        }
    }
    out
}

/// Assemble a [`ModelContext`] that stays within `budget`, billing every
/// section in priority order: task, rules, map, recent observations, then the
/// compact summary. Lower-priority sections are truncated to fit or dropped.
/// Fails with [`ContextError::BudgetTooSmall`] when even the mandatory task
/// section and its marker do not fit.
pub fn build_context(
    spec: &ContextSpec<'_>,
    budget: &ContextBudget,
    counter: &dyn TokenCounter,
) -> Result<ModelContext, ContextError> {
    let free = budget.free_tokens();
    let mut ctx = ModelContext::default();

    // 1. task: mandatory.
    let task = truncate_chars(spec.task, budget.task_cap as usize);
    let task_cost = counter.count(&format!("{TASK}{task}\n"));
    if task_cost > free {
        return Err(ContextError::BudgetTooSmall);
    }
    ctx.task = task;
    let mut used = task_cost;

    // 2. project rules.
    if !spec.project_rules.is_empty() {
        if let Some((content, cost)) = fit_section(
            RULES,
            spec.project_rules,
            budget.rules_cap,
            free.saturating_sub(used),
            counter,
        ) {
            ctx.project_rules = content;
            used += cost;
        }
    }

    // 3. repository map.
    if !spec.repository_map.is_empty() {
        if let Some((content, cost)) = fit_section(
            MAP,
            spec.repository_map,
            budget.map_cap,
            free.saturating_sub(used),
            counter,
        ) {
            ctx.repository_map = content;
            used += cost;
        }
    }

    // 4. recent observations: newest first, each capped, until the budget
    //    runs out. The shared label is billed once.
    let mut picked: Vec<(Observation, String)> = Vec::new();
    for obs in spec.observations.iter().rev() {
        let rendered = truncate_chars(&obs.render(), budget.observation_cap as usize);
        let mut lines: Vec<&str> = picked.iter().map(|(_, r)| r.as_str()).collect();
        lines.push(&rendered);
        let block = format!("{OBS}{}\n", lines.join("\n"));
        if counter.count(&block) > free.saturating_sub(used) {
            break;
        }
        picked.push((obs.clone(), rendered));
    }
    picked.reverse();
    if !picked.is_empty() {
        let block = format!(
            "{OBS}{}\n",
            picked
                .iter()
                .map(|(_, r)| r.as_str())
                .collect::<Vec<_>>()
                .join("\n")
        );
        used += counter.count(&block);
        ctx.observations = picked.into_iter().map(|(o, _)| o).collect();
    }

    // 5. compact summary of older observations: lowest priority.
    if !spec.compact_summary.is_empty() {
        if let Some((content, _)) = fit_section(
            COMPACT,
            spec.compact_summary,
            u32::MAX,
            free.saturating_sub(used),
            counter,
        ) {
            ctx.compact_summary = content;
        }
    }

    Ok(ctx)
}

/// The largest char-prefix of `content` (capped to `cap` chars) whose
/// serialized section (`label` + prefix + newline) fits in `remaining`
/// tokens. Returns the prefix and its cost, or `None` if the label alone does
/// not fit.
fn fit_section(
    label: &str,
    content: &str,
    cap: u32,
    remaining: u32,
    counter: &dyn TokenCounter,
) -> Option<(String, u32)> {
    if counter.count(&format!("{label}\n")) > remaining {
        return None;
    }
    let capped = truncate_chars(content, cap as usize);
    let total = capped.chars().count();
    let mut lo = 0usize;
    let mut hi = total;
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        let prefix: String = capped.chars().take(mid).collect();
        if counter.count(&format!("{label}{prefix}\n")) <= remaining {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    let prefix: String = capped.chars().take(lo).collect();
    let cost = counter.count(&format!("{label}{prefix}\n"));
    Some((prefix, cost))
}

#[cfg(test)]
mod tests {
    use agent_code_model::Observation;

    use super::*;
    use crate::budget::{BytesTokenCounter, ContextBudget, ContextError};

    fn counter() -> BytesTokenCounter {
        BytesTokenCounter::default()
    }

    fn spec<'a>(task: &'a str, obs: &'a [Observation]) -> ContextSpec<'a> {
        ContextSpec {
            task,
            project_rules: "",
            repository_map: "",
            compact_summary: "",
            observations: obs,
        }
    }

    #[test]
    fn assembled_context_stays_within_budget() {
        let c = counter();
        let b = ContextBudget::new(200, 20);
        let obs = [
            Observation::Command {
                program: "cargo".into(),
                argv: vec!["test".into()],
                exit: Some(0),
            },
            Observation::Error {
                signature: "E0308".into(),
                count: 1,
            },
        ];
        let ctx = build_context(&spec("fix the bug", &obs), &b, &c).unwrap();
        // N11: the exact serialized context fits the free budget.
        assert!(c.count(&serialize_context(&ctx)) <= b.free_tokens());
    }

    #[test]
    fn budget_too_small_for_task_errors() {
        let c = counter();
        let b = ContextBudget::new(4, 0);
        let err =
            build_context(&spec("a very long task that cannot fit", &[]), &b, &c).unwrap_err();
        assert!(matches!(err, ContextError::BudgetTooSmall));
    }

    #[test]
    fn task_is_always_present() {
        let c = counter();
        let b = ContextBudget::new(50, 0);
        let ctx = build_context(&spec("the task", &[]), &b, &c).unwrap();
        assert_eq!(ctx.task, "the task");
    }

    #[test]
    fn newest_observations_preferred_under_tight_budget() {
        let c = counter();
        let b = ContextBudget::new(60, 0);
        let obs = [
            Observation::Text("old-observation".into()),
            Observation::Text("new-observation".into()),
        ];
        let ctx = build_context(&spec("task", &obs), &b, &c).unwrap();
        // Under a tight budget the newest observation is kept.
        assert!(ctx
            .observations
            .iter()
            .any(|o| matches!(o, Observation::Text(t) if t.contains("new"))));
        assert!(c.count(&serialize_context(&ctx)) <= b.free_tokens());
    }

    #[test]
    fn map_cannot_eat_the_whole_budget() {
        let c = counter();
        let mut b = ContextBudget::new(80, 0);
        b.map_cap = 10; // a tiny map cap
        let s = ContextSpec {
            task: "task",
            project_rules: "",
            repository_map: "a very long repository map that exceeds its cap",
            compact_summary: "",
            observations: &[Observation::Text("kept".into())],
        };
        let ctx = build_context(&s, &b, &c).unwrap();
        // The map is truncated to its cap, and the observation still fits.
        assert!(ctx.repository_map.chars().count() <= 10);
        assert!(ctx
            .observations
            .iter()
            .any(|o| matches!(o, Observation::Text(t) if t == "kept")));
    }

    #[test]
    fn compact_summary_has_lowest_priority() {
        let c = counter();
        let b = ContextBudget::new(60, 0);
        let s = ContextSpec {
            task: "task",
            project_rules: "",
            repository_map: "",
            compact_summary: "a long compact summary of older observations",
            observations: &[Observation::Text("recent".into())],
        };
        let ctx = build_context(&s, &b, &c).unwrap();
        // The recent observation is kept; the compact summary is dropped or
        // truncated to fit what remains.
        assert!(ctx
            .observations
            .iter()
            .any(|o| matches!(o, Observation::Text(t) if t == "recent")));
        assert!(c.count(&serialize_context(&ctx)) <= b.free_tokens());
    }

    #[test]
    fn rules_and_map_reach_the_model_context() {
        // N01 closed loop: loaded rules and the repository map are present in
        // the model-facing projection, not just loadable.
        let c = counter();
        let b = ContextBudget::new(400, 0);
        let s = ContextSpec {
            task: "task",
            project_rules: "## AGENTS.md\nbe precise",
            repository_map: "# repo map\nmain.rs\nsrc/a.rs",
            compact_summary: "",
            observations: &[],
        };
        let ctx = build_context(&s, &b, &c).unwrap();
        assert!(ctx.project_rules.contains("be precise"));
        assert!(ctx.repository_map.contains("main.rs"));
    }
}
