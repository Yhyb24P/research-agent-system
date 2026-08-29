# Qualification Mainline

This directory defines the post-RC qualification mainline for `research-agent-system`.

The software construction baseline is considered complete enough to freeze architecture while qualification proceeds. Qualification does **not** transfer authority away from `researchd`: Agents, A2A tasks, AG-UI events, LangGraph state, workspace transports, model providers, and job backends remain non-authoritative adapters/runtimes.

## Mainline

```text
IQ — Interoperability Qualification
  IQ01 A2A real interoperability
  IQ02 Workspace transport and fault qualification
  IQ03 AG-UI replay/reconnect compatibility

DQ — Deployment Qualification
  DQ01 Host / sandbox / filesystem
  DQ02 Agent runtime / invocation lifecycle
  DQ03 Cloud provider / egress governance
  DQ04 Backup / restore / disaster recovery
  DQ05 Soak / restart / fault endurance

RQ — Release Qualification
  RQ01 Release provenance and exact-candidate manifest
  RQ02 End-to-end release acceptance
  RQ03 Production Go / No-Go
```

Read `MAINLINE_PLAN.md` first. `GATE_POLICY.md` defines the acceptance semantics and `EVIDENCE_CONTRACT.md` defines what may count as qualification evidence. `QF00_RC79_IMPACT_MATRIX.md` records why the six rc.79 evidence objects are on-candidate reruns rather than inherited historical PASS results.

Machine-readable planning and evidence objects are defined in:

- `schemas/qualification_plan.schema.json`
- `schemas/qualification_evidence.schema.json`
- `schemas/qualification_acceptance.schema.json`
- `schemas/dq04_offhost_protection.schema.json`

Validate the schemas and cross-record semantics with:

```bash
uv sync --frozen --extra qualification
uv run python scripts/qualification_validate.py \
  --plan examples/qualification_plan.example.json \
  --evidence examples/qualification_evidence.example.json \
  --acceptance examples/qualification_acceptance.example.json
```

A successful validator run means that the records are internally consistent.
It does not turn an `INCONCLUSIVE` or `FAILED` observation into a Gate pass.

Run the executable DQ04 software matrix with:

```bash
uv run pytest -q tests/qualification/test_dq04_backup_restore.py
```

This matrix does not replace the deployment-environment primary-loss drill,
off-host protection record, immutable-candidate evidence, or independent Gate
acceptance.

## Non-negotiable rule

A Gate is not complete because code exists or tests passed once. It is complete only when the required evidence is collected against an exact immutable candidate commit and the Gate acceptance rules pass without unresolved hard failures.
