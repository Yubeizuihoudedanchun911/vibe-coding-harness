# Vibe Coding Harness

[![CI](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

[English](README.md)

Vibe Coding Harness 是一套面向 Codex 的长时开发 Skill。它把规划、实现和独立验收拆分给不同角色，同时在目标 Git 仓库中保存按需求隔离、可跨会话恢复的持久化证据。

## 解决的问题

长时开发任务经常因为目标、实现状态或验收证据只存在于聊天上下文而中断。本 Skill 在保持用户交互简洁的同时，强制执行：

- 每个需求只运行一次只读 Planner；
- 只有 Generator 可以修改业务代码；
- Evaluator 使用独立上下文进行只读验收；
- 角色串行执行，并通过有界修复循环处理失败；
- 验收证据绑定到具体 Git 提交；
- 上下文重置或跨会话后仍可从文件恢复。

## 工作方式

```text
用户目标
   |
   v
Root 编排器
   |
   +--> Planner（只读，每个需求一次）
   |
   +--> Generator（唯一业务代码写入者）
   |          ^
   |          |
   +--> Evaluator（只读）-- FAIL --> 修复轮次
                    |
                    +-- PASS --> Goal Gate --> ACCEPTED
```

每个目标拥有独立的持久化记录：

```text
.vibe-coding/requirements/REQ-NNN/
├── state.json
├── plan.md
└── rounds/
    └── NNN/
        ├── implementation.md
        └── review.md
```

仓库文件是恢复契约；Agent 消息只承担实时调度，不是长期事实来源。

## 环境要求

- 支持 `spawn_agent`、`followup_task`、`wait_agent` 的 Codex
- Git
- Python 3.10 或更高版本

如果运行环境不支持多 Agent，Skill 会记录 `BLOCKED`，不会退化为 Root 直接编写业务代码。

## 安装

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone --branch master --single-branch \
  https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/vibe-coding-harness"
```

更新已有安装：

```bash
git -C "${CODEX_HOME:-$HOME/.codex}/skills/vibe-coding-harness" pull --ff-only
```

## 使用

在 Codex 中直接提出用户目标：

```text
使用 $vibe-coding-harness 创建并验收一个个人记账 MVP。
```

Codex 会在内部完成角色选择、实现和验收循环。`scripts/harness.py` 主要用于确定性的状态初始化、恢复与校验：

```bash
python scripts/harness.py init \
  --target /path/to/git/repository \
  --goal "创建并验收一个个人记账 MVP"

python scripts/harness.py check \
  --target /path/to/git/repository \
  --requirement REQ-001
```

## 安全约束

- Root 只负责编排和证据持久化，不修改业务代码。
- Planner 与 Evaluator 保持只读。
- Generator 是唯一业务代码写入者。
- 所有角色严格串行运行。
- PASS 必须包含针对精确 Git 提交的实质证据。
- 历史需求与验收轮次不得覆盖或删除。
- 不自动暂存用户的无关改动。

完整协议见 [SKILL.md](SKILL.md)。

## 开发与贡献

运行完整测试：

```bash
PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -p 'test_*.py' -v
```

提交贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全漏洞请按照 [SECURITY.md](SECURITY.md) 私下报告。社区行为受 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) 约束。

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。
