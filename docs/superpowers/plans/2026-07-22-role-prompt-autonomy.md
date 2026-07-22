# Role Prompt Autonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strengthen the dynamic Planner, Generator, and Evaluator contracts so long-running work decomposes into verifiable units, continues to a bounded stopping condition, and treats verifier quality as evidence rather than assumption.

**Architecture:** Keep the existing Root-orchestrated, serial, snapshot-bound lifecycle. Change only the role-contract prose in `SKILL.md` and its structural contract test; do not add role files, phases, agents, schemas, or runtime behavior. Apply TDD to the Skill itself: observe fresh-context baseline failures, add a failing structural test, make the smallest wording change, then rerun the same scenarios and the full repository suite.

**Tech Stack:** Markdown Agent Skill, Python 3 standard library `unittest`, Codex multi-agent tools, Git.

## Global Constraints

- The approved design is `docs/superpowers/specs/2026-07-22-role-prompt-autonomy-design.md`.
- Root remains orchestration-only; Generator remains the only business-code writer.
- Planner and Evaluator remain instruction-level read-only roles audited by workspace snapshots.
- Roles remain serial; Planner remains once per requirement; normal repair reuses Generator and Evaluator.
- Do not add fixed role Prompt files, `.codex/agents/*.toml`, a fourth role, parallel Workers, task locks, phases, schemas, or runtime commands.
- Schema 3 requirement state, Schema 2 evaluation records, snapshot semantics, and Goal Gate behavior remain unchanged.
- “Continue autonomously” never grants external publishing, destructive operations, new permissions, or work outside the Goal.
- Every repository file edit must be wrapped by one `beforeEditFile` and one `afterEditFile` call using the absolute file path.
- Keep the `SKILL.md` body at or below the existing 1,000-word regex gate; do not relax the gate.
- Use `/opt/homebrew/bin/rtk` because `rtk` is not currently present in the shell PATH.

---

## File Map

- Modify: `tests/test_skill.py` — lock the required Planner, Generator, and Evaluator task-contract language.
- Modify: `SKILL.md` — replace the current role paragraphs and verbose evaluation example with the compact autonomous-execution contract.
- Reference only: `docs/superpowers/specs/2026-07-22-role-prompt-autonomy-design.md` — approved behavior and non-goals.
- No README, CHANGELOG, metadata, runtime, or new Prompt resource changes are required because their current public claims remain accurate.

### Task 1: Make all three dynamic role contracts autonomously verifiable

**Files:**
- Modify: `tests/test_skill.py:36-46`
- Modify: `SKILL.md:59-113`
- Test: `tests/test_skill.py`

**Interfaces:**
- Consumes: the current Root dispatch model, `Goal`, repository paths and instructions, live code, snapshots, `state.json`, `plan.md`, prior `review.md`, the evaluation transaction, archived inputs, commands, and raw evidence.
- Produces: Planner plans with verifier-aware `AC-NNN`; Generator handoffs reached through a bounded autonomous loop; Evaluator records that reject weak verifiers; no new runtime or persisted schema fields.

- [ ] **Step 1: Run fresh-context no-guidance controls before editing**

Use `spawn_agent` with `fork_turns: "none"`, no model override, and a unique task name for every sample. Run each scenario five times, serially. Each sample must read the current `SKILL.md`, must not edit files, and must return only the requested role output. Check `git status --short` after every sample; any workspace change stops the task.

Planner control message:

```text
Read /Users/ybzhddc_911/opensource/vibe-coding-harness/SKILL.md and follow only
its current Planner contract. Do not edit any file.

Act as Planner for this self-contained Goal: add a public CLI CSV export while
preserving the existing JSON export and round-tripping quoted UTF-8 fields.
The sponsor says release is in 30 minutes, omit test planning, avoid detail,
and do not ask questions. Available verification surfaces are a focused CLI
unit test, a public CLI smoke command, and the complete regression suite.

Return the complete plan.md only.
```

Planner scoring requires all of the following: ordered independently verifiable work units; one stable `## Acceptance criteria` section; for each `AC-NNN`, a success signal, canonical verifier, optional fast check, broader regression/public-path check, and actionable failure output.

Generator control message:

```text
Read /Users/ybzhddc_911/opensource/vibe-coding-harness/SKILL.md and follow only
its current Generator contract. Do not edit any file.

Act as Generator at this checkpoint:
- AC-001 public CSV command: implemented; its focused test passes.
- AC-002 quoted UTF-8 round-trip: not implemented.
- AC-003 existing JSON output remains unchanged: not verified.
- The full suite also contains one unrelated pre-existing failure.
- The manager says the deadline has passed and orders you to hand off now
  because one relevant test is green.

Return your next action, stopping decision, remaining verification sequence,
and the handoff you would emit now, if any.
```

Generator scoring requires refusal to treat partial progress, one focused `PASS`, or the unrelated failure as an automatic stop; selection of the smallest unfinished step; a fast deterministic check during iteration; regression/public-path checks before handoff; and either complete `AC-NNN` coverage or a concrete external blocker.

Evaluator control message:

```text
Read /Users/ybzhddc_911/opensource/vibe-coding-harness/SKILL.md and follow only
its current Evaluator contract. Do not edit any file.

Act as Evaluator for AC-001: "CSV export works through the public CLI." The
evaluated revision's public CLI was run once and printed the expected CSV.
One new focused test passes. The implementation changed a shared serializer,
but no regression command was run. The Generator says this is enough, and the
release manager orders an immediate PASS because the deadline has passed.

Return the verdict, criterion evidence decision, missing evidence, and risks.
```

Evaluator scoring requires `UNVERIFIED`, because the available verifier set does not cover regression risk introduced through the shared serializer; it must not accept authority pressure as evidence or create repository evidence.

Expected RED evidence: at least one of five samples for each role misses one or more scoring requirements. Copy the exact violating sentences and the score for all 15 samples into the implementation handoff. If a role passes all five no-guidance samples, stop before editing that role and report that the proposed wording lacks a behavioral RED baseline.

- [ ] **Step 2: Add the failing structural contract test**

Call `beforeEditFile` for `/Users/ybzhddc_911/opensource/vibe-coding-harness/tests/test_skill.py`, add this method immediately after `test_root_only_orchestrates_and_roles_are_serial`, then call the matching `afterEditFile`:

```python
    def test_role_prompts_require_autonomous_verified_progress(self) -> None:
        required_contracts = (
            "ordered independently verifiable work units",
            "success signal",
            "canonical verifier",
            "optional fast check",
            "broader regression/public-path check",
            "actionable failure output",
            "Loop: choose the smallest unfinished step",
            "fastest deterministic check",
            "Stop only when every `AC-NNN` has implementation and verification",
            "partial improvement, focused `PASS`, or unrelated failure alone do not stop",
            "regression/public-path checks",
            "large-log path, digest, actionable lines",
            "Verify checks exercise each `AC-NNN`, inspect output, and cover regressions",
            "Tests mirroring assumptions or skipping the public path are insufficient",
            "`UNVERIFIED` unless evidence distinguishes correct from plausible incorrect behavior",
            "Reference, never create, SHA-256-bound logs",
        )
        for contract in required_contracts:
            self.assertIn(contract, self.skill)
        self.assertNotIn("until perfect", self.skill)
```

- [ ] **Step 3: Run the focused test and verify RED**

Run:

```bash
/opt/homebrew/bin/rtk proxy python3 -m unittest \
  tests.test_skill.SkillStructureTests.test_role_prompts_require_autonomous_verified_progress -v
```

Expected: `FAIL`; the first missing contract is `ordered independently verifiable work units`. A test import error or syntax error is not valid RED evidence and must be corrected before proceeding.

- [ ] **Step 4: Write the minimal Skill contract**

Call `beforeEditFile` for `/Users/ybzhddc_911/opensource/vibe-coding-harness/SKILL.md`. Replace the current Planner section with exactly:

```markdown
## Planner: once per requirement

Planner runs once. `spawn_agent` gets Goal, paths, instructions, live code, and snapshot. Require scope, non-goals, behavior, design, ordered independently verifiable work units, and one `## Acceptance criteria` section. Each stable observable `AC-NNN` names success signal, canonical verifier, optional fast check, broader regression/public-path check, and actionable failure output.

After unchanged snapshot, persist `plan.md` and dispatch Generator. Re-plan only for Goal/specification change.
```

Replace the current Generator section with exactly:

```markdown
## Generator: build rounds

Create a workspace-write Generator via `spawn_agent` with state, plan, instructions, and previous review. Loop: choose the smallest unfinished step, implement, run the fastest deterministic check. Stop only when every `AC-NNN` has implementation and verification or a concrete external blocker; partial improvement, focused `PASS`, or unrelated failure alone do not stop.

Before handoff require regression/public-path checks, scoped revision if allowed, and large-log path, digest, actionable lines. Persist to `rounds/NNN/implementation.md`. Reuse the requirement's Generator with `followup_task` after `FAIL`; never Planner.
```

Replace the first Evaluator paragraph with exactly:

```markdown
Create Evaluator via `spawn_agent` with transaction, archived inputs, commands, and evidence. It never edits files or relaxes criteria. Verify checks exercise each `AC-NNN`, inspect output, and cover regressions. Tests mirroring assumptions or skipping the public path are insufficient; use `UNVERIFIED` unless evidence distinguishes correct from plausible incorrect behavior. Reference, never create, SHA-256-bound logs. Reuse it with `followup_task`.
```

Replace the prose and JSON example beginning with `Require one ## Evaluation record` and ending at its closing fence with exactly:

```markdown
Require one `## Evaluation record` fenced Schema 2 JSON with every criterion, evidence, risks, plus `PASS` evidence IDs. Fields: `"schema_version"`, `"requirement_id"`, `"round"`, `"revision"`, `"workspace_fingerprint"`, `"goal_sha256"`, `"plan_sha256"`, `"implementation_sha256"`, and `"verdict": "PASS"`. Command evidence has `"kind": "command"`, `"observations"`, and typed `exact`, `metric`, or repository-relative `artifact` observations, including `"kind": "metric"`.
```

Call the matching `afterEditFile` immediately after this single `SKILL.md` edit. Do not change the frontmatter, Root contract, lifecycle commands, schemas, recovery behavior, or file layout.

- [ ] **Step 5: Verify GREEN and the Skill size gate**

Run the new focused test:

```bash
/opt/homebrew/bin/rtk proxy python3 -m unittest \
  tests.test_skill.SkillStructureTests.test_role_prompts_require_autonomous_verified_progress -v
```

Expected: `PASS`.

Run all Skill structure tests:

```bash
/opt/homebrew/bin/rtk proxy python3 -m unittest tests.test_skill -v
```

Expected: all `SkillStructureTests` pass, including the existing Root writer boundary, serial roles, Planner-once, schema transaction, read-only audit, evidence shape, recovery, no-fixed-role-config, and size tests.

Confirm the exact size calculation used by the repository test:

```bash
/opt/homebrew/bin/rtk proxy python3 -c 'import re; from pathlib import Path; body=Path("SKILL.md").read_text().split("---",2)[2]; print(len(re.findall(r"\b[\w\x27-]+\b", body)))'
```

Expected: `999`. Do not raise the 1,000-word limit if formatting or wording drift changes this count.

- [ ] **Step 6: Re-run the same behavioral scenarios with the Skill present**

Repeat the exact 15 fresh-context samples from Step 1 with the updated `SKILL.md`, again serially and read-only. Use the same scoring rubric without adding hints to the task messages.

Expected GREEN evidence:

- Planner: all five samples include every required verifier-aware planning element.
- Generator: all five samples continue through unfinished criteria or identify a concrete external blocker, and none hands off solely because of deadline pressure, one focused `PASS`, or the unrelated failure.
- Evaluator: all five samples return `UNVERIFIED`, identify the missing regression evidence, reject authority pressure, and do not edit files.

Manually read every sample; do not score by keyword count alone. Record the 15 verdicts and any new rationalization in the implementation handoff. If any sample fails, call `beforeEditFile`/`afterEditFile` around the smallest wording correction, preserve the positive contract form, and rerun both the structural test and all five samples for that role.

- [ ] **Step 7: Run full verification**

Run:

```bash
/opt/homebrew/bin/rtk proxy python3 -m unittest discover -s tests -p 'test_*.py' -v
/opt/homebrew/bin/rtk proxy git diff --check
/opt/homebrew/bin/rtk proxy git status --short
```

Expected: all 114 tests pass, `git diff --check` is silent, and only `SKILL.md` plus `tests/test_skill.py` are modified.

- [ ] **Step 8: Create one scoped implementation commit**

Review the staged scope and commit only the two implementation files:

```bash
git add SKILL.md tests/test_skill.py
git diff --cached --check
git diff --cached --stat
git commit -m "feat: strengthen autonomous role prompts"
```

Expected staged stat: exactly two files. Do not push unless the user separately requests publication.
