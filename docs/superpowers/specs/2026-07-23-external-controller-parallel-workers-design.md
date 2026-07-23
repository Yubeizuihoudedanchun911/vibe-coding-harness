# 外部 Controller 与并行专项 Worker 设计

日期：2026-07-23

状态：已批准，待实施

## 背景

当前 Vibe Coding Harness 是一个由 `SKILL.md` 驱动的串行三角色流程：
Root 负责调用 Planner、Generator、Evaluator，`scripts/harness.py` 负责
Schema 3 requirement 状态、验收事务、快照和 Goal Gate。这个版本已经具备
较强的证据绑定和中断恢复能力，但 Agent 的启动与控制流仍依赖宿主 Skill，
且同一时刻只有一个 Generator 工作。

本设计参考 Anthropic 的
[《Building a C compiler with a team of parallel Claudes》](https://www.anthropic.com/engineering/building-c-compiler)
及其[公开仓库](https://github.com/anthropics/claudes-c-compiler)，采用外部程序
持续启动全新 Agent、并行处理可分解任务的思路。同时保留本项目已经验证过的
独立规划、独立验收、事务状态和失败闭环，并将这些能力从 Prompt/Skill 约定
下沉到可测试的 Python Controller。

## 取代关系

本设计是 Schema 4 的新产品契约。实施完成后：

- `SKILL.md` 不再是产品入口或运行时依赖，并从发布物中移除。
- `agents/openai.yaml` 中的 Skill 元数据和 Root 默认 Prompt 被移除。
- `scripts/harness.py` 的 Schema 3 CLI 被新的 `vibe` CLI 取代；可复用的
  snapshot、原子写和校验逻辑迁入 `src/vibe/`，旧命令不保留兼容 shim。
- `tests/test_skill.py` 的串行 Skill 契约测试被 Controller、Prompt Registry
  和 CLI 契约测试取代。
- 下列文档继续作为历史决策记录保留，但其运行约束由本设计取代：
  - `2026-07-14-agent-orchestrated-harness-design.md`
  - `2026-07-17-schema-v3-evaluation-transactions-design.md`
  - `2026-07-22-role-prompt-autonomy-design.md`

这不是在 Schema 3 上增加并发开关，而是一次明确的破坏性代际切换。

## 目标

- 由独立 Python Controller 直接启动和管理 Agent 进程，不依赖 Skill 或
  Root Agent 持续在线。
- Planner 生成有限、可校验的任务 DAG。
- 对依赖已满足且修改范围可证明互不冲突的任务并行启动专项 Worker。
- 每个 Worker attempt 使用全新 Agent 上下文、独立 branch 和独立 worktree。
- Controller 是运行状态、调度、验证和 Git 集成的唯一权威。
- 任务集成按 DAG 拓扑顺序串行完成，任何失败都不能污染共享集成分支。
- 通过任务级验证、全局门禁和独立 Evaluator 形成有界修复闭环。
- 前台运行可以停止、崩溃恢复和精确审计。
- Prompt 作为普通版本化模板存在，不是 Skill。
- 首个 Provider 对接 Codex CLI，同时保留稳定的 Provider Adapter 接口。

## 非目标

- 多机器分布式调度、数据库队列或事件溯源。
- 后台 daemon、Web 控制台或远程任务服务。
- V1 中交付 Codex CLI 之外的正式 Provider 实现。
- Agent 自动修改、选择或发布 Prompt。
- 第三方“集成 Agent”自动猜测语义冲突。
- 自动 merge 用户分支、push、创建 PR 或删除失败 worktree。
- Anthropic 原型中的 Worker 自主抢任务、Git task lock、自主 pull/merge/push。
- 容器级安全隔离。V1 使用同机 worktree 和 Provider 权限沙箱，隔离强度低于
  Anthropic 原型的独立 Docker 容器和完整 clone。
- Schema 3 的透明读取或静默迁移。
- 支持 dirty product baseline。Schema 4 新运行只接受明确且干净的 Git
  commit；`.vibe-coding/` 控制数据和 Git ignored 文件除外。

## 总体架构

```text
CLI
  -> Controller / StateStore
      -> PlannerRunner -> versioned Plan DAG
      -> Scheduler
          -> WorktreeManager
          -> ProviderAdapter -> parallel specialist Workers
          -> task verification
      -> Integrator -> serialized candidate verification + Git CAS
      -> GlobalVerifier
      -> EvaluatorRunner
          -> PASS -> SUCCEEDED
          -> NEEDS_REPAIR -> repair Planner -> incremental DAG -> Scheduler
          -> UNVERIFIED -> supplemental evidence
          -> BLOCKED -> PAUSED
```

### 组件职责

#### CLI

负责解析用户命令、定位目标仓库和运行目录，并调用 Controller。除
`stop.request` 外，CLI 不绕过 Controller 修改运行状态。

#### Controller

运行时唯一的编排者和 `state.json` 写入者。它负责：

- 状态迁移和 revision compare-and-swap；
- Planner、Worker、Evaluator 的进程生命周期；
- DAG 调度、路径和资源互斥；
- worktree、branch 和 candidate 管理；
- 验证、集成、修复轮次和终态判断；
- 停止请求和崩溃恢复。

#### StateStore

提供 Schema 4 校验、运行级锁、原子 Artifact 写入和原子状态替换。日志不是
状态恢复真相。

#### PromptRegistry

加载普通版本化 Prompt，校验 prompt ID/version，按角色组合不可变模板和
Controller 注入的任务数据。每次 Agent 运行都在状态和结果中记录实际版本。

#### ProviderAdapter

屏蔽 Agent Provider 的进程协议。V1 接口固定为：

```text
start(request) -> provider_handle
poll(provider_handle) -> RUNNING | SUCCEEDED | FAILED
stop(provider_handle, grace_period) -> stop_result
result(provider_handle) -> role_result
```

首个实现为 `CodexCLIAdapter`。Provider wrapper 将 stdout、stderr、最终输出、
启动身份和退出结果直接写入持久文件，不依赖 Controller 进程仍持有 pipe。

#### PlannerRunner、WorkerRunner、EvaluatorRunner

分别负责构建角色输入、启动 Provider、校验结构化输出。Runner 不自行修改
调度状态，只向 Controller 返回校验后的结果。

#### Scheduler

从 DAG 推导 ready tasks，检查依赖、路径范围、独占资源和并发上限，然后为
每个 attempt 分配独立 worktree。

#### Integrator

一次只处理一个 `READY_TO_INTEGRATE` 任务。它在临时 candidate 中应用 Worker
commits、重新验证，再通过 Git compare-and-swap 推进 run integration ref。

#### VerificationGate

运行任务级和全局确定性门禁，持久化与精确 commit 绑定的命令证据。

## 源码与发布结构

```text
pyproject.toml
src/vibe/
├── __init__.py
├── __main__.py
├── cli.py
├── controller.py
├── models.py
├── state_store.py
├── scheduler.py
├── worktrees.py
├── integrator.py
├── verification.py
├── prompt_registry.py
├── migration/
│   └── schema3.py
├── providers/
│   ├── base.py
│   └── codex_cli.py
└── runners/
    ├── planner.py
    ├── worker.py
    └── evaluator.py
prompts/
├── planner/v1.md
├── workers/base/v1.md
├── workers/implementation/v1.md
├── workers/testing/v1.md
├── workers/performance/v1.md
├── workers/code-quality/v1.md
├── workers/documentation/v1.md
├── workers/general/v1.md
└── evaluator/v1.md
schemas/
├── plan-v1.schema.json
├── worker-result-v1.schema.json
└── evaluation-v1.schema.json
```

`pyproject.toml` 提供：

```toml
[project.scripts]
vibe = "vibe.cli:main"
```

当前 3,711 行的 `scripts/harness.py` 不原样搬入单个模块。实现按上面的边界
拆分，Schema 3 只保留只读迁移解析器所需逻辑。

## CLI 与配置

### 命令

```bash
vibe run --target /path/to/repo --goal-file goal.md
vibe resume --target /path/to/repo RUN-20260723-001
vibe resume --target /path/to/repo RUN-20260723-001 --replan
vibe status --target /path/to/repo RUN-20260723-001 [--json]
vibe stop --target /path/to/repo RUN-20260723-001
vibe logs --target /path/to/repo RUN-20260723-001 \
  [--task TASK-003] [--follow]
vibe migrate --target /path/to/repo \
  (--requirement REQ-001 | --all) --base <commit>
```

`run` 也可使用 `--goal`，但 `--goal` 与 `--goal-file` 必须且只能提供一个。
目标仓库默认为当前目录；文档示例显式写出 `--target` 以避免歧义。

`run` 和 `resume` 是前台长运行命令。它们在运行进入 `SUCCEEDED`、`FAILED`、
`STOPPED`、`PAUSED` 或 `IMPORTED_READ_ONLY` 时退出。

### 项目配置

可选的仓库根目录 `vibe.json` 定义 Provider、强制验证和默认限制。运行创建时，
Controller 将解析后的有效配置冻结进 run Artifact；后续修改配置不会悄悄改变
正在进行的运行。

```json
{
  "provider": {
    "name": "codex-cli"
  },
  "scheduler": {
    "max_workers": 4
  },
  "limits": {
    "task_attempts": 3,
    "repair_rounds": 3
  },
  "verification": {
    "required_commands": [
      {
        "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"],
        "cwd": ".",
        "timeout_seconds": 900
      }
    ]
  }
}
```

命令使用 `argv[]`、受限 `cwd`、timeout 和显式环境 allowlist，默认不经过
shell。Planner 可以增加任务检查，不能删除、替换或放宽项目/CLI 冻结的强制
门禁。状态和 Artifact 不持久化 Provider secret。

## 仓库与 Git 边界

### 干净基线

`vibe run` 在创建任何 branch 前要求：

- `base_sha` 是可解析的完整 commit OID；
- tracked、staged、unstaged 和 non-ignored untracked product paths 均为空；
- `.vibe-coding/` 控制目录和 Git ignored 文件不计入 product dirty 状态；
- 当前 repository identity 与 run 记录一致。

V1 不提供 `--allow-dirty`。这与允许 dirty workspace 的 Schema 3 明确不同，
避免把用户未提交内容静默遗漏在独立 worktree 之外。

### Run ref

Controller 从 `base_sha` 创建未被用户工作区 checkout 的：

```text
refs/heads/vibe/run-<run-id>
```

用户原始 branch、index 和 working tree 在整个运行中保持不变。全局验证和
Evaluator 使用固定 integration commit 的 disposable detached worktree。

### Worker branches

每个 attempt 使用：

```text
refs/heads/vibe/<run-id>/<task-id>-a<attempt-no>
```

以及独立 worktree。Worker source commit 必须满足：

- `worker_head_sha` 从记录的 `task_base_sha` 可达；
- commit range 完整且非空；
- V1 默认禁止 merge commit；
- worktree 无未提交 product 修改；
- source commits 不包含 Controller control paths。

Controller 使用
`git diff --name-status -z <task-base>..<worker-head>` 获取实际修改，不信任
Worker 自述。rename 的源和目标、删除、gitlink 和 `.gitmodules` 都进入范围
审计。

## 持久化布局

```text
.vibe-coding/
├── runs/
│   └── RUN-20260723-001/
│       ├── state.json
│       ├── controller.lock
│       ├── config.json
│       ├── plan/
│       │   ├── plan-v001.json
│       │   ├── repair-v002.json
│       │   └── attempts/
│       │       └── 001/
│       │           ├── prompt.md
│       │           ├── launch.json
│       │           ├── stdout.log
│       │           ├── stderr.log
│       │           └── result.json
│       ├── tasks/
│       │   └── TASK-003/
│       │       ├── task.json
│       │       └── attempts/
│       │           └── 002/
│       │               ├── prompt.md
│       │               ├── launch.json
│       │               ├── stdout.log
│       │               ├── stderr.log
│       │               ├── result.json
│       │               └── verification.json
│       ├── evaluations/
│       │   └── 001/
│       │       ├── prompt.md
│       │       ├── launch.json
│       │       ├── stdout.log
│       │       ├── stderr.log
│       │       ├── result.json
│       │       └── evaluation.json
│       ├── verification/
│       │   └── global-001.json
│       ├── control/
│       │   └── stop.request
│       └── logs/
│           └── controller.jsonl
├── worktrees/
│   └── RUN-20260723-001/
└── schema3-backups/
```

`state.json` 是唯一可变的权威状态。plan、task、prompt、result、verification
和 evaluation 文件均为写后不可变 Artifact。状态只保存它们的路径和
SHA-256；同一事实不能同时在 state 与 Artifact 中独立修改。旧 plan 和已完成
task 不被 repair round 改写。

`controller.jsonl` 是可丢失的审计日志，不参与恢复判断。

## Schema 4

### Run state

下面是字段级最小形状；完整约束由实现中的模型和测试共同固定：

```json
{
  "schema_version": 4,
  "run_id": "RUN-20260723-001",
  "revision": 17,
  "goal": "实现可恢复的并行 Agent Controller",
  "repository": {
    "identity": "canonical-repository-id",
    "base_ref": "refs/heads/main",
    "base_sha": "full-commit-oid",
    "integration_ref": "refs/heads/vibe/run-RUN-20260723-001",
    "integration_head": "full-commit-oid"
  },
  "status": "EXECUTING",
  "resume_status": null,
  "plan_version": 1,
  "repair_round": 0,
  "max_repair_rounds": 3,
  "max_workers": 4,
  "controller": {
    "pid": 12345,
    "process_start_identity": "platform-specific-start-token",
    "process_group": 12345
  },
  "tasks": {},
  "pending_dispatches": {},
  "pending_integration": null,
  "latest_evaluation": null,
  "last_error": null,
  "created_at": "2026-07-23T10:00:00+08:00",
  "updated_at": "2026-07-23T10:01:00+08:00"
}
```

### Run 状态

```text
CREATED
PLANNING
EXECUTING
GLOBAL_VERIFYING
EVALUATING
REPAIRING
PAUSED
STOPPED
SUCCEEDED
FAILED
IMPORTED_READ_ONLY
```

- `PAUSED`、`STOPPED` 是可恢复静止态。
- `SUCCEEDED`、`FAILED` 是 Schema 4 最终态。
- `IMPORTED_READ_ONLY` 是 Schema 3 终态记录的只读迁移态，不能 `resume`。
- `FAILED` 不自动重开；需要以其 integration ref 为显式 base 创建新 run。

活动主路径和分支转移固定为：

```text
CREATED
  -> PLANNING
  -> EXECUTING
  -> GLOBAL_VERIFYING
  -> EVALUATING
       -> PASS -> SUCCEEDED
       -> NEEDS_REPAIR -> REPAIRING -> EXECUTING
       -> UNVERIFIED -> EVALUATING
       -> BLOCKED -> PAUSED

GLOBAL_VERIFYING failure -> REPAIRING -> EXECUTING
any active state -> PAUSED
any active state -> STOPPED
PAUSED or STOPPED -> recorded resume_status
```

进入 `PAUSED` 或 `STOPPED` 前必须把原活动状态写入 `resume_status`。恢复完成后，
Controller 先清除旧进程和 pending operation，再回到该状态对应的确定性入口；
不能只靠当前文件数量猜测恢复阶段。

### Task 与 Attempt 分层

Task 状态：

```text
PENDING -> READY -> RUNNING -> READY_TO_INTEGRATE
        -> INTEGRATING -> COMPLETED
```

异常终态为 `FAILED`、`CANCELLED`。

Attempt 状态：

```text
STARTING -> RUNNING -> VERIFYING -> SUCCEEDED
                                  -> FAILED
                                  -> CANCELLED
                                  -> ABANDONED
```

Attempt 失败且仍可重试时，本次 Attempt 以不可变 Artifact 关闭，Task 增加
`attempt_no` 后回到 `READY`。Task 只有达到语义 attempt 上限或遇到不可恢复
错误时才进入 `FAILED`。

`state.tasks` 只保存可变状态和不可变 Artifact 指针，不复制 task objective：

```json
{
  "TASK-003": {
    "task_path": "tasks/TASK-003/task.json",
    "task_sha256": "sha256:...",
    "status": "RUNNING",
    "attempt_no": 2,
    "max_attempts": 3,
    "active_attempt": {
      "attempt_token": "ATTEMPT-...",
      "status": "VERIFYING",
      "task_base_sha": "full-commit-oid",
      "branch": "refs/heads/vibe/RUN-.../TASK-003-a2",
      "worktree": ".vibe-coding/worktrees/RUN-.../TASK-003-a2",
      "provider_handle": "provider-specific-handle",
      "result_path": "tasks/TASK-003/attempts/002/result.json"
    },
    "source_commits": [],
    "integrated_commits": [],
    "last_error": null
  }
}
```

结束 Attempt 时，Controller 将其完整记录冻结到对应 attempt Artifact，
`active_attempt` 置空；state 只保留恢复所需的当前指针和 commit identity。

计数器语义严格区分：

- `provider_retry_no`：限流、短暂网络错误等启动/连接重试，不消耗代码 attempt。
- `attempt_no`：重新运行 Worker 解决同一 task 的语义尝试。
- `repair_round`：全局门禁或 Evaluator 要求修改交付物后产生的新修复 DAG。

### 状态写事务

Controller 持有 run 级 OS 文件锁。每次状态更新：

1. 校验当前完整 Schema 和预期 `revision`；
2. 先将新的不可变 Artifact 写入同目录临时文件；
3. flush、fsync 并原子 rename Artifact；
4. 在 state 中绑定 Artifact path 和 SHA-256；
5. 写入新的临时 state，flush、fsync、原子 rename；
6. fsync 所在目录；
7. `revision += 1`。

崩溃产生但未被 state 引用的 Artifact 是 orphan，不自动成为真相。

## 外部副作用事务

文件原子替换不能单独覆盖进程启动和 Git ref 更新。Schema 4 为外部副作用
保留 prepared intent。

### Agent dispatch

Controller 在启动 Provider 前，将 attempt token、角色身份、可选 task、
prompt Artifact、worktree、branch、结果路径和预期 base 写入
`pending_dispatches`，Attempt 进入 `STARTING`。Provider wrapper 使用同一
token 写 `launch.json` 和最终结果。该协议同样覆盖 Planner、Worker 和
Evaluator，不只覆盖代码任务。

恢复时：

- wrapper 身份和进程启动 token 匹配且仍存活：继续 poll；
- 进程已结束且结果完整：校验并接收结果；
- 无可靠身份或输出不完整：将旧 Attempt 标为 `ABANDONED`，检查是否已有合法
  commit，再决定接收或创建新 Attempt；
- PID 相同但启动身份不同：视为 PID 复用，不得发送信号或接管。

晚到结果必须匹配当前 attempt token；过期 token 的输出只归档，不改变状态。

### Integration transaction

candidate 验证通过后，Controller 先持久化：

```json
{
  "pending_integration": {
    "operation_id": "INT-0003",
    "task_id": "TASK-003",
    "attempt_no": 2,
    "expected_head": "old-integration-sha",
    "candidate_head": "verified-candidate-sha",
    "source_base": "task-base-sha",
    "source_head": "worker-head-sha",
    "verification_path": "tasks/TASK-003/attempts/002/verification.json",
    "verification_sha256": "sha256:..."
  }
}
```

然后执行：

```bash
git update-ref <integration-ref> <candidate-head> <expected-head>
```

成功后再将 Task 标记为 `COMPLETED`、记录 integrated commits、更新
`integration_head` 并清除 marker。

`resume` 只允许三种判定：

- ref 等于 `expected_head`：candidate 仍有效时重试 CAS；
- ref 等于 `candidate_head`：补齐状态提交；
- ref 是其他值：进入 `PAUSED`，不猜测、不强制覆盖。

## Planner 计划契约

Planner 输出单个符合 `plan-v1.schema.json` 的 JSON 对象：

```json
{
  "schema_version": 1,
  "plan_version": 1,
  "summary": "实现目标的有限任务计划",
  "acceptance_criteria": [
    {
      "id": "AC-001",
      "description": "用户可观察的成功条件"
    }
  ],
  "global_verification": [],
  "tasks": [
    {
      "id": "TASK-003",
      "objective": "补充并发调度测试",
      "worker_type": "testing",
      "covers": ["AC-001"],
      "depends_on": ["TASK-001"],
      "path_scope": ["tests/scheduler/"],
      "exclusive_resources": [],
      "acceptance_checks": [
        {
          "argv": ["python3", "-m", "pytest", "tests/scheduler"],
          "cwd": ".",
          "timeout_seconds": 300
        }
      ],
      "max_attempts": 3
    }
  ]
}
```

Controller 在接受计划前校验：

- task ID、acceptance ID 唯一且格式稳定；
- 所有依赖存在，DAG 无环；
- 每个 acceptance criterion 至少被一个 task 覆盖；
- `worker_type` 来自注册表；
- path scope 合法，命令满足结构化执行约束；
- attempt 和 task 数量在运行上限内；
- Planner 没有删除项目强制门禁。

Repair plan 使用递增 `plan_version`，只追加未完成的修复任务和依赖；旧任务、
旧评估和旧计划保持不可变。

初始 Planner 在 `base_sha`、repair Planner 在当前 `integration_head` 的
disposable detached worktree 中运行。Controller 在调用前后核对 tracked
diff、Git ref 和 product status；Planner 修改 tracked source、创建 commit
或移动 ref 时，本次输出无效。disposable worktree 会被隔离，不允许 Planner
直接写 `state.json` 或 plan Artifact，也不允许 repair plan 削弱既有 Goal 或
acceptance criteria。

## 并行调度

### Ready 条件

Task 只有同时满足以下条件才能进入 `READY`：

- 所有 `depends_on` task 已经 `COMPLETED` 并进入 integration ref；
- 与当前活跃 task 的 path scope 可证明不重叠；
- `exclusive_resources` 与活跃 task 不相交；
- 尚未达到 `max_workers`；
- run 不处于停止、暂停或失败状态。

默认 `max_workers=4`，可由冻结配置或 CLI 调整。

### Path scope 语义

V1 不接受难以判定交集的任意 glob。每个 scope 是规范化的 repo-relative：

- 精确文件路径；或
- 以 `/` 结尾的目录前缀；或
- `.`，表示整个 product repo，并强制独占运行。

拒绝绝对路径、`..`、空路径、control root 和指向仓库外的路径。两个目录存在
前缀包含关系、文件相同、文件落入另一目录时都视为重叠。无法证明不相交时
保守串行。

`exclusive_resources` 是可选的逻辑名称，用于端口、数据库、缓存或测试设备等
非文件共享资源；名称相同即互斥。

### Fresh-context 原则

每个 Attempt 都启动全新 Agent 上下文。所谓“交回原 Worker”是保持同一个
task identity 和 `worker_type`，将最新 base、冲突和验证证据交给新的 Agent
Attempt，不复用隐式聊天记忆。

## 专项 Worker 与 Prompt

### Prompt 组合

Worker Prompt 的顺序固定为：

1. `workers/base@v1`；
2. 一个专项 overlay；
3. 仓库适用指令；
4. Controller 生成的 task contract；
5. 当前 base、允许路径、验证命令和前次失败证据；
6. `worker-result-v1` 输出 schema。

所有动态数据作为清晰分隔的数据块注入，不允许仓库内容覆盖 Controller
权限边界。

### Planner Prompt 核心

```text
You are the read-only planning agent for one Vibe Controller run.

Inspect the live repository, tests, and applicable repository instructions
before producing a plan. Do not modify files, create commits, change Git refs,
or execute the plan.

Return one finite acyclic task graph. Every task must declare its objective,
acceptance criteria coverage, dependencies, specialist worker type, exact path
scope, exclusive resources, structured verification commands, and bounded
attempt count. Plan parallel tasks only when their scopes and resources can be
proven independent. Optimize for safe useful parallelism, not maximum task
count. Return exactly one JSON object matching the supplied plan schema.
```

### Worker 基础 Prompt 核心

```text
You are assigned exactly one task and one isolated Git worktree by the external
Vibe Controller.

Inspect the live task base before changing code. Complete only the assigned
task. Modify only the declared path scope, honor repository instructions, run
the supplied checks, and create non-merge source commits on your task branch.

Do not select another task, modify Controller state, touch control paths, merge
the integration branch, push, publish, or coordinate through shared task
files. A partial improvement is not completion. If the task cannot be completed
safely, return a concrete blocker and evidence instead of claiming success.
Return exactly one JSON object matching the supplied worker-result schema.
```

专项 overlay：

| `worker_type` | 增量契约 |
|---|---|
| `implementation` | 完成生产行为，并增加任务要求的针对性测试 |
| `testing` | 优先构造可复现、高判别力的测试；没有明确授权时不改生产行为 |
| `performance` | 记录前后测量和测试条件；正确性优先，不做无基准优化 |
| `code-quality` | 做有边界、可验证的简化；默认保持外部行为不变 |
| `documentation` | 以 live code、CLI 和测试为依据，不写无法验证的声明 |
| `general` | 无适用专项时严格按 task contract 完成 |

Worker 输出包含 task/attempt identity、`COMPLETED | BLOCKED`、source commit
range、changed paths、checks、residual risks 和 blocker。Controller 从 Git 和
进程结果独立重建关键事实，不信任 Agent 自报 commit 或测试结论。

### Evaluator Prompt 核心

```text
You are the independent acceptance evaluator for one immutable integration
commit.

Judge the final integrated snapshot against the original goal and acceptance
criteria, not against task completion labels. Inspect the frozen plan, Git
diff, source commits, task results, and Controller-produced verification
evidence. Do not modify tracked files, create commits, change Git refs, repair
code, lower criteria, or schedule workers.

Return PASS only when every criterion has direct relevant evidence and no
blocking defect remains. Use NEEDS_REPAIR for a product or test defect,
UNVERIFIED when the available evidence cannot distinguish a correct result
from a plausible incorrect one, and BLOCKED for an external condition that
prevents evaluation. Return exactly one JSON object matching the supplied
evaluation schema.
```

Evaluator 使用固定 commit 的 disposable detached worktree。确定性 canonical
commands 由 Controller 运行；Evaluator 如需额外执行检查，也只能在该 disposable
worktree 内执行。Controller 对 Evaluator 前后 tracked diff 做审计，发现源码变更
即拒绝本次结果。

## 任务验证与集成

### Worker 自验证

Worker 在 task worktree 中运行任务检查，并在结果中报告命令和输出。该结果只
是 handoff，不是集成授权。

### Controller candidate 验证

Worker 成功返回后，Controller：

1. 验证 branch、base、commit range、clean worktree 和 path/resource contract；
2. 从当前 integration head 创建临时 candidate branch/worktree；
3. 按顺序 cherry-pick 完整 source commit range；
4. 拒绝冲突、merge commit、越界路径和 control path 修改；
5. 在 candidate 上重跑 task checks 和不可删除的项目门禁；
6. 冻结与 candidate SHA 绑定的 verification Artifact；
7. 准备 `pending_integration` 并通过 CAS 推进 run ref。

若 cherry-pick 冲突、越界或复验失败：

- candidate 被放弃；
- integration ref 保持不变；
- 当前 Attempt 以失败证据关闭；
- Task 在未超限时基于最新 integration head 创建新 Attempt；
- 不启动第三方 Agent 代替 Worker 解决冲突。

## 全局验证与 Evaluator

全部当前计划任务集成后，Controller 在 integration head 的 disposable detached
worktree 中运行：

- 冻结配置中的 required commands；
- Planner 只能追加的 global verification；
- repository clean、ref 和状态一致性检查。

每个证据记录：

- integration commit；
- argv、cwd、允许的环境；
- 开始/结束时间和 timeout；
- exit code；
- stdout/stderr Artifact path 和 SHA-256。

确定性门禁失败时不允许成功验收，直接进入 repair planning。

全局门禁通过后，Controller 启动全新 Evaluator。Controller 为评估结果添加
不可由 Agent 伪造的 envelope，至少绑定：

- run ID 和 evaluation round；
- goal、plan、task-result 和 verification manifest 的 SHA-256；
- integration head；
- Planner、Worker、Evaluator Prompt 版本；
- Evaluator 原始输出 SHA-256。

Evaluator verdict 路由：

- `PASS`：进入最终 Goal Gate；
- `NEEDS_REPAIR`：将 findings 和证据交给 Planner repair mode；
- `UNVERIFIED`：在同一 integration commit 上补证或重新评估，不增加
  repair round；
- `BLOCKED`：运行进入 `PAUSED`。

Evaluator finding 必须包含 criterion、严重度、可定位证据、受影响路径和
repair hint。Evaluator 不直接生成调度任务；Planner 负责将有效 finding
转换为增量 DAG。

## 修复闭环与成功条件

默认限制：

- 每个 Task 最多 3 个语义 Attempt；
- 全局最多 3 个 repair round；
- Provider transient retry 使用独立上限，不消耗 repair round。

Repair plan 基于当前 integration head 创建，不重写已完成历史。修复任务仍走
并行调度、candidate 验证、串行集成、全局门禁和独立 Evaluator 的完整流程。

Run 只有同时满足以下条件才进入 `SUCCEEDED`：

- 所有 plan version 中进入执行范围的任务均已 `COMPLETED`；
- integration ref 等于状态记录的 integration head；
- 全局 required commands 全部通过；
- 最新 Evaluator verdict 为 `PASS`；
- 所有 acceptance criteria 有直接证据；
- 没有 unresolved blocker、pending dispatch 或 pending integration；
- 最终 state 已原子落盘。

成功不会自动 merge 或 push 用户分支。

## Stop、恢复与错误处理

### Stop 协议

活跃 Controller 长时间持有 run lock，因此另一个 `vibe stop` 不能直接修改
`state.json`。`stop`：

1. 校验 state 中的 PID、进程启动身份和 process group；
2. 生成唯一 nonce，将 run ID、观察到的 state revision、请求时间和 nonce
   原子写入独立的 `control/stop.request`；
3. 向身份仍匹配的 Controller 发送通知信号；
4. 由持锁 Controller 停止领取新任务；
5. 通过 Provider Adapter 请求终止活跃 Agent；
6. 等待或强制终止进程组，归档晚到输出；
7. Controller 将 run 原子标记为 `STOPPED`。

若 Controller 已死亡，`stop` 获取 run lock 后执行同样的恢复性停止。只有确认
相关 Provider 进程已经终止或不再属于该 Attempt，才能写 `STOPPED`。
Controller 在状态中绑定已消费的 stop nonce，再将请求归档为不可变 receipt；
重复提交同一 nonce 是幂等操作，过期或已消费请求不能影响后续 `resume`。

### Resume 协议

`resume` 获取 run lock，先校验完整 Schema，再核对：

- repository identity 和 integration ref；
- controller/process identity；
- pending dispatches、launch/result Artifact 和活跃进程；
- worktree 与 branch base；
- pending integration 与实际 ref；
- latest verification/evaluation 绑定的 commit；
- stop request 和重试上限。

恢复只依据持久状态、不可变 Artifact 和 Git，不依据聊天上下文或审计日志。

### 错误分类

| 错误 | 处理 |
|---|---|
| 限流、短暂网络失败 | Provider 退避重试，不消耗语义 Attempt |
| Agent 超时或无效输出 | 关闭 Attempt；未超限时启动全新上下文 |
| Provider 认证或配置错误 | `PAUSED`，环境修复后 `resume` |
| 必需命令不存在 | `PAUSED`，不把缺门禁视为通过 |
| Worker 路径越界或验证失败 | 拒绝集成，附证据创建新 Attempt |
| Git cherry-pick 冲突 | integration ref 不变，交回同类型新 Attempt |
| integration ref 被外部移动 | `PAUSED`，绝不 force update |
| Evaluator `UNVERIFIED` | 同 commit 补证，不增加 repair round |
| Evaluator `BLOCKED` | `PAUSED` |
| Task/repair 上限耗尽 | `FAILED` |
| Schema 或状态不变量破坏 | fail closed；能安全落盘时记为 `FAILED`，否则只读报告 |

## Schema 3 显式迁移

Schema 4 CLI 发现 `.vibe-coding/requirements/REQ-NNN/` 时不会直接运行旧状态。
用户必须显式执行：

```bash
vibe migrate --target /path/to/repo --requirement REQ-001 --base <commit>
```

或：

```bash
vibe migrate --target /path/to/repo --all --base <commit>
```

### 迁移前置条件

- 对每个旧 requirement 执行完整 Schema 3 只读验证；
- 损坏、缺失或篡改的旧事务默认拒绝迁移，不“尽力修复”；
- `--base` 必须解析为明确 commit；
- ACTIVE/BLOCKED requirement 要继续执行时，目标 product workspace 必须满足
  Schema 4 clean baseline 规则；
- Schema 3 dirty snapshot 可以归档，但不能静默成为 worktree base。

### 映射

| Schema 3 status | Schema 4 结果 |
|---|---|
| `ACCEPTED` | `IMPORTED_READ_ONLY`，保留 accepted revision 和完整证据 |
| `DEGRADED` | `IMPORTED_READ_ONLY`，保留用户 degradation acceptance |
| `ACTIVE` | 新 run 为 `PAUSED`，reason=`SCHEMA3_REPLAN_REQUIRED` |
| `BLOCKED` | 新 run 为 `PAUSED`，保留 blocker，reason=`SCHEMA3_REPLAN_REQUIRED` |

ACTIVE/BLOCKED 迁移不会伪造历史 DAG。用户执行：

```bash
vibe resume --target /path/to/repo <new-run-id> --replan
```

Planner 才会基于明确的 clean base 和旧 Goal 生成第一份 Schema 4 plan。

### 备份与幂等

- 不原地修改 `.vibe-coding/requirements/**`。
- 每个迁移在 `.vibe-coding/schema3-backups/<migration-id>/` 保存完整 requirement
  副本和 migration manifest。
- manifest 记录 source requirement、Schema 3 校验结果、源 Artifact hashes、
  `--base`、目标 run ID 和迁移时间。
- 相同 source identity 和 manifest hash 的重复迁移返回既有结果。
- 同一 requirement 指向不同 base 或内容变化时拒绝复用旧 migration，要求用户
  显式处理冲突。
- `--all` 按 REQ 编号确定性处理；任何一项准备失败时，不提交该批次的新 run
  映射。

Schema 3 的 evaluation-record Schema 2、interruption record、失败轮次、
attempts 和 hash receipts 原样归档，不转换成伪造的 Schema 4 task/evaluation。

## 与 Anthropic C Compiler 原型的差异

| 维度 | Anthropic 原型 | 本设计 |
|---|---|---|
| 控制层 | 简单外部无限循环，每轮读取 Prompt 启动新 session | 可恢复的中央事务 Controller |
| 角色 | 公开基础 harness 使用通用 Prompt；另有人为指定的性能、代码质量、文档等专项职责，但没有公开正式角色契约 | Planner、版本化专项 Worker、Evaluator |
| 规划 | 没有 orchestration agent；Worker 自主选择下一个明显问题 | Planner 生成有限 DAG，Controller 验证和调度 |
| 协调 | Worker 在 `current_tasks/` 写 Git-tracked lock，自主 pull、merge、push 和解锁 | Controller 状态事务、路径/资源互斥、独立 worktree |
| 隔离 | 每个 Agent 使用独立 Docker 容器和完整 clone | 同机独立 worktree，依赖 Provider 沙箱 |
| 集成 | Worker 自行合并，频繁冲突并自行处理 | candidate 验证后串行 Git CAS，冲突交回新 Attempt |
| 反馈 | 高质量测试 harness、回归 CI 和人工设计 verifier；未描述独立 Evaluator Agent | 两级确定性门禁和独立 Evaluator |
| GCC oracle | Linux kernel 阶段用于差分定位和重新拆分失败空间 | 不内置语言 oracle，由项目配置提供领域 verifier |
| 生命周期 | 围绕固定总体目标无限持续改进 | 有限 DAG、Attempt/repair 上限和明确成功终态 |

本设计保留的核心经验是：外部循环、fresh context、并行独立 Worker、快速且
高信号反馈、完整日志外置、真实测试和 Git commit 证据。专项化本身并非新增；
新增的是专项职责的契约化与自动分配、中央事务状态、Planner DAG、路径/资源
互斥、独立 Evaluator 和有界修复闭环。

## 安全与授权边界

- Planner：product source read-only，只生成 plan Artifact。
- Worker：只能修改分配 worktree 中声明的 product path scope。
- Evaluator：source read-only；使用 disposable worktree 并接受前后审计。
- Controller：唯一状态写入者和 Git integration ref 管理者。
- `stop` CLI 只能写 stop request；不能与 Controller 双写 state。
- 所有 Agent 都不得 push、发布、创建外部资源或扩大用户授权。
- Controller 不执行 destructive cleanup；失败 worktree 和证据保留给用户。
- 任意外部权限、发布或超出 Goal 的决定进入 `PAUSED`，不由 Agent 自行推断。

## 测试策略

### 单元测试

- Schema 4 模型、枚举、revision 和状态迁移不变量；
- Task/Attempt/provider retry/repair round 的独立计数；
- DAG 拓扑、缺失依赖、cycle 和 acceptance coverage；
- path scope 规范化、重叠、rename 两端和 exclusive resources；
- Prompt 组合顺序、版本绑定和结构化输出校验；
- Provider 错误分类和 stale attempt token；
- Schema 3 status 映射和 migration manifest 幂等性。

### Git 集成测试

使用真实临时仓库覆盖：

- run ref、task branch 和 worktree 生命周期；
- source commit ancestry、merge commit 拒绝和完整 range；
- candidate cherry-pick、任务复验和成功 CAS；
- conflict、path escape、gitlink、`.gitmodules` 和外部 ref 移动；
- `update-ref` 成功但 state 未提交时的恢复；
- 用户 base branch、index 和 working tree 始终不变。

### Fake Provider 端到端测试

Fake Provider 可编程返回 Planner、各类 Worker 和 Evaluator 结果，覆盖：

- 两个无依赖、路径和资源不重叠的任务真实并行；
- 路径或资源重叠任务强制串行；
- invalid JSON、timeout、rate limit、auth failure 和晚到结果；
- task failure -> 新 Attempt；
- global failure -> repair plan；
- `UNVERIFIED` -> 同 commit 补证；
- repair 后 `PASS` -> `SUCCEEDED`；
- attempt/repair 上限 -> `FAILED`。

### 故障注入测试

在以下位置终止 Controller：

- state intent 已写、Provider 尚未启动；
- Provider 已启动、launch/result 尚未绑定；
- Worker 已提交、task verification 尚未绑定；
- candidate 已验证、`pending_integration` 已写；
- `git update-ref` 已成功、state 尚未完成；
- stop request 已写、活跃 Worker 尚未退出。

每种情况都必须证明 `resume` 不重复启动已完成工作、不重复集成、不误杀 PID
复用进程，也不接受不完整 Artifact。

### CLI 与迁移测试

- `run/resume/status/stop/logs/migrate` 的人类输出和 `--json`；
- run lock 和并发 stop 协议；
- Schema 4 明确拒绝直接读取 Schema 3；
- 单 requirement、`--all`、重复迁移和批次失败；
- ACCEPTED/DEGRADED 只读导入；
- ACTIVE/BLOCKED 必须 `--replan`；
- dirty Schema 3 snapshot 只归档、不变成执行 base。

### Codex CLI 冒烟测试

真实 `CodexCLIAdapter` 测试由显式环境开关启用，不进入默认离线 CI，避免依赖
账号、网络和调用费用。默认 CI 必须通过 Fake Provider 覆盖完整控制流。

## 文件影响

| 路径 | 实施动作 |
|---|---|
| `pyproject.toml` | 新增 package 和 `vibe` console entry point |
| `src/vibe/**` | 新增 Controller、状态、调度、Git、验证、Provider 和迁移模块 |
| `prompts/**` | 新增版本化 Planner、专项 Worker、Evaluator Prompt |
| `schemas/**` | 新增结构化 Agent 输出 schema |
| `SKILL.md` | 移除 |
| `agents/openai.yaml` | 移除 Skill 元数据 |
| `scripts/harness.py` | 在迁移逻辑转移后移除 |
| `tests/test_skill.py` | 移除并由 Prompt/Controller 契约测试替代 |
| `tests/test_harness.py` | 拆分；复用精确 snapshot、原子写和边界测试 |
| `tests/test_repository_health.py` | 改为检查 Python package、Prompt 和单一 CLI 入口 |
| `README.md`、`README.zh-CN.md` | 改为外部 CLI 安装、配置、运行和迁移说明 |
| `CONTRIBUTING.md` | 更新架构不变量和测试入口 |
| `CHANGELOG.md` | 记录 Schema 4 breaking change |
| `.github/workflows/ci.yml` | 从 Skill/runtime script 检查切换到 package 测试 |

## 准出标准

实现完成必须同时证明：

1. 运行入口不读取 `SKILL.md`，也不依赖 Root Agent。
2. Planner、Worker、Evaluator 均由 Controller 通过 Provider Adapter 启动。
3. Planner 计划是有限、无环、路径和验收覆盖可校验的 DAG。
4. 无冲突任务实际并行，有冲突任务稳定串行。
5. 每个 Worker Attempt 使用全新上下文、branch 和 worktree。
6. 越界、冲突或失败 candidate 从不污染 integration ref。
7. 集成 crash window 能由 prepared marker 和 Git ref 唯一恢复。
8. 两级验证和 Evaluator 都绑定同一个 integration commit。
9. `UNVERIFIED` 不伪装为代码缺陷或成功，`BLOCKED` 不消耗 repair round。
10. `stop` 与 Controller 不会双写状态，`resume` 不依赖易失聊天上下文。
11. 用户原始 branch、index、working tree 和远端始终不被自动修改。
12. Schema 3 只能显式、可审计、可重复地迁移，原始证据完整保留。
13. 默认离线测试覆盖并行、修复、故障注入和迁移完整路径。

## 选择结论

采用“中央事务状态机 + 外部 Provider Controller + Planner DAG + 并行专项
Worker + 串行候选集成 + 独立 Evaluator”的方案。

它不像 Anthropic 原型那样把任务选择、锁、合并和持续性都交给 Worker，也不再
像当前版本一样由 Skill Root 串行推进。Agent 只承担最适合模型判断的规划、
专项实现和独立评估；并发安全、状态、Git、验证和恢复由确定性外部控制层负责。
