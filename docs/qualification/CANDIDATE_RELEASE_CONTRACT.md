# Candidate and Release Contract

## Lifecycle

```text
unfrozen development
  -> candidate-ready tree
  -> annotated immutable RC tag (identity frozen)
  -> exact-candidate software gate + IQ/DQ + RQ01/RQ02
  -> RQ03 GO / NO_GO
  -> GitHub Release only on GO
```

An RC tag establishes only an immutable candidate identity and the version
mapping needed to bind evidence. It does not establish Gate pass, L4 release
qualification, production approval, signed provenance, or a GitHub Release.

## Version mapping

```text
vX.Y.Z-rc.N <-> X.Y.ZrcN
vX.Y.Z      <-> X.Y.Z
```

Branch preflight may be untagged. Exact-candidate mode requires the expected
tag to resolve to, and be checked out at, the candidate commit.

## rc.81 disposition

`v1.0.0-rc.81` is an annotated, immutable, unsigned product-candidate
snapshot that dereferences to:

```text
f7785244acc0687324376806666ead2be26bf478
```

Its status is:

```text
candidate_identity      = FROZEN
qualification_status    = NOT_ESTABLISHED
release_claim           = L0 only
GitHub Release           = absent
```

It must not be moved, deleted, reused, or retrospectively described as a
qualified release candidate. Any repair after this snapshot uses a new,
monotonically newer RC identity.

## Evidence terminology

Pull-request CI may validate GitHub's generated merge ref. It is
**merge-compatibility evidence**, not exact-candidate evidence. Exact-candidate
evidence requires a workflow that resolves the immutable tag and asserts its
checked-out SHA before building and testing the installed artifact.
