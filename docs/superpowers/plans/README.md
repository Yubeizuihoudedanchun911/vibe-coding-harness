# External Controller implementation record

The roadmap and six phase plans in this directory record the reviewed execution
sequence for the Schema 4 external Controller cutover. The implementation is
complete on `codex/external-controller-parallel-workers-design`.

Phase commits:

1. Schema 4 foundation: `48a63c1`, `e681d8e`, `1663708`
2. Prompts, Providers, and runners: `17ae7e9`, `f3f1285`, `863521d`
3. Git scheduling and integration: `faf3fb0`, `7188f3c`, `abdf4ec`,
   `620932a`, `60c9bd8`
4. Controller and evaluation loop: `f3ccda2`
5. Recovery and CLI: `c5ddde9`
6. Schema 3 migration: `1e0f8cc`
7. Product cutover and packaging: `8e2b7d7`

The unchecked boxes are retained as the original execution checklist, not as a
claim that work remains. Live source, Schemas, Prompts, tests, and the current
README files are the product truth if a historical plan snippet differs.

The final offline release gate passed 173 tests with one explicitly opt-in real
Codex CLI smoke test skipped. Wheel and sdist builds passed Twine checks and
loaded all nine Prompt resources and three JSON Schemas from isolated
installations outside the checkout.
