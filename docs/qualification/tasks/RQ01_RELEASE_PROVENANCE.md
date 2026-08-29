# RQ01 — Release Provenance and Exact-Candidate Manifest

## Objective

Bind the release candidate identity, source, dependencies, qualification evidence and CI result into one reproducible release record.

## Required inputs

- exact candidate commit SHA;
- immutable `v1.0.0-rc.*` tag pointing to that commit;
- green CI for that exact commit;
- lock-derived SBOM;
- Python/platform/toolchain version records;
- accepted evidence IDs for IQ01-IQ03 and DQ01-DQ05;
- hashes of qualification acceptance records;
- release-manifest generator output.

## Required checks

- tag dereferences to exact candidate commit;
- CI head SHA equals candidate commit;
- SBOM generated from the committed lock;
- no runtime database or environment evidence is accidentally committed;
- release manifest contains no secret;
- every referenced evidence bundle reports the same candidate commit or an explicit approved compatibility/requalification record;
- provenance signing/attestation status is recorded.

## Acceptance

Unsigned provenance may remain a documented release risk for an RC, but Production Go must explicitly decide whether unsigned tag/attestation is acceptable. Never describe unsigned provenance as signed or verified.
