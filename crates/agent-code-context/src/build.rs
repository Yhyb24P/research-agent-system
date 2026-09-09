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
        for line in &ctx.observations {
            out.push_str(line);
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

    // 1. task: mandatory, but dynamically sized to fit both task_cap and the
    //    free budget. Only if even the section framing does not fit do we fail.
    let mut used = match fit_section(TASK, spec.task, budget.task_cap, free, counter) {
        Some((content, cost)) => {
            ctx.task = content;
            cost
        }
        None => return Err(ContextError::BudgetTooSmall),
    };

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
    //    runs out. The shared label is billed once. We store the exact
    //    bounded text that was billed, so serialization cannot exceed it.
    let mut picked: Vec<String> = Vec::new();
    for obs in spec.observations.iter().rev() {
        let rendered = truncate_chars(&obs.render(), budget.observation_cap as usize);
        let mut lines = picked.clone();
        lines.push(rendered.clone());
        let block = format!("{OBS}{}\n", lines.join("\n"));
        if counter.count(&block) > free.saturating_sub(used) {
            break;
        }
        picked.push(rendered);
    }
    picked.reverse();
    if !picked.is_empty() {
        let block = format!("{OBS}{}\n", picked.join("\n"));
        used += counter.count(&block);
        ctx.observations = picked;
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

/// The largest head/tail-truncated body of `content` (within `cap` chars)
/// whose serialized section (`label` + body + newline) fits in `remaining`
/// tokens. The body keeps both ends with a `…` marker when truncated, so a
/// second, budget-driven truncation never silently drops the tail. Returns
/// the body and its cost, or `None` if the section framing alone does not
/// fit.
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
    // Largest char budget S (<= cap) whose head/tail-truncated body fits.
    let mut lo = 0usize;
    let mut hi = cap as usize;
    while lo < hi {
        let mid = (lo + hi).div_ceil(2);
        let body = truncate_chars(content, mid);
        if counter.count(&format!("{label}{body}\n")) <= remaining {
            lo = mid;
        } else {
            hi = mid - 1;
        }
    }
    let body = truncate_chars(content, lo);
    let cost = counter.count(&format!("{label}{body}\n"));
    Some((body, cost))
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
        // Even the task section's framing ("[task]\n" + newline ≈ 3 tokens)
        // does not fit, so there is no room for any task content.
        let c = counter();
        let b = ContextBudget::new(2, 0);
        let err =
            build_context(&spec("a very long task that cannot fit", &[]), &b, &c).unwrap_err();
        assert!(matches!(err, ContextError::BudgetTooSmall));
    }

    #[test]
    fn task_truncates_to_fit_instead_of_erroring() {
        // A task longer than the budget is head/tail truncated to fit, not an
        // error: only a budget that cannot hold the framing fails.
        let c = counter();
        let b = ContextBudget::new(10, 0);
        let ctx = build_context(&spec("a very long task that cannot fit", &[]), &b, &c).unwrap();
        assert!(ctx.task.chars().count() < "a very long task that cannot fit".chars().count());
        assert!(ctx.task.contains('…'));
        assert!(c.count(&serialize_context(&ctx)) <= b.free_tokens());
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
        assert!(ctx.observations.iter().any(|s| s.contains("new")));
        assert!(c.count(&serialize_context(&ctx)) <= b.free_tokens());
    }

    #[test]
    fn a_very_long_observation_is_stored_bounded() {
        // An observation far larger than observation_cap is stored in its
        // bounded, billed form (head/tail truncated to the cap), not the full
        // original — so the serialized context can never exceed the budget
        // because of uncounted re-rendering (N11).
        let c = counter();
        let b = ContextBudget::new(1000, 0);
        let huge = "x".repeat(10_000);
        let obs = [Observation::Text(huge)];
        let ctx = build_context(&spec("task", &obs), &b, &c).unwrap();
        assert_eq!(ctx.observations.len(), 1);
        let stored = &ctx.observations[0];
        // Exactly the bounded cap, not the 10k-character original.
        assert_eq!(stored.chars().count(), b.observation_cap as usize);
        assert!(stored.chars().count() < 10_000);
        // N11: the exact serialized context (with the bounded obs) fits.
        assert!(c.count(&serialize_context(&ctx)) <= b.free_tokens());
    }

    #[test]
    fn a_very_long_observation_that_cannot_fit_is_dropped() {
        // When even the bounded observation does not fit the remaining budget
        // it is dropped; the context still respects the budget (N11).
        let c = counter();
        let b = ContextBudget::new(80, 0);
        let obs = [Observation::Text("x".repeat(10_000))];
        let ctx = build_context(&spec("task", &obs), &b, &c).unwrap();
        assert!(ctx.observations.is_empty());
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
        assert!(ctx.observations.iter().any(|s| s == "kept"));
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
        assert!(ctx.observations.iter().any(|s| s == "recent"));
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
