# Cloud Egress Policy

## Governing rule

Cloud-bound context is constructed from authoritative database IDs. Cloud Lead callers provide an immutable `CloudContextSelection` containing a `run_id` and optional matching WorkOrder, Artifact, Observation, and Verification IDs. The adapter owns `ContextBuilder`; it does not accept a caller-built bundle or arbitrary text.

| Classification | Direct cloud eligibility |
|---|---|
| `PUBLIC` | Allowed |
| `CLOUD_SAFE` | Allowed |
| `PROJECT_PRIVATE` | Denied; substituted only when a registered `CLOUD_SAFE` derivation exists |
| `LOCAL_ONLY` | Denied |
| `SECRET` | Denied |
| unknown | Denied |

Private/raw artifact classification cannot be changed in place. Migration `0002` installs a database trigger rejecting classification changes. Cloud-safe material is new content-addressed bytes with source IDs, producer/version, canonical parameters/hash, transformation hash, and creation time. A private/raw source cannot be derived directly as `PUBLIC`.

## Context construction

`ContextBuilder` validates Run/WorkOrder/Attempt relationships, resolves every selected record from SQLite, and permits only `PUBLIC`/`CLOUD_SAFE` evidence. A cloud-safe Observation must be backed exclusively by eligible Artifact sources; step/job sourced observations are denied. Verification selection also pulls its referenced Observations through the same checks. Artifact bytes are classification-checked before reading, eligible derivations are substituted, SHA256 is verified, and byte/MIME/UTF-8 limits are enforced before deterministic redaction.

The resulting immutable `CloudContextBundle` contains redacted goal/objective fields and selected `PUBLIC`/`CLOUD_SAFE` Artifact, Observation, and Verification records. Canonical JSON serialization and its SHA256 are recorded with every cloud interaction.

## Redaction defense-in-depth

The deterministic redactor removes:

- PEM private-key blocks;
- bearer tokens;
- configured credential environment variables;
- configured secret literal fixtures;
- configured private filesystem prefixes.

Redaction never grants eligibility. A denied classification remains denied even if its content appears redactable.

## Failure behavior

Context construction fails closed for missing/mismatched records, unknown or denied classification, unsafe evidence sources, missing PROJECT_PRIVATE derivation, unsupported MIME, oversized content, invalid UTF-8, or artifact hash mismatch. On failure, no provider call occurs.
