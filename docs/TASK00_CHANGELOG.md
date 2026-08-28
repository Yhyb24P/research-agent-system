# TASK 00 Changelog

## Baseline

Implementation started from the sealed architecture handoff package. No application code or usable Git metadata existed in the workspace.

## Files changed

- Added Python/uv project metadata and package boundaries.
- Added protocol-independent typed domain DTOs, enums, ID constraints, acceptance criteria, and deterministic transition tables.
- Copied the frozen JSON schemas and representative examples into repository-owned fixtures.
- Copied accepted ADRs into `docs/adr` and recorded dependency/identity/time conventions.
- Added schema, validation, transition, ID-boundary, and static typing tests.

## Domain/API changes

This is the initial contract freeze. Capabilities are a closed enum; acceptance criteria are discriminated typed unions. Entity-prefixed IDs reject cross-entity values at validation time and are distinct aliases during static analysis.

## Migration changes

None. Persistence and migration behavior are deferred to TASK 01.

## Security impact

The domain rejects unknown fields, unknown capabilities, invalid state transitions, and cross-entity IDs. No LLM, network, host execution, secret, or protocol SDK surface has been introduced.

## Tests executed / results

- `uv run pytest`: 17 passed.
- `uv run mypy`: success across 32 source/test files.
- `uv lock --check`: lock is current.
- `rg -i "a2a|mcp" src/researchd/domain`: no matches.
- Draft 2020-12 meta-schema and fixture validation are included in pytest.

## Known limitations

- Runtime persistence and transactional transition enforcement do not exist yet.
- Artifact derivation, policy, egress, and approvals are only documented contracts until TASK 02.
- Sandbox security remains implementation-dependent and is not claimed by this task.

## Deferred work

All TASK 01–08 implementation remains gated.

## Gate checklist

- [x] All tests pass.
- [x] Typed ID validation boundaries reject cross-entity IDs.
- [x] Draft 2020-12 schemas validate.
- [x] Domain contains no protocol dependencies.
- [x] Domain/state review completed against the handoff contracts.
