//! N12: compacting the model-facing context never deletes or alters the
//! durable observation history. Proven across a close/reopen of the database.

use agent_code_context::{
    build_compact_context, compact_older, serialize_context, BytesTokenCounter, ContextBudget,
    TokenCounter,
};
use agent_code_core::{AgentState, Journal, SessionId};
use agent_code_model::Observation;
use agent_code_storage::SqliteJournal;
use rusqlite::Connection;

#[test]
fn compact_context_preserves_durable_history() {
    let path = std::env::temp_dir().join(format!("agent_n12_{}.db", std::process::id()));
    let _ = std::fs::remove_file(&path);
    let session = SessionId::new("s");

    // Append a long history, then close the database.
    {
        let conn = Connection::open(&path).expect("open db");
        let mut j = SqliteJournal::open(conn, session.clone()).expect("bind");
        j.create(AgentState::Initializing).expect("create");
        for i in 0..50 {
            j.append_observation(&Observation::Command {
                program: "cargo".into(),
                argv: vec![format!("step-{i}")],
                exit: Some(0),
            })
            .expect("append command");
        }
        for i in 0..5 {
            j.append_observation(&Observation::Error {
                signature: format!("E{i:04}"),
                count: 1,
            })
            .expect("append error");
        }
    }

    // Reopen: build a compact context under a tight budget.
    let conn = Connection::open(&path).expect("reopen db");
    let j = SqliteJournal::open(conn, session.clone()).expect("bind");
    let budget = ContextBudget::new(120, 0);
    let counter = BytesTokenCounter::default();
    let ctx = build_compact_context(&j, &session, "the task", "", "", &budget, &counter)
        .expect("build compact context");

    // N11: the assembled context fits the budget.
    assert!(counter.count(&serialize_context(&ctx)) <= budget.free_tokens());

    // N12: the durable history is intact — count and content unchanged.
    let durable = j.read_observations().expect("read durable");
    assert_eq!(durable.len(), 55);
    assert!(durable
        .iter()
        .any(|o| matches!(o, Observation::Command { exit: Some(0), .. })));
    assert!(durable
        .iter()
        .any(|o| matches!(o, Observation::Error { signature, .. } if signature == "E0004")));

    // The kept observations are the newest suffix; the older prefix compacts
    // into a summary that retains the required fields (not silent deletion).
    let older = &durable[..durable.len() - ctx.observations.len()];
    let summary = compact_older(older);
    assert!(summary.contains("older observations"));
    assert!(summary.contains("cargo"));

    let _ = std::fs::remove_file(&path);
}
