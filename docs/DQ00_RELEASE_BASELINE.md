# DQ00 — Release Baseline and Supply-Chain Gate

## Purpose

DQ00 freezes the V1 control plane before target-environment qualification. Every
DQ01–DQ06 result must identify one source commit, dependency lock hash, schema
revision, and host/runtime manifest. This gate does not claim production
readiness; it establishes reproducible release provenance.

## Generate the manifest

Run from the repository root using the exact environment that will run the
qualification:

```text
.venv/bin/python scripts/release_manifest.py --output release-manifest.json
```

The command refuses a dirty worktree by default. The manifest records only
non-secret metadata: commit/branch and tree counts, `uv.lock` SHA-256, package
versions, Python version, Alembic head, OS/kernel/architecture, and Bubblewrap
version. It must never be populated with API keys, environment dumps, prompts,
CAS bytes, or runtime database contents.

## Current release evidence

- Repository: `https://github.com/Yhyb24P/research-agent-system`
- Qualification branch: `main`
- Current commit: `96d7516a61ee37b2a8e6623e2db8214f1b81dbd0`
- Functional baseline schema head: `0007`; the current DQ branch advances the
  schema only through explicitly reviewed qualification changes.
- Initial functional baseline regression: 95 pytest tests passed; strict mypy
  passed for 95 files. Current RC reruns the suite after each DQ hardening
  change; the current count is recorded in the qualification report.
- Functional baseline at `96d7516`: 140 files / 9215 lines. The RC adds only
  DQ00/DQ01 governance tooling and documentation; the generated manifest is the
  authoritative count for the tagged checkout.
- Dependency lock: `uv.lock` is committed; its SHA-256 must be captured by the
  generated manifest.

## Gate checklist

- [x] Source is a Git repository and `main` is pushed to the public remote.
- [ ] An immutable RC tag (for example `v1.0.0-rc.1`) is created after the
      baseline commit and points to the exact tested commit.
- [ ] A generated `release-manifest.json` is retained with the DQ evidence.
- [ ] Dependency/SBOM and target container image digests are recorded.
- [ ] Bubblewrap binary version, path, permissions, and file capabilities are
      recorded on the target host.
- [ ] The test and type-check commands are rerun from the tagged RC.

The missing checklist items are intentionally DQ work, not reasons to add new
core features. If a qualification failure identifies an implementation defect,
make the smallest fix, create a new RC, and rerun affected gates.
