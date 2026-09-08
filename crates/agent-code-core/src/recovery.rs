use crate::error::JournalError;
use crate::journal::Journal;
use crate::state::{ToolCallId, ToolCallState};

/// Recover a crashed session from its journal. A tool call left in `Running`
/// is not assumed replay-safe, so it is marked `Interrupted` and will never be
/// blindly re-executed.
pub fn recover_interrupted_tools<J: Journal>(
    journal: &mut J,
) -> Result<Vec<ToolCallId>, JournalError> {
    let running = journal.running_tools();
    for call in &running {
        journal.record_tool_state(*call, ToolCallState::Interrupted)?;
    }
    Ok(running)
}
