# Schema 3 migration fixtures

`states.json` freezes representative Schema 3 state shapes for all four
historical statuses. Tests materialize each state in a fresh Git repository,
replace `BASE_SHA` with that repository's fixed baseline, and add opaque
round/evaluation/interruption files. The migration never imports this fixture
module or executes the removed Schema 3 harness.

The fixtures intentionally exercise byte preservation: every regular file,
mode, relative name, and digest in a requirement tree must survive in the
immutable backup. They are test evidence, not a supported Schema 3 writer.
