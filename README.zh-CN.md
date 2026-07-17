# Vibe Coding Harness

[![CI](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/Yubeizuihoudedanchun911/vibe-coding-harness/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

[English](README.md)

Vibe Coding Harness 是一套面向 Codex 的长时开发 Skill。它把规划、实现和独立验收拆分给不同角色，同时在目标 Git 仓库中保存按需求隔离、可跨会话恢复的持久化证据。

## 解决的问题

长时开发任务经常因为目标、实现状态或验收证据只存在于聊天上下文而中断。本 Skill 在保持用户交互简洁的同时，强制执行：

- 每个需求只运行一次、通过快照审计的指令级只读 Planner；
- 只有 Generator 可以修改业务代码；
- Evaluator 使用独立上下文，并通过前后快照审计只读边界；
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
        ├── evaluation-inputs/
        │   ├── plan.md
        │   └── implementation.md
        ├── attempts/
        │   └── NNN.md
        ├── implementation.md
        ├── review.md
        └── interruption.json
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

Codex 会在内部完成角色选择、实现和验收循环。Schema 3 使用显式验收事务：

```bash
python3 scripts/harness.py init \
  --target /path/to/repo \
  --goal "创建并验收一个个人记账 MVP"
python3 scripts/harness.py snapshot --target /path/to/repo
python3 scripts/harness.py begin-evaluation --target /path/to/repo \
  --requirement REQ-001
python3 scripts/harness.py record-review --target /path/to/repo \
  --requirement REQ-001 \
  --review-source /tmp/review.md
# 仅在已评测快照发生漂移时运行：
python3 scripts/harness.py restart-evaluation --target /path/to/repo \
  --requirement REQ-001 --reason "说明观察到的漂移"
python3 scripts/harness.py accept --target /path/to/repo \
  --requirement REQ-001
python3 scripts/harness.py check --final --target /path/to/repo \
  --requirement REQ-001
```

`begin-evaluation` 会把精确目标、计划、实现交接、Git revision 和产品工作区字节冻结为一个事务。它先写入 `pending_evaluation`，再把计划与实现快照归档到 `evaluation-inputs/`，最后返回 Evaluator 必须原样复述的事务身份及哈希。若写入中断，重新运行 `begin-evaluation`：匹配的 prepared 输入会被完成，已变化的当前输入会被安全重备。`init --resume` 会主动报告该标记，不负责协调它。`record-review` 的来源必须是目标仓库 outside（外部）的普通文件。

评审持久化采用两阶段事务：先写入 prepared 状态标记，再修改 `review.md`，因此 `init --resume` 能确定性完成中断写入。替换 PASS 或 UNVERIFIED 评审时，旧评审的精确字节会归档到 `attempts/`；FAIL 在推进轮次前生成哈希绑定的历史收据。`restart-evaluation` 同样先准备并校验 `interruption.json`，把其摘要写入历史，并且只在事务输入或证据产物真实漂移时进入新构建轮次。

Schema 3 是破坏性升级：不支持也不会迁移 Schema 2 状态。评测记录与中断记录各自使用 Schema 2 契约；它们的 Schema 1 形式同样不受支持。

## 安全约束

- Root 只负责编排和证据持久化，不修改业务代码。
- Planner 与 Evaluator 是指令级只读角色，Root 使用前后工作区快照审计边界。
- Generator 是唯一业务代码写入者。
- 所有角色严格串行运行。
- PASS 必须把评审记录 Schema 2 的 typed observations 绑定到需求 ID、轮次、精确目标/计划/实现哈希、提交、完整工作区指纹、验收标准 ID 和 review 字节。自由文本摘要只用于解释；证据必须给出精确值、有限数值指标，或 SHA-256 与当前字节一致的仓库相对产物。
- 允许已有 dirty 文件，但 tracked 原始字节、staged、unstaged、非 ignored 的 untracked 产品内容以及递归检查的 submodule 内容都会进入验收指纹；clean filter 和 `assume-unchanged` 无法掩盖 tracked 变化。
- 快照命令禁用 external diff 与 text-conversion helper。
- 事务输入归档、被替换的评审尝试、失败评测收据和中断收据都会在校验时重新计算哈希；伪造或篡改历史会被拒绝。
- 生命周期写入使用 prepared 状态标记和原子文件替换，使重试只能完成已记录事务或安全失败；孤立的生命周期文件不会被当成已提交事务的权威。
- 评测最多 999 轮；文件系统和 Unicode 错误会以结构化 CLI 错误返回，不输出 traceback。
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
