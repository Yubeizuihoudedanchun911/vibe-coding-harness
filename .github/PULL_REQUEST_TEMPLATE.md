## Outcome

<!-- Describe the user or maintainer outcome. -->

## Contract impact

<!-- Cover Schema 4 state, Prompts/Schemas, Provider, recovery/CAS, migration,
parallel scheduling, packaging, and platform compatibility. -->

## Validation

```text
focused command:
result:

offline full suite:
result:

wheel/sdist smoke:
result:
```

## Provider disclosure

<!-- State whether only fake/offline Providers ran. If a real Codex CLI command
ran, list the exact opt-in command and whether network access was possible. -->

## Checklist

- [ ] The change is focused and excludes unrelated files.
- [ ] A failing test preceded each behavior change.
- [ ] Schema 4 state and every new recovery/crash window are covered.
- [ ] Migration impact and Schema 3 compatibility are explicit.
- [ ] Prompt and Schema package resources are complete and versioned.
- [ ] The full offline suite passes on a supported Python version.
- [ ] Wheel and sdist install outside the checkout and `vibe --help` works.
- [ ] No acceptance, process-identity, path-scope, or Git-CAS gate was weakened.
- [ ] No automated merge, push, pull request, publication, or remote Provider
      workflow was added.
- [ ] Documentation matches live behavior.
- [ ] No secrets, private data, caches, or local agent state are included.
