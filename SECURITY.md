# Security Policy

## Supported versions

The project is pre-1.0. Security fixes target the latest commit on the
canonical branch; older commits and unmaintained forks are unsupported.

## Report a vulnerability

Do not open a public issue. Use GitHub private vulnerability reporting:

<https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/security/advisories/new>

Include the affected command or file, realistic impact, reproduction steps,
affected revisions, and any suggested mitigation. Do not include secrets or
repository data you do not own.

## Security boundaries

Reports are especially useful for violations of these boundaries:

- Controller state: only the registered Controller may mutate a locked Schema
  4 run; revisions, append-only histories, artifacts, and migration claims must
  remain hash-bound and crash recoverable.
- Worker scope: an Attempt may change only its declared repository-relative
  paths and must not alter protected Git/config/remote state.
- Provider identity: persisted parent/child PID, process-start identity,
  process group, launch policy, and Attempt token must match before polling,
  stopping, recovering, or trusting a result.
- Output schemas: Provider JSON is hostile input until strict UTF-8,
  duplicate-key, Unicode-scalar, field, and role-specific Schema validation
  succeeds.
- Git CAS: source branches, task refs, and run integration refs must never be
  overwritten without the recorded expected OID.
- Filesystem confinement: symlinks, non-regular artifacts, traversal, ancestor
  swaps, and writes outside the target/control roots must fail closed.
- Migration: Schema 3 source and backup trees must be byte/mode identical;
  partial batches and reservation races must not create duplicate mappings.
- Command authorization: project verification commands require explicit user
  authorization and exact-source digest binding.

Potential data disclosure, arbitrary command execution, evidence forgery,
unsafe process termination, or remote-contact paths should always be reported
privately.
