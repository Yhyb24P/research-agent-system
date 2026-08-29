# Qualification Evidence Contract

## Evidence object

Qualification evidence is an observation bundle, not a narrative claim. It should validate against `schemas/qualification_evidence.schema.json`.

Minimum fields:

```json
{
  "evidence_id": "qe_...",
  "gate_id": "IQ01",
  "candidate_commit": "40-hex-sha",
  "candidate_tag": "v1.0.0-rc.N",
  "environment_fingerprint": "sha256:...",
  "producer": {
    "actor_type": "HUMAN|SYSTEM|AGENT",
    "actor_id": "stable actor identity"
  },
  "started_at": "UTC timestamp",
  "completed_at": "UTC timestamp",
  "tool_versions": {},
  "checks": [],
  "artifacts": [],
  "result": "PASS|FAIL|INCONCLUSIVE"
}
```

## Evidence classes

| Class | Examples |
|---|---|
| COMMAND | command, exit code, stdout/stderr hash |
| PROTOCOL_TRACE | request/response/event transcript with secret redaction |
| STATE_SNAPSHOT | DB/query snapshots and state hashes |
| ARTIFACT | output file/CAS object/hash/provenance |
| METRIC | latency, resource usage, retry counts, duplicate counts |
| FAULT_INJECTION | injected fault, trigger time, expected containment |
| RESTORE | backup ID, restore target, integrity checks |
| HUMAN_REVIEW | reviewer decision referencing prior evidence |

## Sensitive evidence

Evidence collection must not weaken the system under test.

- SECRET values are never stored in qualification evidence.
- LOCAL_ONLY payloads remain local; only hashes/derived summaries may be exported when policy allows.
- Provider request/response bodies are stored only according to classification and egress policy.
- Redaction itself must be testable; a redacted transcript should preserve structural fields needed for verification.

## Evidence immutability

Once an evidence bundle is accepted, do not edit it. A rerun creates a new `evidence_id` and may supersede an older result. RQ uses an explicit list of accepted evidence IDs.

## Acceptance object

Evidence describes observations; it does not approve its own Gate. A separate
record conforming to `schemas/qualification_acceptance.schema.json` binds the
review decision to the exact candidate and accepted evidence IDs. The evidence
producer and reviewer must be different actors. A correction creates a new
`acceptance_id` and points to the superseded record.

`scripts/qualification_validate.py` enforces schema validity plus candidate,
dependency, result-severity, self-approval and cross-record consistency. A
successful run proves contract consistency only; the acceptance result remains
`PASSED`, `FAILED` or `INCONCLUSIVE` as recorded.
