# Planner、Generator、Evaluator Prompt 自主执行增强设计

日期：2026-07-22

## 背景

当前 Vibe Coding Harness 通过 Root 串行调度 Planner、Generator 和 Evaluator。Root 负责需求状态、持久化证据、快照审计和 Goal Gate；Planner 与 Evaluator 是 instruction-level read-only 角色；Generator 是唯一业务代码写入者。

Anthropic 在《[Building a C compiler with a team of parallel Claudes](https://www.anthropic.com/engineering/building-c-compiler)》中总结了长时间自主开发的几个关键条件：Agent 应把目标拆成小任务、持续跟踪进展、自主选择下一步并持续工作；测试必须准确表达目标；反馈需要低噪声、可检索；开发过程中应优先使用快速且稳定的检查，最终再运行完整验证。

该实验使用同质 Worker 并行抢占任务，没有 orchestration agent 或高层目标管理流程。当前 Harness 则使用共享工作区上的三角色串行闭环，因此只吸收其自主执行和反馈设计原则，不引入任务锁、并行 Worker、独立 clone 或无限循环。

## 目标

增强三个角色的动态任务契约，使它们在现有生命周期内具备更明确的自主推进和验证纪律：

- Planner 将 Goal 拆成可独立验证、依赖关系明确的工作单元，并为每个验收条件定义可判定反馈。
- Generator 在一个 Build round 内持续选择最小未完成步骤、实现、验证并推进，直到全部验收条件得到覆盖或出现真实外部阻塞。
- Evaluator 不把“测试通过”直接等同于“验收通过”，同时验证 verifier 的相关性、真实路径和回归覆盖。
- 大体量命令输出不污染角色上下文；结构化结果保持紧凑，完整原始输出通过可校验 Artifact 引用。

## 非目标

- 不增加第四种角色或专业化 Worker。
- 不把三个角色改成并行执行。
- 不新增固定 `.codex/agents/*.toml`、独立角色 Prompt 文件或重复规则文件。
- 不改变 Schema 3 requirement state、Schema 2 evaluation record 或 Goal Gate。
- 不允许 Root 写业务代码，也不放宽 Planner、Evaluator 的只读边界。
- 不使用“持续工作直到完美”这类不可判定的停止条件。

## 设计原则

### 动态上下文优先

角色 Prompt 继续由 Root 根据当前 Goal、状态、计划、评审、Git 和 workspace snapshot 动态组装。`SKILL.md` 只定义必须注入的输入、行为约束、停止条件和输出契约，不保存脱离当前事务的完整静态 Prompt。

### 小步推进，但不改变生命周期

“小任务”是角色内部的执行纪律，不新增 Harness phase。Planner 在 `plan.md` 中描述可独立验证的工作单元；Generator 在同一个 Build round 内循环执行这些单元；只有形成完整 handoff 后才进入 Evaluator。普通实现步骤完成不触发新 round。

### 明确且有限的停止条件

Generator 只在以下情况结束当前任务：

1. 每个 `AC-NNN` 都已有对应实现和验证覆盖，可以形成完整 handoff；
2. 出现无法从 Goal、计划、仓库规则、当前代码或可执行证据中解决的具体外部阻塞。

局部测试通过、只完成部分行为、发现与当前改动无关的普通失败，均不自动构成停止条件。Generator 必须先判断该失败是否阻止目标继续推进，并记录证据。

### Verifier 也是被审查对象

Evaluator 不只执行测试，还要确认测试确实走到目标路径、检查了相关输出，并能区分正确实现与看似合理的错误实现。若 verifier 只验证实现细节、静默跳过目标路径、只使用 mock，或没有检查用户可见结果，证据不足时应返回 `UNVERIFIED`。

## Planner Prompt 增强

保留现有输入：Goal、路径、仓库指令、live code 和执行前 snapshot。增加以下任务约束：

```text
Before finalizing the plan:

1. Decompose the Goal into the smallest independently verifiable work units.
2. For every AC-NNN, identify:
   - the observable success signal;
   - the canonical verification method;
   - a fast feedback command when available;
   - the broader regression or public real-path check.
3. Make dependencies and required execution order explicit.
4. Ensure failure output is sufficient for Generator to identify the next
   action without asking the user for routine guidance.
5. Do not use implementation details as acceptance criteria unless they are
   repository invariants required by the Goal.
```

Planner 输出仍然只有一个 `## Acceptance criteria` 章节，每条验收条件继续使用稳定 `AC-NNN`。工作单元可以出现在计划主体中，但不得引入第二套验收 ID 或独立状态机。

## Generator Prompt 增强

保留现有输入：state、plan、仓库指令和上一轮 review。增加以下执行纪律：

```text
Execution discipline:

1. Orient from the persisted Goal, plan, repository instructions, Git state,
   and previous review before changing code.
2. Break the remaining work into small, independently testable steps.
3. Select the smallest highest-signal unfinished step, implement it, verify it,
   and then select the next step.
4. Continue autonomously until every AC-NNN has implementation and verification
   coverage or a concrete external blocker prevents further safe progress.
5. A partial improvement, a passing focused test, or an unrelated existing
   failure is not by itself a stopping condition.
6. During development, prefer the fastest deterministic relevant check. Before
   handoff, run the required regression and public real-path checks.
7. Keep command output concise. Persist large logs as artifacts and report their
   paths, digests, summaries, and actionable failure lines.
8. Do not ask the user for routine implementation choices that can be resolved
   from the Goal, plan, repository conventions, or executable evidence.
```

“继续自主执行”不扩大授权范围。涉及外部发布、破坏性操作、新权限或明显超出 Goal 的设计选择时，Generator 仍必须停止并报告具体阻塞。

Generator handoff 继续记录本轮目标、变更路径、命令和结果、被评估 revision、未验证项、残余风险及下一验证目标。对于大日志，handoff 只保留高信号摘要和 Artifact 引用，不内嵌大段原始输出。

## Evaluator Prompt 增强

保留完整 evaluation transaction、冻结输入、canonical commands 和 raw evidence。增加以下 verifier 审查约束：

```text
Verifier quality:

1. Do not assume that a passing test proves the acceptance criterion.
2. Confirm that each verifier exercises the behavior claimed by its AC-NNN and
   inspects the relevant output.
3. Check for regressions outside the newly added focused tests.
4. Treat tests that only reproduce the implementation's assumptions, silently
   skip the target path, or fail to inspect user-visible output as insufficient
   evidence.
5. Use UNVERIFIED when the available verifier cannot reliably distinguish a
   correct implementation from a plausible incorrect one.
6. Keep the evaluation record compact and reference provided SHA-256-bound
   artifacts for large raw outputs. Do not create repository artifacts; use
   UNVERIFIED when required raw evidence is unavailable.
```

现有规则继续有效：用户可见 `PASS` 必须运行被评估 revision 的公共入口并检查输出；只有 unit 或 mock 证据时必须为 `UNVERIFIED`；Evaluator 不修改文件、不修复代码、不降低验收条件。

## 数据流

```text
Goal + repository truth + snapshot
  -> Planner
  -> plan.md
       - ordered verifiable work units
       - one Acceptance criteria section
       - fast and broad verification guidance
  -> Generator
       - orient
       - select smallest unfinished step
       - implement
       - run fast deterministic check
       - repeat until complete or blocked
  -> implementation.md + revision + compact evidence/artifact references
  -> begin-evaluation transaction
  -> Evaluator
       - validate verifier relevance
       - run focused, regression, and public-path checks
       - emit criterion-level PASS/FAIL/UNVERIFIED
  -> record-review
  -> existing Goal Gate
```

## 错误与阻塞处理

- 普通测试失败：Generator 使用错误信息选择下一最小步骤，不立即向用户请求指导。
- 输出过大：由 Generator 或既有验证工具保留完整日志 Artifact；Evaluator 只引用已有 Artifact，并在 evaluation record 中记录摘要、关键失败行、路径与 digest。缺失必要原始证据时返回 `UNVERIFIED`。
- Verifier 与 AC 不匹配：Evaluator 返回 `UNVERIFIED`，说明缺失的可判定证据，不自行修改测试。
- 回归失败：Evaluator 返回 `FAIL`，由现有流程将持久化 review 发回同一 Generator。
- 外部权限、破坏性操作或 Goal 外决策：角色停止并报告具体 blocker，不把“持续自主推进”解释为新增授权。
- Workspace drift：继续使用现有 snapshot、`restart-evaluation` 和 `BLOCKED` 规则，不由 Prompt 文案覆盖运行时真相。

## 验证策略

实现应先在 `tests/test_skill.py` 中增加契约测试，再修改 `SKILL.md`：

1. Planner 契约包含 independently verifiable work units、每个 `AC-NNN` 的 success signal、fast feedback 和 broader verification。
2. Generator 契约包含最小未完成步骤循环、明确停止条件、fast deterministic check 和完整 handoff 前的 regression/public-path check。
3. Generator 契约明确 partial improvement 和 passing focused test 不是停止条件。
4. Evaluator 契约明确 passing test 不自动证明 AC，并在 verifier 不足时返回 `UNVERIFIED`。
5. 大输出通过 compact summary 和 SHA-256-bound Artifact 处理。
6. 现有 Root writer boundary、角色串行、Planner-once、Schema 3 transaction、Goal Gate 和无固定角色配置测试继续通过。

完成后运行仓库现有完整测试集，并检查 `SKILL.md` 正文仍满足 1,000 words 上限。如果新增文字超限，应压缩或替换现有重叠描述，不能通过放宽测试解决。

## 修改范围

推荐最小修改范围：

- `SKILL.md`：增强三个动态角色任务契约。
- `tests/test_skill.py`：增加上述行为的回归断言。

只有在现有 README 或 CHANGELOG 对角色行为的描述因此变得不准确时才同步更新；不创建新的 Prompt 资源目录或固定角色配置。

## 选择结论

采用“增强现有动态角色契约”方案。不采用固定 Prompt 文件，也不改造为 Anthropic 实验中的并行 Worker 架构。该方案保留当前 Harness 的事务、快照、独立验收和恢复优势，同时补足长时间自主执行最关键的任务拆解、反馈质量、持续推进和 verifier 审查能力。
