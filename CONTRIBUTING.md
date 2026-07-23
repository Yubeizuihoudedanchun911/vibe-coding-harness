# Contributing

Thank you for improving Vibe Coding Harness. Contributions must preserve the
external Controller's recovery, isolation, evidence, and Git safety contracts.

## Development setup

Python 3.10 or newer is required. Runtime code uses only the standard library.

```bash
git clone https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness.git
cd vibe-coding-harness
python -m pip install --no-deps -e .
PYTHONDONTWRITEBYTECODE=1 \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

## Workflow

1. Use a focused feature branch.
2. Write a failing test before changing behavior.
3. Preserve unrelated files and stage explicit paths.
4. Run the focused tests, then the complete offline suite.
5. Update live documentation, migration notes, and package resources together.
6. Report whether any real Provider command was run; offline fakes are the
   default.

## Architectural invariants

- The Controller is the sole Schema 4 state writer.
- Planner and Evaluator operations remain read-only and snapshot-audited.
- Worker Attempts use fresh worktrees, branches, contexts, and Provider
  identities.
- Parallel dispatch requires dependency, path-scope, and exclusive-resource
  safety.
- Candidate verification and integration are serial and Git-ref updates use
  compare-and-swap.
- Provider output remains untrusted until identity, JSON, and output-schema
  validation passes.
- Every durable intent is recoverable or fails closed at a recorded boundary.
- Goal success requires current evidence and an independent Evaluator PASS.
- No code path automatically merges, pushes, opens pull requests, publishes
  packages, or contacts a remote.

## Prompts, Schemas, and Provider adapters

- Version Prompt resources under `prompts/`; never replace an existing version
  in place.
- Keep Planner, specialist Worker, base Worker, and Evaluator responsibilities
  explicit.
- Update the three JSON Schemas and their parser tests together when changing a
  contract.
- Provider adapters must persist launch identity, stdout/stderr, exit receipt,
  and output paths before the result is trusted.
- Installed wheel and sdist smoke tests must load all nine Prompts and all
  three Schemas outside the checkout.

## Recovery and migration

- Add fault-injection coverage for every new prepared marker or CAS window.
- `FAILED` stays terminal.
- Schema 3 migration remains explicit, read-only, byte-preserving,
  prepare-all-before-install, and idempotent for one source/base identity.
- Never make historical Schema 3 success look like native Schema 4 Goal Gate
  success.

## Pull requests

Complete the pull request template with exact commands and results. Disclose
schema, recovery, migration, packaging, platform, and Provider impact. Never
include credentials, private repository bytes, local agent state, or generated
caches.

AI-assisted contributions are welcome, but contributors remain responsible for
understanding and verifying the submitted behavior.
