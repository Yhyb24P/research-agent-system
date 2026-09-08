//! Token accounting, the budget shape, and char-safe truncation.

/// A token budget for the model-facing context, with a character cap per
/// section so no single section can consume the whole budget.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ContextBudget {
    /// Total token budget for the context.
    pub max_tokens: u32,
    /// Tokens always reserved for the next model output / tool call.
    pub reserved_output: u32,
    /// Extra tokens held back as a safety margin.
    pub safety_margin: u32,
    /// Character cap for the task section.
    pub task_cap: u32,
    /// Character cap for the project-rules section.
    pub rules_cap: u32,
    /// Character cap for the repository-map section.
    pub map_cap: u32,
    /// Character cap for a single observation.
    pub observation_cap: u32,
}

impl ContextBudget {
    /// A budget with default section caps; `safety_margin` starts at 0.
    pub fn new(max_tokens: u32, reserved_output: u32) -> Self {
        Self {
            max_tokens,
            reserved_output,
            safety_margin: 0,
            task_cap: 4000,
            rules_cap: 4000,
            map_cap: 4000,
            observation_cap: 2000,
        }
    }

    /// Tokens available for context content.
    pub fn free_tokens(&self) -> u32 {
        self.max_tokens
            .saturating_sub(self.reserved_output)
            .saturating_sub(self.safety_margin)
    }
}

/// Measures the token cost of a text. The default byte-based estimator is
/// conservative, not a guarantee of the real model's token limit.
pub trait TokenCounter {
    fn count(&self, text: &str) -> u32;
}

/// A deterministic byte-based estimator: `ceil(bytes / divisor)`. A divisor of
/// 3 is accurate for CJK (3 UTF-8 bytes ≈ 1 token) and conservative for ASCII.
/// Model adapters can supply a real tokenizer later.
#[derive(Debug, Clone, Copy)]
pub struct BytesTokenCounter {
    divisor: u32,
}

impl BytesTokenCounter {
    pub fn new(divisor: u32) -> Self {
        Self {
            divisor: divisor.max(1),
        }
    }
}

impl Default for BytesTokenCounter {
    fn default() -> Self {
        Self::new(3)
    }
}

impl TokenCounter for BytesTokenCounter {
    fn count(&self, text: &str) -> u32 {
        if text.is_empty() {
            0
        } else {
            (text.len() as u32).div_ceil(self.divisor)
        }
    }
}

/// Errors from building a bounded context.
#[derive(Debug)]
pub enum ContextError {
    /// The budget cannot hold even the mandatory task section and its marker.
    BudgetTooSmall,
    /// A durable history source failed to read.
    History(String),
    /// A rules or repository-map file could not be read.
    Io(String),
}

impl std::fmt::Display for ContextError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::BudgetTooSmall => write!(f, "context budget too small to hold the task"),
            Self::History(m) => write!(f, "history source error: {m}"),
            Self::Io(m) => write!(f, "io error: {m}"),
        }
    }
}

impl std::error::Error for ContextError {}

/// Truncate to at most `cap` chars, keeping a head/tail split around a `…`
/// marker. Char-boundary safe.
pub fn truncate_chars(s: &str, cap: usize) -> String {
    let n = s.chars().count();
    if n <= cap {
        return s.to_string();
    }
    const MARKER: &str = "…";
    if cap <= MARKER.chars().count() {
        return s.chars().take(cap).collect();
    }
    let keep = cap - MARKER.chars().count();
    let head = keep / 2;
    let tail = keep - head;
    let chars: Vec<char> = s.chars().collect();
    let h: String = chars[..head].iter().collect();
    let t: String = chars[n - tail..].iter().collect();
    format!("{h}{MARKER}{t}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn free_tokens_subtracts_reserved_and_margin() {
        let b = ContextBudget::new(100, 20);
        assert_eq!(b.free_tokens(), 80);
        let b = ContextBudget {
            safety_margin: 10,
            ..b
        };
        assert_eq!(b.free_tokens(), 70);
    }

    #[test]
    fn byte_counter_is_conservative_and_cjk_safe() {
        let c = BytesTokenCounter::default();
        // ASCII: 3 bytes -> 1 token (conservative vs ~0.25 real).
        assert_eq!(c.count("abc"), 1);
        // CJK: 3 bytes per char -> 1 token per char (accurate).
        assert_eq!(c.count("中"), 1);
        assert_eq!(c.count(""), 0);
    }

    #[test]
    fn truncate_keeps_head_and_tail() {
        let s = "abcdefghij";
        let t = truncate_chars(s, 6);
        assert_eq!(t.chars().count(), 6);
        assert!(t.starts_with('a'));
        assert!(t.ends_with('j'));
        assert!(t.contains('…'));
    }

    #[test]
    fn truncate_is_char_boundary_safe() {
        // A multibyte char must never be split.
        let s = "中中中中中";
        let t = truncate_chars(s, 2);
        assert_eq!(t.chars().count(), 2);
    }

    #[test]
    fn truncate_short_string_is_unchanged() {
        assert_eq!(truncate_chars("hi", 5), "hi");
    }
}
