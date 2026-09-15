# S0 Context Flow & Byte Budget Audit

Pure measurement. No implementation, no `Status` / `Summary` / `Trace` design,
no renderer change. It answers what the Lead actually receives, how large each
part is in **bytes**, and what is never rendered at all.

```text
base            main@8088252ab06c23d6a1a0083dd7013e0a2867d21b
measured object LeadContext -> coded Lead prompt (both Lead brains)
data source     ONE real CodexExec-Lead heterogeneous run (case study, see 7)
                (/tmp/am-v05-s2/e2e/.agentmosaic/state.db, 4 tasks, 3 workers)
method          code reading (file:line) + SQLite read-back + a faithful
                re-implementation of the render model in the code

BYTE_BUDGET_MEASURED=true
TOKEN_COUNT_MEASURED=false
```

Bytes are not tokens. No token count was measured, and nothing here infers one
from a byte count.

## 1. What `LeadContext` contains

`crates/agentmosaic-team/src/lead.rs:53-67` defines exactly eight fields, and
`lead.rs:330-371` fills them from the board each round:

| Field | Filled from | Appears in the rendered prompt |
|---|---|---|
| `root_task_id` | the run's root | yes (header) |
| `objective` | `team_tasks.objective` | yes, L1-bounded, every round |
| `round` | Lead loop counter | yes (header) |
| `candidates` | scheduler registry agent ids | yes, unbounded by L1 |
| `results` | succeeded descendants' attempt results | yes, L1-bounded |
| `artifacts` | `artifacts` rows of those tasks | yes (path L1-bounded, digest not) |
| `failures` | failed attempts' bounded error text | yes, L1-bounded |
| `messages` | `messages_to(lead_agent)` | yes (body L1-bounded, from/to not) |

There is no runtime-event field, no raw transcript, no tool payload and no
hidden reasoning.

## 2. What is stored vs what is rendered

Case-study run (`REAL_RUN_CASE_STUDY=true`):

```text
attempt result text stored        316 bytes   -> rendered (all 3 succeeded results)
artifact rows                       3 rows    -> rendered (path + 64-hex digest each)
messages addressed to the Lead      0         -> nothing to render
runtime events stored              17 events / 4,706 bytes payload
objective                          605 bytes -> re-rendered into every round
```

Runtime-event records are **not directly rendered** into `LeadContext` or the
Lead prompt: they are a durable observation plane. They are absent from the
prompt by construction, not because they were filtered out at render time, and
the 4,706-byte figure above is a storage measurement, not a prompt cost.

## 3. The two bounds

The renderer applies two different bounds, and only the first is per-field:

```text
L1  per_text = (budget / max(entries,1)).clamp(64, 4096)        codex_lead.rs:259-260
    entries  = results + artifacts + failures + messages + 1     codex_lead.rs:257-258
    L1 applies to: result summary, artifact path, failure error,
                   message body, objective
    L1 does NOT apply to: candidate ids, message from/to, artifact
                   sha256, task ids, round, root_task_id, JSON envelope

L2  bound_utf8(&payload.to_string(), budget)                     codex_lead.rs:313
    a byte cap on the whole serialized payload, applied last

budget  = max_prompt_bytes - PROMPT_PREFIX.len() - PROMPT_SUFFIX.len() - 2
        = 32,768 - 37 - 175 - 2 = 32,554                (defaults; team_runner.rs:52)
```

`bound_utf8` is UTF-8-safe (it backs up to a character boundary) but is **not
JSON-structure-aware**: it can cut inside the serialized document.

Consequences:

* Because the L1 divisor excludes candidates, message from/to ids, artifact
  digests, task ids and the envelope, `entries <= 7` does **not** prove that L2
  cannot trigger. The two bounds are independent.
* `per_text` allocation begins decreasing at 8 entries (32,554 // 8 = 4,069, just
  under the 4,096 ceiling).
* At the floor, integer division already equals 64 at 507 and 508 entries
  (32,554 // 507 = 64, 32,554 // 508 = 64); at 509 entries the division gives 63,
  so the `clamp(64, ..)` lower bound starts doing the work. That is a statement
  about L1 only: **the first whole-payload truncation point is not
  characterized**.

```text
WHOLE_PAYLOAD_TRUNCATION_CHARACTERIZED=false
```

## 4. L2 may produce structurally incomplete JSON (hypothesis, not a product defect)

The mechanism is certain, and it is mechanical rather than observed:

```text
payload is one complete top-level JSON object
  -> serde_json::Value::to_string() emits a document that ends with its final `}`
  -> if L2 fires, bound_utf8 returns a strict prefix of that string
  -> a strict prefix cannot contain the final top-level closing brace
  => L2 truncation implies the rendered context is not a complete JSON document
```

Consequences, kept strictly separate:

```text
L2_MECHANISM_CHARACTERIZED=true
L2_TRUNCATION_IMPLIES_INVALID_JSON=true
L2_ACTIVATION_BOUNDARY_CHARACTERIZED=false     (which case first reaches L2 is unmeasured)
MALFORMED_JSON_PRODUCT_REACHABLE=unresolved    (no product-reachable case is established)
```

Whether any current product-reachable state can actually reach that point is an
S1 question and is **not** answered here. `L2_TRUNCATION_IMPLIES_INVALID_JSON`
is a statement about the renderer's mechanism; it is **not** a claim that the
current product can reach it, and it is not a product bug claim. No fix is
proposed in this document.

## 5. Case-study byte shares

Faithful re-implementation of `render_context` (compact JSON, L1 per field, then
L2) on the case-study run:

```text
round 0 (no results yet): context 778 bytes, prompt 991 bytes
  objective 78.8% | candidates 6.9% | envelope+ids 13.2% | results/artifacts/failures/messages ~0.3% each

round 1 (3 results + 3 artifacts): context 1,498 bytes, prompt 1,711 bytes
  objective 40.9% | results 27.4% | artifacts 21.0% | candidates 3.6%
  envelope+ids 6.9% | failures/messages 0.1% each

round 2 (unchanged board): 1,498 bytes — the rendered context is a pure function
  of the durable board, so an unchanged board re-sends an identical payload.
```

## 6. Fixed per-turn overhead

```text
DEVELOPER_INSTRUCTIONS                                 1,602 bytes
codex-exec Lead        instructions + prefix + context + suffix on EVERY turn  ~1.8 KB fixed/turn
codex app-server Lead  instructions once at thread start; later turns carry only prefix + context + suffix
```

For the case-study run that is ~3,313 bytes per codex-exec turn versus ~1,711 for
the app-server shape, before any provider-side tokenization. This is a transport
asymmetry between the two Lead runtimes, not a defect in either.

## 7. Case-study scope

The single run above is evidence about one shape of run, not a distribution:

```text
REAL_RUN_CASE_STUDY=true
MULTIRUN_CONTEXT_DISTRIBUTION_MEASURED=false
FAILURE_CONTEXT_MEASURED=false          (this run had no failed attempts)
MESSAGE_CONTEXT_REACHABILITY_MEASURED=false  (this run had no Lead messages)
```

## 8. Validation responsibilities (corrected)

Decision validation is layered, and the layers check different things:

```text
Lead prompt / semantic inputs
        |
        v
codex_lead parser (parse_reply -> task_spec / team_result)
    - reply must be exactly one JSON object (unknown fields rejected)
    - delegate: 1..=32 tasks; objective non-empty; target must be one of
      self.candidates (codex_lead.rs task_spec)
    - complete: answer non-empty and <= max_answer_bytes; selected_task_ids
      non-empty, <= 256, no repeats; selected_artifacts <= 256; artifact path
      non-empty and its task_id present in selected_task_ids; sha256 must be 64
      lowercase hex characters (local format constraint)
        |
        v
Lead::verify_completion (lead.rs:410-449) - the authoritative grounding
    - task_refs non-empty and the root is not an acceptable selection
    - each selected task must exist on the board, be Succeeded, and descend
      from the root
    - each selected artifact's task must be in task_refs, and the artifact must
      exist in the board's artifact rows for that task
        |
        v
authoritative TaskBoard / artifact records
```

So the parser checks shape, candidate target and local count/id/sha-format
constraints; it does **not** prove that a selected task succeeded or that an
artifact exists. `Lead::verify_completion` is the layer that grounds the final
selection in the authoritative board.

## 9. Semantic inputs vs authoritative grounding

```text
semantic model inputs (all rendered):
  objective, round, candidates, results, artifacts, failures, messages

authoritative/validator grounding (checked outside the prompt):
  candidate target  -> must be one of the routable candidates
  final task refs   -> exist, succeeded, descend from the root
  final artifact refs -> exist on the authoritative board for a selected task
```

The prompt is an input to reasoning; it is never the authority. Nothing in the
prompt can settle task state.

## 10. Representation and projection locations

The same fact can be stored, projected and re-sent without that meaning it is
redundant. Measured for the case-study run:

```text
objective
  durable truth        team_tasks.objective
  prompt projection    re-rendered into every round
  per-round transport  repeated in each turn's payload

child summary
  durable truth        team_task_runs.result
  prompt projection    rendered into the Lead context

artifact path+sha256
  durable truth        artifacts rows
  prompt projection    rendered into the Lead context
  final-selection      team_final_artifact_refs after a successful completion

runtime events
  durable truth        runtime_events (observation plane)
  prompt projection    none
```

## 11. Open questions this audit does not answer

```text
1. Can any product-reachable state push the serialized payload past L2? Requires
   the real renderer and the production writer bounds (S1-A/S1-B).
2. In real runs, do entries ever exceed 7, where per_text starts decreasing?
   Requires multi-run measurement, not one case study.
3. Does any real Lead decision depend on a failures or messages entry?
4. Is the objective's per-round re-send worth its retry/follow-up semantics
   change? (Not proposed here.)
```

## 12. Explicit non-goals

No context, prompt, transport, storage or Lead behavior was changed. No new
module, field, event, schema or wire value. No renderer fix, no
`Status`/`Summary`/`Trace` design or implementation, no jCodeMunch, no
ProcessSupervisor work (that line is closed:
`docs/experiments/process-supervisor-e1.md`).

## 13. Evidence (ephemeral)

```text
measurement output   /tmp/am-ctx-audit-evidence.txt          ephemeral, not durable
source board         /tmp/am-v05-s2/e2e/.agentmosaic/state.db ephemeral, not committed
code references      lead.rs:53-67, lead.rs:330-371, lead.rs:410-449,
                     codex_lead.rs:240-247 (render_prompt/budget),
                     codex_lead.rs:257-313 (render_context, L1 + L2),
                     codex_lead.rs:319 (parse_reply), :353 (task_spec),
                     :379 (team_result), :613-622 (bound_utf8),
                     team_runner.rs:52 (default max_prompt_bytes)
```

The `/tmp` files are local scratch evidence for the case study; the repository
retains the summarized measurements and conclusions above. The source database
is not committed.

## Status

```text
S0_CONTEXT_FLOW_AUDIT_COMPLETE=true
BYTE_BUDGET_MEASURED=true
TOKEN_COUNT_MEASURED=false

L1_BOUND_CHARACTERIZED=true
L2_BOUND_CHARACTERIZED=true
WHOLE_PAYLOAD_TRUNCATION_CHARACTERIZED=false
L2_MECHANISM_CHARACTERIZED=true
L2_ACTIVATION_BOUNDARY_CHARACTERIZED=false
L2_TRUNCATION_IMPLIES_INVALID_JSON=true
MALFORMED_JSON_PRODUCT_REACHABLE=unresolved

REAL_RUN_CASE_STUDY=true
MULTIRUN_CONTEXT_DISTRIBUTION_MEASURED=false
FAILURE_CONTEXT_MEASURED=false
MESSAGE_CONTEXT_REACHABILITY_MEASURED=false

CONTEXT_IMPLEMENTATION_CHANGED=false
RENDERER_IMPLEMENTATION_CHANGED=false
STATUS_SUMMARY_TRACE_DESIGNED=false
JCODEMUNCH_INTEGRATED=false
PROCESS_SUPERVISOR_LINE_CLOSED=true
```
