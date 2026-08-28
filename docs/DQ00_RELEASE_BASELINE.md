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
- Qualification baseline commit: `125a2cc` (`v1.0.0-rc.17`)
- Current schema head: `0008`; the DQ branch advances the
  schema only through explicitly reviewed qualification changes.
- Current RC regression: 107 pytest tests passed; strict mypy passed for 86
  source files. The RC is tagged only after these checks pass.
- The generated manifest is the authoritative file/line/dependency count for
  the tagged checkout and must be retained with qualification evidence.
- Dependency lock: `uv.lock` is committed; its SHA-256 must be captured by the
  generated manifest.
- The repository quality workflow reruns pytest, mypy, Alembic migrations, and
  diff hygiene on pushes and pull requests. It intentionally does not require
  GPU drivers, cloud credentials, or production secrets.

## Gate checklist

- [x] Source is a Git repository and `main` is pushed to the public remote.
- [x] Immutable RC tag `v1.0.0-rc.17` points to the exact tested commit.
- [ ] A generated `release-manifest.json` is retained with the DQ evidence.
- [ ] Dependency/SBOM and target container image digests are recorded.
- [ ] Bubblewrap binary version, path, permissions, and file capabilities are
      recorded on the target host.
- [ ] The test and type-check commands are rerun from the tagged RC.

The missing checklist items are intentionally DQ work, not reasons to add new
core features. If a qualification failure identifies an implementation defect,
make the smallest fix, create a new RC, and rerun affected gates.
