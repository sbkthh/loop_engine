# AI Native — Loop Engine 完全使用指南

## 概述

AI Native 是一套 spec 驱动的开发编排系统，由三层组成：

| 层 | 名称 | 职责 |
|----|------|------|
| Phase 0 | Setup | 初始化需求、Git worktree、注册 |
| Layer 1 | Spec Session | AI 管理 spec 的 SCORE 往返（Dashboard + 评分 + 精炼） |
| Layer 2 | Scheduler | 定时轮询 + 自动派发（纯 Python，不依赖 AI） |

核心概念：

- **需求 (requirement)**：一个业务目标，如"战略备货系统升级"
- **项目 (project)**：一个 Git 仓库，需求可能涉及多个项目
- **模块 (module)**：一个 spec 文件 + 对应代码，最小编排单元
- **状态机**：SYNCED → PARTIAL → READY → MAKER_STEP0 → ... → SYNCED

---

## 一、从零搭建

### 前置条件

- Python ≥ 3.9
- Node.js (qodercli 依赖)
- Git

### 安装

```bash
# 1. 克隆 loop engine
git clone <repo-url> ~/.qoder/loop_engine
cd ~/.qoder/loop_engine

# 2. 安装（macOS 推荐 shim 方式）
python3 cli.py self-install

# 3. 验证
loop_engine self-check
```

预期输出：

```
Check        Status   Detail
------------------------------------------------------------
CLI          OK       available
Skill        OK       /Users/.../spec-session/SKILL.md
Data dir     OK       /Users/.../.qoder/loop_engine
Registry     OK       0 requirement(s) registered
Tests        OK       98 passed in 3.76s

System ready.
```

---

## 二、注册一个需求

需求根目录是一个**中立目录**，不隶属于任何项目。每个项目通过 Git worktree 挂载到根目录下。

### 简单注册（已有现成目录结构，不创建 worktree）

```bash
loop_engine requirement-add <requirement-name> /path/to/root \
  --description "需求描述，用于语义匹配"
```

### 从 PRD 文档一键初始化（推荐）

一条命令完成：解析 PRD → 创建 worktree → 生成 spec → 注册。

```bash
loop_engine requirement-add <name> <root-path> \
  --prd doc.md \
  --change <change-id> \
  --projects name=path,name=path \
  --modules "模块A,模块B"    # 可选：只生成指定模块，默认从 ## 标题推断
```

`--projects` 的 `name=path` 中：
- `name` — 项目在根目录下的目录名（也是 worktree 名）
- `path` — 源 Git 仓库路径（**不需要 cd 进去**，loop engine 自动创建 worktree）

**单项目示例：**

```bash
# 根目录 ~/loop-work/stockup 自动创建，下面挂载一个 worktree
loop_engine requirement-add strategic-stockup-system-upgrade \
  ~/loop-work/stockup \
  --prd ~/docs/prd-strategic-stockup.md \
  --change ssu-001 \
  --projects backend=~/IdeaProjects/zkh-opc-sna
```

结果目录结构：
```
~/loop-work/stockup/
├── backend/              ← Git worktree (feature/ssu-001), 源 = ~/IdeaProjects/zkh-opc-sna
├── .loop/state.json
└── openspec/changes/ssu-001/specs/
```

**多项目示例：**

```bash
# 一个需求涉及后端 + 前端两个仓库
loop_engine requirement-add cross-dock-system \
  ~/loop-work/cross-dock \
  --prd ~/docs/prd-cross-dock.md \
  --change cd-001 \
  --projects backend=~/repos/backend-service,frontend=~/repos/web-portal
```

结果目录结构：
```
~/loop-work/cross-dock/
├── backend/              ← worktree from ~/repos/backend-service, branch feature/cd-001
├── frontend/             ← worktree from ~/repos/web-portal, branch feature/cd-001
├── .loop/state.json
└── openspec/changes/cd-001/specs/...
```

`--prd` 自动完成：

1. 解析 PRD 中 `##` 标题作为模块（或用 `--modules` 指定）
2. 为每个项目创建 Git worktree + feature branch
3. 生成 `openspec/changes/<change-id>/specs/<module>/spec.md`（含 PRD 内容）
4. 初始化 `.loop/state.json`，所有模块状态为 DRAFT
5. 注册到 registry

**下一步**：在 qodercli 中用 grilling + openspec-propose 技能精炼 spec，然后启动 SCORE 往返。

### 查看所有已注册需求

```bash
loop_engine requirement-list
```

---

## 三、初始化状态（每个需求只需一次）

如果注册时没有使用 `--prd`，需要手动初始化状态：

```bash
loop_engine init --root /path/to/your-project
```

这会在项目根目录创建 `.loop/state.json` 和 `openspec/` 目录。

> 使用 `--prd` 注册时，初始化已自动完成，无需再执行此步骤。

---

## 四、手动运行（"手动试点"模式）

在需求根目录上操作，**不需要 cd 进任何项目目录**。切换不同需求只需换 `--root`：

```bash
# 1. 查看状态
loop_engine status --root ~/loop-work/stockup

# 2. 路由下一步 → directives
loop_engine next --root ~/loop-work/stockup

# 3. 查看 directives 的输出，按指示干活
#    - SCORE：给 spec 打分，写 result.md
#    - MAKER：实现代码
#    - CHECKER：验证一致性
#    - CODE_REVIEW：审查代码
cat ~/loop-work/stockup/.loop/result.md

# 4. 提交结果，状态机推进
loop_engine commit --root ~/loop-work/stockup

# 5. 重复 1-4 直到 SYNCED

# 切换到另一个需求（状态隔离，切换无损）
loop_engine status --root ~/loop-work/cross-dock
```

### SCORE 往返详细流程

`next` 路由到 SCORE 时：

1. `next` → 输出 directives，包含 `spec_path` 和评分标准
2. 打开 spec 文件，对照评分标准打分
3. 将评分结果写入 `.loop/result.md`（格式按 directives 要求）
4. `commit` → 分数 ≥ 90 转 READY，< 90 转 NEEDS_REFINEMENT
5. NEEDS_REFINEMENT：精炼 spec.md，重新打分直到 READY

### 完整链路预览

```
SCORE → CLASSIFY_CHANGE → MAKER_STEP0 → STEP1_RED → STEP2_GREEN
  → CHECKER → MAKER_FIX(可选) → CODE_REVIEW → CODE_REVIEW_FIX(可选) → SYNCED
```

---

## 五、Layer 2 调度器（自动轮询）

调度器自动检测需求状态变化并派发任务。

### 一次轮询

```bash
loop_engine poll
```

检测所有注册需求的状态变化，输出：

```
Poll results:
  strategic-stockup-system-upgrade: SPEC_CHANGED (module: stock-creation)
  strategic-stockup-system-upgrade: READY_PENDING (module: ai-recommend)
  No pending work.
```

### 查看待办

```bash
loop_engine pending
```

### 批准自动执行

```bash
# 批准单个需求
loop_engine approve strategic-stockup-system-upgrade

# 批准所有可执行的待办
loop_engine approve --all
```

### 运行一个需求（从当前状态到完成或阻塞）

```bash
loop_engine run strategic-stockup-system-upgrade
```

### 调度器配置

```bash
# 查看当前配置
loop_engine schedule status

# 设置轮询间隔（分钟）
loop_engine schedule interval 5

# 设置最大并行数
loop_engine schedule max-concurrency 2
```

### 定时自动轮询（crontab）

```bash
# 每 5 分钟轮询一次（shim 路径，不依赖 cd）
crontab -e
*/5 * * * * /Users/chuan.li/.local/bin/loop_engine poll
```

---

## 六、Layer 1 Spec Session（AI 管理会话）

在 qodercli 中唤起 spec-session skill：

```
@spec-session 查看所有需求状态
```

Skill 会自动：

1. 读取所有已注册需求
2. 展示 Dashboard（需求 → 模块 → 状态）
3. 高亮需要关注的项目（NEEDS_REFINEMENT / BLOCKED / DRAFT）
4. 执行 SCORE 往返、精炼 spec、跨模块一致性检查

### 可用命令（在 skill 内，使用绝对路径）

- `loop_engine requirement-list` — 所有注册需求
- `loop_engine status --root ~/loop-work/stockup` — 模块状态
- `loop_engine next --root ~/loop-work/stockup` / `commit` — SCORE 往返
- `loop_engine poll` / `pending` / `approve` — 调度器可见性

---

## 七、管理命令

```bash
# 重命名需求
loop_engine requirement-rename old-name new-name

# 删除需求
loop_engine requirement-remove <name>

# 重置模块到 DRAFT
loop_engine reset --root <path> <change_id/module_name>

# 手动设置模块状态
loop_engine set-status --root <path> <module> <status>

# 添加阻塞项
loop_engine add-blocker --root <path> <module> <description>

# 解决 DRAFT 决议
loop_engine resolve-draft --root <path> <id> accept|reject
```

---

## 八、完整示例：从零到 SYNCED

### 方式一：从 PRD 文档开始（推荐，单项目）

```bash
# 1. 一条命令：注册 + worktree + spec 初始化
#    根目录 ~/loop-work/stockup 自动创建，不依赖 cd
loop_engine requirement-add strategic-stockup-system-upgrade \
  ~/loop-work/stockup \
  --prd ~/docs/prd-strategic-stockup.md \
  --change ssu-001 \
  --projects backend=~/IdeaProjects/zkh-opc-sna

# 2. 在 qodercli 中用 AI 技能精炼 spec
#    @grilling → @openspec-propose → SCORE 往返

# 3. 手动运行第一轮 SCORE
loop_engine status --root ~/loop-work/stockup
loop_engine next --root ~/loop-work/stockup
# → 编辑 .loop/result.md（评分）
loop_engine commit --root ~/loop-work/stockup

# 4. 重复直到 SYNCED
loop_engine status --root ~/loop-work/stockup

# 5. 或者让调度器自动跑
loop_engine poll
loop_engine approve --all
loop_engine run strategic-stockup-system-upgrade
```

### 方式二：从 PRD 文档开始（多项目）

```bash
# 1. 一条命令：两个仓库同时创建 worktree
loop_engine requirement-add cross-dock-system \
  ~/loop-work/cross-dock \
  --prd ~/docs/prd-cross-dock.md \
  --change cd-001 \
  --projects backend=~/repos/backend-service,frontend=~/repos/web-portal

# 结果结构：
#   ~/loop-work/cross-dock/
#     backend/    ← worktree, feature/cd-001
#     frontend/   ← worktree, feature/cd-001
#     .loop/state.json
#     openspec/...

# 2-5. 后续步骤同方式一（status → next → commit → SYNCED）
```

### 方式三：手动注册 + 初始化

```bash
# 1. 注册需求
loop_engine requirement-add strategic-stockup-system-upgrade \
  ~/loop-work/stockup \
  --description "战略备货系统升级"

# 2. 初始化
loop_engine init --root ~/loop-work/stockup

# 3. 手动创建 spec 文件到 openspec/changes/.../specs/ 下
# 4. 手动创建 worktree: git worktree add -b feature/ssu-001 ...

# 5. 手动试点：第一轮 SCORE
loop_engine status --root ~/loop-work/stockup
loop_engine next --root ~/loop-work/stockup
# → 编辑 .loop/result.md（评分）
loop_engine commit --root ~/loop-work/stockup

# 6. 重复直到 SYNCED
loop_engine status --root ~/loop-work/stockup

# 7. 或者让调度器自动跑
loop_engine poll
loop_engine approve --all
loop_engine run strategic-stockup-system-upgrade
```

---

## 九、架构说明

```
~/.qoder/loop_engine/           # 代码目录
├── cli.py                      # CLI 入口 + 命令处理
├── machine.py                  # 状态机路由
├── state.py                    # StateManager
├── directives.py               # 指令生成
├── parser.py                   # result.md 解析
├── report.py                   # 报告生成
├── spec_utils.py               # spec 工具函数 + PRD 解析
├── scheduler.py                # Layer 2 调度器
├── setup.py                    # Phase 0 初始化
├── registry.py                 # 需求注册表
├── constants.py                # 常量
├── __main__.py                 # Python -m 入口
├── pyproject.toml              # 构建配置
└── tests/
    ├── test_machine.py
    ├── test_state.py
    ├── test_parser.py
    ├── test_directives.py
    ├── test_spec_utils.py
    ├── test_setup.py
    └── test_scheduler.py       # 调度器 29 个测试

~/.qoder/loop_engine/           # 数据目录
├── requirements.json           # 需求注册表
└── schedule.json               # 调度器配置

~/.qoder/skills/spec-session/   # Layer 1 Skill
└── SKILL.md

~/.local/bin/loop_engine        # 命令行 shim
```

### 关键设计决策

- **调度器不 import 引擎核心模块**：只通过文件（`.loop/state.json`）和 CLI 子进程通信
- **安全阀**：同 action 重复 3 次自动中断、200 步上限、`.loop/lock` PID 文件锁
- **状态隔离**：每个需求独立 `.loop/state.json`，切换 `--root` 无损
- **macOS 兼容**：pip 安装因 externally-managed-environment 被屏蔽，改用 shim 方式