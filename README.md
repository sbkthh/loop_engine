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

### 平台支持

- **macOS / Linux**：原生支持
- **Windows**：未验证。核心逻辑是纯 Python，但 audit hook 与命令行 shim 是 shell 脚本、定时轮询依赖 crontab，均属 Unix 机制——需在 WSL 或 Git Bash 下运行，并用任务计划程序替代 crontab。

### 安装

```bash
# 1. 克隆代码到 ~/loop_engine（位置可自选，self-install 会自适应）
git clone <repo-url> ~/loop_engine
cd ~/loop_engine

# 2. 安装：生成 shim（~/.local/bin/loop_engine，指向当前代码目录）
#    + spec-session skill + 数据目录（~/.qoder/loop_engine）
python3 __main__.py self-install

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
Tests        OK       176 passed in 6.57s

System ready.
```

### 定时轮询（crontab，每台机器手动配置）

```bash
crontab -e
*/10 * * * * ~/.local/bin/loop_engine poll
0 3 * * 1 ~/.local/bin/loop_engine session-clean
```

### WeCom Bot（可选）

需要微信交互时再配置，见「十、WeCom 企业微信 Bot」。

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

### 环境上下文（数据库 / Nacos / 网关）

需求涉及的运行时环境（数据库 UAT/PROD、Nacos namespace/dataID、API 网关等）存放在 `.loop/context.json`（机器本地，不入库）。敏感值只写环境变量引用（`password_env`），不存明文：

```json
{
  "databases": {
    "uat": {"host": "10.x.x.x", "port": 3306, "name": "wms_uat", "password_env": "DB_UAT_PASSWORD"}
  },
  "nacos": {"namespace": "cross-dock-v2", "data_ids": ["wms-inbound.yml"]}
}
```

**写入**：注册时用 `--context <json文件>`（`setup` / `requirement-add --prd` 均支持）；已注册的需求用：

```bash
loop_engine context --root <req_root> set --file /path/context.json
loop_engine context --root <req_root> show
```

**消费**：`loop_engine next` 会把 context 合并进 directives 的 `context.environment`，MAKER/Checker 据此配置 dgate CLI、Nacos MCP 等访问；loop engine 自身不连接任何服务，只透传。

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
  → CHECKER ─→ 不一致 → GRAY_LIST（微信裁决）
  │               ├─ 全部接受 → MAKER_FIX → CHECKER
  │               ├─ 全部拒绝 → ALIGN_DOCS → CHECKER
  │               └─ 混合     → MAKER_FIX → ALIGN_DOCS → CHECKER
  → CHECKER ─→ 一致 → CODE_REVIEW → CODE_REVIEW_FIX(可选) → SYNCED
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

# 设置最大并行数
loop_engine schedule max-concurrency 2
```

> 轮询间隔由 crontab 唯一决定（见下），schedule.json 不存间隔配置，
> 避免两套配置漂移。

### 定时自动轮询（crontab）

```bash
# 每 10 分钟轮询一次 + 每周一 3 点清理旧 qodercli 会话（shim 路径，不依赖 cd）
crontab -e
*/10 * * * * ~/.local/bin/loop_engine poll
0 3 * * 1 ~/.local/bin/loop_engine session-clean
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

## 十、WeCom 企业微信 Bot

WeCom bot 允许通过企业微信发送消息与 loop engine 交互。

### 架构

```
用户发消息 → WeCom → POST /callback → 服务器解密 → 返回"success"
  → 后台 qodercli 处理 → API 推送结果
```

所有消息走异步 LLM 路径：服务器立即返回 `"success"`（WeCom 不重试），后台调 qodercli 处理，完成后通过 API 推送结果。没有关键词匹配，LLM 处理全部意图。

### 可用命令

- `查状态` — 查看所有需求状态
- `批准执行` — 批准待执行的需求
- 其他自然语言问题 — LLM 自动理解并回答

### 配置

WeCom 配置存储在 `~/.qoder/loop_engine/wecom.json`：

| 字段 | 说明 |
|------|------|
| `corp_id` | 企业 ID |
| `agent_id` | 应用 Agent ID |
| `secret` | 应用 Secret |
| `token` | 回调 Token |
| `encoding_aes_key` | 回调加密密钥 |

### 启动

```bash
# 启动服务器（默认端口 5000）
loop_engine wecom start --port 5000

# 查看状态
loop_engine wecom status

# 停止服务器
loop_engine wecom stop

# 配置
loop_engine wecom config --show
loop_engine wecom config --set key=value
```

### 隧道

企业微信回调 URL 需要公网可达。使用 autossh 建立反向隧道：

```bash
autossh -M 0 -N -o ServerAliveInterval=30 \
  -R 0.0.0.0:5000:localhost:5000 root@<server-ip>
```

### 异步推送

慢操作（如查询复杂状态、执行 LLM 推理）走异步路径：
1. 服务器立即返回 `"success"`（WeCom 不重试）
2. 后台线程调 qodercli 处理
3. 完成后通过 WeCom API 主动推送结果

异步推送需要将服务器公网 IP 加入企业微信应用管理的"企业可信IP"列表。

### 模型

后台 qodercli 子进程自动使用 `~/.qoder/settings.json` 中配置的默认模型，可通过 `/model` 命令切换。

---

## 十一、架构说明

### 进程调用关系

```
┌────────────────────────────────────────────────────────────────────┐
│  crontab  (*/10 * * * * loop_engine poll)  ← 周期轮询触发器         │
│  → scheduler.poll() = 读 state.json → 合并 pending.json → 微信通知  │
│  → dispatch 兜底（仅捡起已 approved 但未启动的）                    │
└──────────────────────────┬─────────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────────────┐
│  A: loop_engine CLI 主进程（argparse 分发）                         │
│     cmd_poll = scheduler.poll() + dispatch(已 approved 项) 兜底     │
│     poll / pending / approve / run / schedule / session-clean /    │
│     manual-begin|end / wecom ...                                   │
│     approve 不含 dispatch → dispatch 由各触发者各自负责              │
└──────┬──────────────────────────────────────┬──────────────────────┘
       │ dispatch 兜底（通常已被 G 抢先）       │ manual-begin/end
       ▼（仅捡 approved 但未被 G 启动的）      ▼（手动循环接管锁）
┌────────────────────────────────────────────────────────────────────┐
│  B: run_requirement 子进程（G 或 A fork，start_new_session）        │
│     循环: next → qodercli → commit → next → ... → IDLE             │
│     持有 .loop/lock 锁；心跳推送；重试 1 次；步数/重复上限           │
└──────┬──────────────────────────────────────┬──────────────────────┘
       │ 每步 fork 一次性子进程                 │
       ▼                                       │
┌──────────────────────────────────────────────┐
│  C: next 子进程    读 state.json → 输出 directives
│  D: qodercli 子进程  --print --session-id <uuid5(root:action:retries)>
│                     --no-session-persistence --cwd <root>
│                     --append-system-prompt LOOP_AGENT_PROMPT
│                     输入 = directives + context.previous_result
│                     输出 → .loop/result.md（无会话记忆，外部记忆）
│  E: commit 子进程  解析 result.md → 状态机推进 → 清空 result.md
└──────────────────────────────────────────────

┌────────────────────────────────────────────────────────────────────┐
│  F: wecom_server 守护进程（端口 5000，A fork 常驻）                  │
│     微信回调 → 立即返回 "success" → 后台 LLM 处理 → API 推送结果     │
│     识别前缀路由: __APPROVE__ / __HISTORY__ / __GRAY_LIST__ /       │
│     __ADJUDICATE__ / __SPEC_RESULT__ → 进程内执行对应 handler       │
└──────┬─────────────────────────────────────────────────────────────┘
       │ 每个消息派生一个 G
       ▼
┌────────────────────────────────────────────────────────────────────┐
│  G: qodercli 子进程（每个微信消息一次）                              │
│     --session-id/--resume <按用户+需求稳定的会话>（对话记忆）        │
│     --settings <audit hook>（敏感 Bash 命令审计，只挂在 G 上）       │
│                                                                     │
│     前缀路由（F 根据 LLM 回复的第一行识别，不经过二次 LLM）：          │
│     __APPROVE__ <name>         → approve + dispatch → fork B       │
│     __HISTORY__ <name|ALL>     → 读取执行历史                       │
│     __GRAY_LIST__ <name|ALL>   → 列出待裁决灰名单草稿                │
│     __ADJUDICATE__ <name> <ids|all> <accept|reject>                │
│                                → 裁决草稿；全部裁决完毕自动          │
│                                  approve + dispatch → fork B        │
│     __SPEC_RESULT__ <name> <key> → 校验/备份/置 PARTIAL → 等批准    │
└────────────────────────────────────────────────────────────────────┘
```

| 进程 | 身份 | 触发者 | 关键特征 |
|------|------|--------|----------|
| A | loop_engine CLI | crontab / 手动 / F | 命令分发；manual-begin/end 锁；scheduler.poll() 只检测不启动 |
| B | run_requirement | G (微信，即时) / A (poll 兜底) | 循环驱动；锁 + 心跳 + 重试；并发上限 max_concurrency |
| C/D/E | 每步一次性 | B | C 路由、D 干活、E 推进；D 无会话记忆，靠 previous_result 传续 |
| F | wecom_server | A (wecom start) | 常驻 :5000；LLM 分类 → 前缀路由 → handler 进程内执行 |
| G | qodercli | F（每消息） | 按用户+需求共用会话；audit hook 审计；5 种前缀触发不同 handler |

```
~/loop_engine/                  # 代码目录（git 主仓库，开发在此进行）
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
├── README.md                   # 使用指南
├── wecom_server/               # WeCom 机器人（F/G）
└── tests/
    ├── test_machine.py
    ├── test_state.py
    ├── test_parser.py
    ├── test_directives.py
    ├── test_spec_utils.py
    ├── test_setup.py
    ├── test_scheduler.py       # 调度器 29 个测试
    └── test_session_clean.py

~/.qoder/loop_engine/           # 数据目录（仅数据，无代码）
├── requirements.json           # 需求注册表
├── pending.json                # poll 待执行清单
├── schedule.json               # 调度器配置
├── wecom.json                  # WeCom 应用配置（密钥）
├── audit.log                   # 敏感命令审计日志
├── sessions/                   # 微信用户会话状态
└── .loop/                      # 本地循环状态

~/.qoder/skills/spec-session/   # Layer 1 Skill
└── SKILL.md

~/.local/bin/loop_engine        # 命令行 shim
```

### 关键设计决策

- **调度器不 import 引擎核心模块**：只通过文件（`.loop/state.json`）和 CLI 子进程通信
- **安全阀**：同 action 重复 3 次自动中断、200 步上限、`.loop/lock` PID 文件锁
- **状态隔离**：每个需求独立 `.loop/state.json`，切换 `--root` 无损
- **macOS 兼容**：pip 安装因 externally-managed-environment 被屏蔽，改用 shim 方式