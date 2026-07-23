# Vibe Coding Harness

[![CI](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

[English](README.md)

## 它是什么

Vibe Coding Harness 是一个运行于 Git 仓库外部、可恢复的 Controller。
安装后的 `vibe` CLI 负责调度 Planner、并行专项 Worker 和独立 Evaluator，
并把 Schema 4 状态持久化到目标仓库；聊天上下文不是恢复真相源。

## 架构

```text
目标 -> Planner -> 有限依赖 DAG
                   |
                   +-> 隔离 Attempt 中的专项 Worker
                   |      （仅在路径和资源安全时并行）
                   v
              串行候选验证 + Git CAS
                   |
                   v
              独立 Evaluator
                   |
       PASS / NEEDS_REPAIR / UNVERIFIED / BLOCKED
```

Controller 是状态的唯一写入者。每个 Worker Attempt 使用新的 worktree、
分支、Provider 进程和提示词上下文。候选提交经过串行验证后，只能通过
compare-and-swap 更新 run ref；有界修复循环会把发现交给新的 Planner
操作。

## 环境要求

- Git
- Python 3.10 或更高版本
- Provider adapter 可调用 Codex CLI

产品仓库必须从已提交的 clean 基线开始，不提供 `--allow-dirty`。

## 安装

```bash
git clone \
  https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness.git
cd vibe-coding-harness
python -m pip install .
vibe --help
```

## 配置

可选的仓库级 `vibe.json` 用于配置并发、重试上限和显式验证命令：

```json
{
  "scheduler": {"max_workers": 4},
  "limits": {
    "task_attempts": 3,
    "provider_retries": 3,
    "evidence_rounds": 3,
    "repair_rounds": 3,
    "max_plan_tasks": 128
  },
  "verification": {
    "command_catalog": [
      {
        "id": "unit",
        "purpose": "运行离线单元测试",
        "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
        "cwd": ".",
        "timeout_seconds": 900,
        "env_allowlist": []
      }
    ],
    "required_command_ids": ["unit"]
  }
}
```

非空项目命令必须显式传入 `--allow-project-commands`。命令来源摘要和
授权模式会冻结到每个 run。

## 启动

```bash
vibe run --target /path/to/repo \
  --goal "创建并验收一个个人记账 MVP"
```

也可用 `--goal-file` 读取目标文件。前台运行会创建
`.vibe-coding/runs/RUN-YYYYMMDD-NNN/` 和私有的
`refs/heads/vibe/run-*` 集成引用。

## 恢复

```bash
vibe resume --target /path/to/repo RUN-20260723-001
```

前台进程中断以及 `vibe stop` 都可恢复。`FAILED` 是终态，不能 resume：
检查失败后，切换到期望的 clean commit，再显式执行新的 `vibe run`，
让新 run 记录新的基线。

从 Schema 3 导入的 ACTIVE/BLOCKED 需求需要显式重新规划：

```bash
vibe resume --target /path/to/repo RUN-20260723-002 --replan
```

只有迁移已完成、Schema 4 计划为空且当前产品基线 clean 时才允许执行。

## 状态

```bash
vibe status --target /path/to/repo RUN-20260723-001
vibe status --target /path/to/repo RUN-20260723-001 --json
```

## 停止

```bash
vibe stop --target /path/to/repo RUN-20260723-001
```

停止请求是持久化的，并绑定到已登记的 Controller 进程身份。恢复时会
创建新 Attempt，不会复用被取消的上下文。

## 日志

```bash
vibe logs --target /path/to/repo RUN-20260723-001
vibe logs --target /path/to/repo RUN-20260723-001 --task TASK-001
```

## Schema 3 迁移

Schema 3 不会被透明加载或自动迁移。必须选择一个需求或全部需求，并绑定
到显式 Git base：

```bash
vibe migrate --target /path/to/repo \
  --requirement REQ-001 --base HEAD
vibe migrate --target /path/to/repo --all --base refs/heads/main
```

迁移会在创建任何映射前校验全部选中树，把精确字节和 mode 保存到
`.vibe-coding/schema3-backups/MIG-*/`，并创建不可变 claim。
`ACCEPTED`/`DEGRADED` 映射为 `IMPORTED_READ_ONLY`；
`ACTIVE`/`BLOCKED` 映射为带 `SCHEMA3_REPLAN_REQUIRED` 的 `PAUSED`。
相同 source/base 的重试幂等，源字节或 base 变化会冲突。脏产品字节可以
作为历史上下文归档，但绝不会成为 Schema 4 基线。

## 安全约束

- 只有 Controller 可以修改 run 状态和受保护的 Vibe refs。
- Planner 与 Evaluator 只读运行，并接受前后快照审计。
- Worker 只能修改声明的 path scope；exclusive resource 防止不安全并行。
- Provider 输出在通过严格 JSON、Schema 和身份校验前不可信。
- Attempt、source commit、integration、evaluation、stop 和 migration
  协议在已覆盖的崩溃窗口内都可幂等恢复。
- Goal Gate 要求任务全部完成、验证证据绑定当前 integration head，且独立
  Evaluator 给出 PASS。
- 工具不会自动 merge、push、创建 PR 或发布包。

## 开发

```bash
python -m pip install --no-deps -e .
PYTHONDONTWRITEBYTECODE=1 \
  python -m unittest discover -s tests -p 'test_*.py' -v
python -m compileall -q src/vibe
```

默认 CI 只运行离线测试。真实 Codex CLI smoke 必须通过对应环境变量显式
启用，CI 不会默认联系 Provider。

提交贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，公开使用问题请按
[SUPPORT.md](SUPPORT.md) 选择正确入口，安全漏洞请按
[SECURITY.md](SECURITY.md) 私下报告。

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。
