# Agent 编排型 Vibe Coding Harness 设计

日期：2026-07-14

## 目标

将 Vibe Coding Harness 调整为面向长期 AI Coding 的最小三角色编排器：Root 只负责调度、Goal Gate 和持久化追踪；Planner、Generator、Evaluator 均由独立于 Root 的角色 Agent 承担；Planner 每个需求运行一次，Generator 与 Evaluator 根据评审结果循环，直到通过、阻塞或用户接受降级。

设计遵循 Anthropic harness 的两个核心结论：用持久化文件和 Git 在会话之间交接上下文；Planner 负责一次性扩展产品规格，正常迭代发生在 Build/QA 之间，而不是每轮重新规划。

参考：

- [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps)
- [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

## 非目标

- 不创建全局 `progress.md`。
- 不恢复固定 Sprint、通用验收门禁或语言规则脚手架。
- 不让 Root 修改业务代码，也不在 multi-agent 不可用时回退为单 Agent 实现。
- 不为尚未产生的阶段创建空 Markdown 占位文件。
- 不为正常修复轮次重复启动 Planner。
- 不为每个 Build/QA round 重建角色 Agent；同一需求内复用相互隔离的 Generator 和 Evaluator 会话。

## 需求目录

每个 Goal 对应一个独立需求目录：

```text
.vibe-coding/
└── requirements/
    └── REQ-001/
        ├── state.json
        ├── plan.md
        └── rounds/
            ├── 001/
            │   ├── implementation.md
            │   └── review.md
            └── 002/
                ├── implementation.md
                └── review.md
```

`state.json` 在初始化时创建。`plan.md`、`implementation.md` 和 `review.md` 只在对应 Agent 产生有效结果后创建。

需求编号按仓库内已有编号递增。终态需求保留在原目录；初始化新需求时不得覆盖既有记录。

## 最小状态

```json
{
  "schema_version": 2,
  "requirement_id": "REQ-001",
  "goal": "用户可见目标",
  "status": "ACTIVE",
  "phase": "BUILDING",
  "active_round": 2,
  "next_action": "修复上一轮发现的持久化问题",
  "last_good_revision": "git-sha",
  "latest_verdict": "FAIL",
  "residual_risks": []
}
```

字段约束：

- `status` 只允许 `ACTIVE`、`BLOCKED`、`DEGRADED`、`ACCEPTED`。
- `phase` 在活动需求中只允许 `PLANNING`、`BUILDING`、`EVALUATING`。
- `latest_verdict` 只允许空值、`PASS`、`FAIL`、`UNVERIFIED`。
- `active_round` 从 1 开始，只在 Evaluator 返回 `FAIL` 且需要再次实现时递增。
- `DEGRADED` 必须带有非空 `degradation_acceptance`，记录用户明确接受。
- `ACCEPTED` 必须绑定当前 Git revision，并有当前轮 `review.md` 的 PASS 证据。

路径由 `requirement_id` 和 `active_round` 推导，不在状态中重复保存。

## Agent 职责

### Root

- 创建或恢复需求状态。
- 为需求启动相互独立的 Planner、Generator 和 Evaluator 角色 Agent；等待当前角色任务完成后再调度下一角色。
- 校验 Agent 输出是否完整、是否仍与 Goal 一致。
- 将 Agent 返回的结构化结果写入需求目录，并更新 `state.json`。
- 在 Evaluator PASS 后执行 Goal Gate；Root 不替代 Evaluator 重新测试实现，也不修改业务代码。

### Planner

- 每个需求启动一个新的只读 Agent。
- 读取用户目标、仓库指令、现有实现和约束。
- 生成一次自包含 `plan.md`：目标、范围、非目标、交付行为、高层设计和可验证验收条件。
- 不提前指定没有证据支撑的细粒度实现，不修改仓库文件。

只有用户目标发生变化，或证据表明产品规格本身错误时，Root 才重新进入 `PLANNING`。这属于异常重启，不是正常迭代状态。

### Generator

- 每个需求使用一个独立的 workspace-write Generator Agent；后续 Build round 通过新任务继续调度同一角色会话。
- 读取 `state.json`、`plan.md`；修复轮额外读取上一轮 `review.md`。
- 作为唯一业务代码写入者实施最小变更，运行聚焦测试和相关真实链路。
- 在仓库规则允许时创建仅包含本轮变更的 scoped commit，并在交接中记录被评估的 revision；未形成可识别 revision 时不得进入最终 `ACCEPTED`。
- 返回自包含实现交接，由 Root 写入当前轮 `implementation.md`：本轮目标、变更路径、命令与结果、未验证项、残余风险和下一验证目标。

### Evaluator

- 每个需求使用一个独立于 Generator 的只读 Evaluator Agent；后续 QA round 通过新任务继续调度同一角色会话。
- 读取 `state.json`、`plan.md`、当前 `implementation.md`、实际 Git diff 和原始测试证据。
- 每轮任务只提供当前验收输入；持久化文件是恢复真相，既有聊天上下文不得覆盖文件证据。
- 按计划中的验收条件进行真实验证，返回 criterion-level 证据与 `PASS`、`FAIL` 或 `UNVERIFIED`。
- Root 将结果写入当前轮 `review.md`。Evaluator 不修代码、不降低验收标准。

## 正常流转

```text
PLANNING
  → Planner 输出 plan.md
  → BUILDING Round 1
  → Generator 输出 implementation.md
  → EVALUATING Round 1
  → Evaluator 输出 review.md
      ├─ PASS → Root Goal Gate → ACCEPTED
      ├─ FAIL → BUILDING Round 2 → Generator → Evaluator → ...
      └─ UNVERIFIED → 保持当前轮 EVALUATING，向 Evaluator 发送补证任务
```

Planner 不参与普通 Build/QA 修复循环。

## Skill 启动协议

Skill 不使用 shell 脚本模拟 Agent。Root 必须通过宿主提供的 multi-agent 工具启动角色。在 Codex 中先用 `spawn_agent` 创建三个相互隔离的角色 Agent，再通过后续任务调度同一需求的 Build/QA 轮次，并等待每个角色任务完成。

- Planner：独立 Agent，read-only 指令，只执行需求级规划。
- Generator：独立 Agent，workspace-write 指令，每轮任务包含当前 `plan.md` 和上一轮评审路径。
- Evaluator：独立 Agent，read-only 指令，每轮任务包含验收条件、当前实现记录、diff 和可执行验证命令。

三个角色不得同时执行任务。需求终态后释放角色 Agent；若宿主不支持释放，则不再向它们发送任务。若 multi-agent 能力不可用，Root 将需求标记为 `BLOCKED` 并报告原因，不回退为业务代码写入者。

## 恢复协议

1. 扫描 `.vibe-coding/requirements/*/state.json`。
2. 只有一个非终态需求时自动恢复；存在多个时要求显式指定需求编号。
3. 校验状态、需求路径和受管文件均位于仓库内且不是符号链接。
4. 读取 `state.json`、`plan.md` 和当前轮已存在的文件。
5. 当 `phase=BUILDING` 且 `active_round>1` 时，额外读取上一轮 `review.md`。
6. 检查 Git status、`last_good_revision` 和 `next_action`。
7. 根据 `phase` 调度对应角色；只有该角色尚未创建或已不可用时才创建新的角色 Agent。

## 失败处理

- Agent 中断或未返回有效结果：保持阶段和轮次，重新调度同一角色；角色会话不可用时才创建替代 Agent。
- Generator 未留下可验证交接：保持 `BUILDING`，不得启动 Evaluator。
- Evaluator `FAIL`：记录评审，递增轮次并返回 `BUILDING`。
- Evaluator `UNVERIFIED`：不递增轮次，保留 `EVALUATING` 并向同一 Evaluator发送补证任务；新的 attempt 追加到当前 `review.md`，不得覆盖既有记录。
- 状态或必需文档无效：停止调度并标记或报告 `BLOCKED`，不得猜测恢复。
- 用户明确接受有缺口交付：记录 `degradation_acceptance` 后才允许 `DEGRADED`。

## 验证策略

实现测试至少覆盖：

- 初始化按顺序生成新的 `REQ-xxx/state.json`，且不创建空阶段文档。
- `PLANNING` 完成后必须存在非空 `plan.md` 才能进入 `BUILDING`。
- 当前轮存在有效 `implementation.md` 才能进入 `EVALUATING`。
- `FAIL` 递增轮次，`UNVERIFIED` 保持当前轮，`PASS` 才允许进入 Goal Gate。
- Planner 只在首次规划或产品规格异常重启时调度。
- Planner、Generator、Evaluator 分别由独立角色 Agent 承担；同一需求内 Generator/Evaluator 按轮次复用各自会话，Root 不写业务代码，Writer 不重叠。
- 多个非终态需求必须显式选择，恢复不得猜测。
- `ACCEPTED` 需要当前轮 PASS 评审和当前 Git revision。
- 受管目录或文件的符号链接不能将读写引向仓库外。
- Skill 包不重新引入固定 Agent 配置、全局进度文件或通用治理模板。

## 实现范围

- 重写 `SKILL.md` 的启动、角色、流转、恢复和验收协议。
- 将 `scripts/harness.py` 从全局 `state.json + progress.md` 改为按需求目录管理。
- 更新 `agents/openai.yaml` 的默认提示，明确 Root 是 orchestrator。
- 重写 CLI 与 Skill 结构测试，以覆盖新状态和文件边界。
