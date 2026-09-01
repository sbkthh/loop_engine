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
- **状态机**：DRAFT / PARTIAL / READY / NEEDS_REFINEMENT / BLOCKED / SYNCED，动作链 SCORE → MAKER_STEP0 → STEP1_RED → STEP2_GREEN → CHECKER → CODE_REVIEW → SYNCED

---

## 一、从零搭建

### 前置条件

- Python ≥ 3.9
- Node.js (qodercli 依赖)
- Git

### 平台支持

- **macOS / Linux**：原生支持（锁机制依赖 `fcntl.flock`，Unix-only）
- **Windows**：未验证。核心逻辑是纯 Python，但 flock 锁、audit hook 与命令行 shim 是 shell/Unix 机制、定时轮询依赖 crontab——需在 WSL 或 Git Bash 下运行，并用任务计划程序替代 crontab。

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
Check               Status   Detail
------------------------------------------------------------
CLI                 OK       available
Skill-spec-session  OK       /Users/.../spec-session/SKILL.md
Skill-prd-to-spec   OK       /Users/.../prd-to-spec/SKILL.md
Skill-grill-me      OK       /Users/.../grill-me/SKILL.md
Skill-requirement-register OK  /Users/.../requirement-register/SKILL.md
Skill-manual-loop   DEPRECATED  /Users/.../manual-loop/SKILL.md（改为引导走 approve）
Data dir            OK       /Users/.../.qoder/loop_engine
Registry            OK       0 requirement(s) registered
Tests               OK       326 passed in 19.24s

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

一条命令完成：解析 PRD → 创建 worktree → 写 PRD 摘要 → 注册。

```bash
loop_engine requirement-add <name> <root-path> \
  --prd doc.md \
  --change <change-id> \
  --projects name=path,name=path \
  --modules "模块A,模块B"    # 可选：只注册指定模块，默认从 ## 标题推断
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
├── .loop/
│   ├── state.json        ← 模块状态
│   └── prd_summary.json  ← PRD 摘要（/prd-to-spec 的输入）
└── openspec/             ← /prd-to-spec 运行后生成 artifacts
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
├── .loop/
│   ├── state.json
│   └── prd_summary.json  ← PRD 摘要（/prd-to-spec 的输入）
└── openspec/             ← /prd-to-spec 运行后生成 artifacts
```

`--prd` 自动完成：

1. 解析 PRD 中 `##` 标题作为模块（或用 `--modules` 指定）
2. 为每个项目创建 Git worktree + feature branch
3. 初始化 `.loop/state.json`，所有模块状态为 DRAFT
4. 写入 `.loop/prd_summary.json`（PRD 摘要：模块名 + 各节内容 + 项目信息）
5. 注册到 registry

> 注意：`--prd` 不再直接生成 spec 文件。spec 由 qodercli 侧的 `/prd-to-spec` skill 根据 PRD 摘要生成（见下）。

**下一步（两条路径，任选其一）：**

- **终端**：在 qodercli 中运行 `/prd-to-spec`，它会读取 PRD 摘要并驱动 openspec 生成 artifacts（proposal → design → specs → tasks）；然后唤起 `@spec-session`，它将展示 Dashboard、对 DRAFT 模块自动调用 grill-me 澄清需求，然后驱动 SCORE 往返直至 READY。
- **微信（推荐，全程微信闭环）**：注册本身也可以在微信里完成——直接发 PRD 路径 + 需求名/根目录/change-id/项目仓库（缺的参数 G 会逐个问），G 自动执行 `requirement-add --prd` 并汇报结果。之后说"按 PRD 生成 spec"生成 artifacts，G 自动进入 grill-me 澄清，定稿后 G 以 `__JSON_ACTION__` 动作块登记 spec，微信"批准执行"即进入调度器。全程无需打开终端/qodercli。

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
SPEC_CHANGED → CLASSIFY_CHANGE ─→ 轻量 → CHECKER
                                └→ 重量 → SCORE ─→ MAKER_STEP0

READY → SCORE → MAKER_STEP0 → MAKER_STEP1_RED → MAKER_STEP2_GREEN → CHECKER

MAKER_STEP1_RED ← 编辑 plan 后 plan_hash 变更（PLAN_CHANGED，重跑实现）

CHECKER ─→ 硬错误 → MAKER_FIX ─→ CHECKER（最多 3 次，用尽转 BLOCKED）
        │
        ├→ 软警告 → GRAY_LIST（微信裁决，带 spec↔代码证据对照）
        │             ├─ 全部接受 → MAKER_FIX → CHECKER
        │             ├─ 全部拒绝 → ALIGN_DOCS → CHECKER（拒绝项不再重复检出）
        │             └─ 混合     → MAKER_FIX + ALIGN_DOCS → CHECKER
        │
        └→ 一致 → CODE_REVIEW → CODE_REVIEW_FIX(可选, 1 次) → SYNCED

BLOCKED（hard_errors 用尽 / 手动标记）：需人工介入，改 spec 或 reset 后恢复
```

> **ALIGN_DOCS 文档分工**：spec.md 只承载**契约**（字段表、API 签名、状态机、业务规则、Scenarios）；**实现细节**（事务边界、IO 位置、算法参数、技术决策）写入 `openspec/changes/<change_id>/design.md`；plan.md 更新任务描述、文件归属、数据流。Checker 拒绝的每条警告按归属写入对应文档——契约分歧改 spec.md，实现层分歧改 design.md。

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

# 设置最大并行数（跨需求并行上限；同一需求内部始终串行）
loop_engine schedule max-concurrency 2
```

> 轮询间隔由 crontab 唯一决定（见下），schedule.json 不存间隔配置，
> 避免两套配置漂移。
>
> **并发模型**：同需求内部按顺序串行执行（避免同一需求的多个 run 交错写
> state.json），不同需求可并行，总并行数受 max_concurrency 上限约束。
> 每个需求由 `.loop/lock`（fcntl.flock 内核独占锁）跨进程互斥，进程死亡
> 锁自动释放，无需人工清理。

### 定时自动轮询（crontab）

```bash
# 每 10 分钟轮询一次 + 每周一 3 点清理旧 qodercli 会话（shim 路径，不依赖 cd）
crontab -e
*/10 * * * * ~/.local/bin/loop_engine poll
0 3 * * 1 ~/.local/bin/loop_engine session-clean
```

---

## 六、Layer 1 Spec Session（AI 管理会话）

### 终端路径（qodercli）

在 qodercli 中唤起 spec-session skill：

```
@spec-session 查看所有需求状态
```

> 从 PRD 注册的新需求（`requirement-add --prd`）没有 spec 文件，先运行 `/prd-to-spec` 生成 OpenSpec artifacts，再进入本会话。

Skill 会自动：

1. 读取所有已注册需求
2. 展示 Dashboard（需求 → 模块 → 状态）
3. 高亮需要关注的项目（NEEDS_REFINEMENT / BLOCKED / DRAFT）
4. 对 DRAFT 模块自动调用 grill-me 逐个追问澄清需求，确认后编辑 spec.md
5. 执行 SCORE 往返（next → 打分 → commit）、跨模块一致性检查

### 微信路径（推荐，spec 全程在微信完成）

微信侧 G（qodercli 子进程）已内置同样的 spec 管理规则，无需手动唤起 skill：

1. **注册**：发 PRD 路径 + 需求参数（缺啥 G 问啥）→ G 执行 `requirement-add --prd`
2. **生成 artifacts**：说"按 PRD 生成 spec"→ G 按 prd-to-spec 流程生成 proposal/design/specs/tasks
3. **澄清**：G 自动进入 grill-me 逐个追问，确认后编辑 spec.md
4. **登记**：编辑完 G 在回复末尾追加 `__JSON_ACTION__` 动作块（spec_result）→ 服务器校验/备份/置 PARTIAL
5. **批准**：微信"批准执行"→ 调度器自动跑 SCORE → MAKER → SYNCED
6. **后续修改**：微信里直接说改需求 → 同样走澄清 → 编辑 → 登记 → 批准

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

# 审计未申报改动：比对模块声明的 files_created/files_modified 与实际 git 工作区改动
loop_engine scope-audit --root <path>

# 手动接管锁（调试用，已废弃——优先用 approve 走调度器）
loop_engine manual-begin --root <path>

# 手动结束循环（调试用，已废弃）
loop_engine manual-end --root <path>
```

> scope-audit 是**只读审计**：发现未申报改动时打印完整报告并自动推微信通知；工作区干净时静默无输出。检测基线是 git 未提交改动（含未跟踪文件），不含已提交历史。

---

## 八、完整示例：从零到 SYNCED

### 方式一：从 PRD 文档开始（推荐，单项目）

```bash
# 1. 一条命令：注册 + worktree + PRD 摘要
#    根目录 ~/loop-work/stockup 自动创建，不依赖 cd
loop_engine requirement-add strategic-stockup-system-upgrade \
  ~/loop-work/stockup \
  --prd ~/docs/prd-strategic-stockup.md \
  --change ssu-001 \
  --projects backend=~/IdeaProjects/zkh-opc-sna

# 2. 在 qodercli 中运行 /prd-to-spec，从 PRD 摘要生成 OpenSpec artifacts
#    （proposal / design / specs / tasks）

# 3. 用 @spec-session 管理 spec：grill-me 澄清 → SCORE 往返 → READY

# 4. 手动运行第一轮 SCORE
loop_engine status --root ~/loop-work/stockup
loop_engine next --root ~/loop-work/stockup
# → 编辑 .loop/result.md（评分）
loop_engine commit --root ~/loop-work/stockup

# 5. 重复直到 SYNCED
loop_engine status --root ~/loop-work/stockup

# 6. 或者让调度器自动跑
loop_engine poll
loop_engine approve --all
loop_engine run strategic-stockup-system-upgrade
```

> **微信替代**：第 1-5 步全部可在微信完成——发 PRD 路径 + 参数注册，说"按 PRD 生成 spec"，G 澄清后编辑 spec 并以 `__JSON_ACTION__` 动作块登记，回复"批准执行"即进入第 6 步。参见「六、Layer 1 Spec Session — 微信路径」。

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
#     .loop/state.json + prd_summary.json
#     openspec/...

# 2-6. 后续步骤同方式一（/prd-to-spec → @spec-session → status → next → commit → SYNCED）
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

所有消息走异步 LLM 路径：服务器立即返回 `"success"`（WeCom 不重试），后台调 qodercli 处理，完成后通过 API 推送结果。没有关键词匹配，LLM 处理全部意图；LLM 判定需要后端动作时，在回复末尾追加 `__JSON_ACTION__ {"action": ..., ...}` 动作块，服务器识别后进程内执行对应 handler（不经过二次 LLM）。

### 可用命令

- `查状态` / `总览` / `所有需求状态` — 查看所有需求状态。跨需求全局问题统一走 global 会话，回复前缀为 【通用】；消息带需求名/模块名时路由到对应需求会话（前缀【需求名】），无关键词的短应答自动回到最近会话
- `批准执行` — 批准待执行的需求（有未裁决灰名单草稿时会被拒绝）
- `注册需求`（发 PRD 路径 + 需求名/根目录/change-id/项目仓库）— 自动执行 `requirement-add --prd`
- `给 XX 加项目`（如"给越库二期加项目 kunhe-wms"，附源仓库路径）— 自动执行 `requirement-add-project`：创建 worktree 并注册到需求，分支缺省沿用需求内第一个项目的分支
- `按 PRD 生成 spec` — 生成 proposal/design/specs/tasks，然后自动进入 grill-me 澄清；新模块注册后 G 会询问归属项目并执行 `set-project-root` 绑定工作目录
- 改需求 / 澄清 spec — 自动走 spec-session + grill-me 流程，编辑后等待批准执行
- `查看执行历史` — 各需求的运行历史（runs.json）
- `查看灰名单` / `接受 1 拒绝 2` — 列出待裁决草稿 / 混合裁决（支持 `全部接受`、`全部拒绝`）
- 其他自然语言问题 — LLM 自动理解并回答

动作块支持的动作：`approve`（批准+派发）、`spec_result`（登记 spec 变更，需 requirement+module）、`history`（执行历史）、`gray_list`（列草稿）、`adjudicate`（裁决，需 requirement+target+decision）。

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

# 重启服务器（代码改动后）
loop_engine wecom stop && loop_engine wecom start --port 5000

# 配置
loop_engine wecom config --show
loop_engine wecom config --set key=value
```

### 隧道

企业微信回调 URL 需要公网可达。使用 autossh 建立反向隧道：

```bash
# 启动隧道（服务器 IP 见 ~/.qoder/loop_engine/wecom.json 或历史记录）
autossh -M 0 -N -o ServerAliveInterval=30 \
  -R 5000:localhost:5000 root@<server-ip>

# 查看隧道状态
pgrep -fl autossh

# 停止隧道
pkill -f "autossh.*5000:localhost:5000"

# 重启隧道
pkill -f "autossh.*5000:localhost:5000"
autossh -M 0 -N -o ServerAliveInterval=30 \
  -R 5000:localhost:5000 root@<server-ip>
```

隧道断线后 autossh 会自动重连；只有服务器 IP 或 SSH 密钥变更时才需手动重启。

### 异步推送

慢操作（如查询复杂状态、执行 LLM 推理）走异步路径：
1. 服务器立即返回 `"success"`（WeCom 不重试）
2. 后台线程调 qodercli 处理
3. 完成后通过 WeCom API 主动推送结果

异步推送需要将服务器公网 IP 加入企业微信应用管理的"企业可信IP"列表。

### 模型

后台 qodercli 子进程自动使用 `~/.qoder/settings.json` 中配置的默认模型，可通过 `/model` 命令切换。

---

## 十点五、飞书接入

`feishu_server/` 与 `wecom_server/` 平行，复用同一套核心流程（`wecom_server.router.dispatch`：LLM 分类 → `__JSON_ACTION__` 动作块 → 后端执行）。传输层使用官方 SDK 的 **WebSocket 长连接**：无需公网地址、反向隧道、安全组放行，也不需要签名校验/challenge 握手。

### 飞书开放平台配置

1. [飞书开放平台](https://open.feishu.cn) 创建企业自建应用，开启**机器人**能力
2. 记录 `App ID` / `App Secret`
3. 「权限管理」开通：`im:message`（收消息）、`im:message:send_as_bot`（发消息）
4. 「事件与回调」→ 订阅方式选择「**使用长连接接收事件**」
5. 「事件与回调」→ 添加事件：`接收消息 im.message.receive_v1`
6. 「版本管理与发布」→ 创建版本并发布（权限发版后生效）

### 本地配置与启动

```bash
# 配置
loop_engine feishu config

# 启动 / 状态 / 停止
nohup loop_engine feishu start >> ~/.qoder/loop_engine/feishu.log 2>&1 &
loop_engine feishu status
loop_engine feishu stop
```

`~/.qoder/loop_engine/feishu.json` 字段：

| 字段 | 说明 |
|------|------|
| `app_id` | 应用 App ID（必填） |
| `app_secret` | 应用 App Secret（必填） |
| `encrypt_key` | 长连接模式下不使用（webhook 模式遗留，可忽略） |
| `verification_token` | 长连接模式下不使用（可忽略） |
| `receipt_enabled` | 收到消息即推「已收到，正在处理…」回执（缺省开；置 `false` 关闭） |

依赖：`lark-oapi`（官方 SDK，`pip install lark-oapi`）。企业代理网络下默认 OpenSSL 路径不含劫持根证书时，`start()` 会自动把 `SSL_CERT_FILE` 指向 certifi 证书包。

### 与企微的差异

- 长连接纯出站连接，进程停止即收不到事件（SDK 自动重连）
- 消息处理全程在后台线程，结果通过 IM API 主动推送
- 事件按 `event_id` 去重（SDK 重连后可能重投）
- 推送分通道：含 markdown（`**`/链接/`<font>`）走交互卡片渲染（`<font>` 压平，其余保留），纯文本仍走 text 消息
- 支持接收**文件消息**与**富文本（post）消息**：文件下载到 `~/.qoder/loop_engine/files/`（文件名按 UTF-8 还原），post 中的文字说明与附件路径合并注入 prompt——发文件时附一句说明即可告知用途（企微的 media/get 链路无法稳定收文件）
- 调度器通知（`scheduler.notify_text`）按 `last_user.json` 中的 `platform` 字段路由到最近活跃用户所在平台（缺省 `wecom`，向后兼容）

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
│     manual-begin|end (deprecated) / wecom ...                     │
│     approve 不含 dispatch → dispatch 由各触发者各自负责              │
└──────┬──────────────────────────────────────┬──────────────────────┘
       │ dispatch 兜底                         │ manual-begin/end（废弃）
       ▼（仅捡 approved 项）                   ▼（不再使用）
┌────────────────────────────────────────────────────────────────────┐
│  B: run_requirement 子进程（G 或 A fork，start_new_session）        │
│     循环: next → qodercli → commit → next → ... → IDLE             │
│     持有 .loop/lock（fcntl.flock 独占锁）；步骤/心跳双超时：         │
│     LLM 步骤 6h 上限（STEP_TIMEOUT）、本地命令 30s（QUICK_TIMEOUT）、 │
│     锁心跳 60s 刷新；重试 1 次；步数/重复上限；同需求串行            │
└──────┬──────────────────────────────────────┬──────────────────────┘
       │ 每步 fork 一次性子进程                 │
       ▼                                       │
┌──────────────────────────────────────────────┐
│  C: next 子进程    读 state.json → 输出 directives
│  D: qodercli 子进程  --print (--resume|--session-id)
│                     <uuid5(root:module_key) 或 :retryN 后缀>
│                     --strict-mcp-config --mcp-config minimal_mcp.json
│                     --cwd <root> --append-system-prompt LOOP_AGENT_PROMPT
│                     输入 = directives + context.previous_result
│                     输出 → .loop/result.md
│                     同模块 run 内跨步复用 session 历史（spec Read 等
│                     一次性成本 amortize）；重试换新 sid 保持隔离
│  E: commit 子进程  解析 result.md → 状态机推进 → 清空 result.md
└──────────────────────────────────────────────

┌────────────────────────────────────────────────────────────────────┐
│  F: wecom_server 守护进程（端口 5000，A fork 常驻）                  │
│     微信回调 → 立即返回 "success" → 后台 LLM 处理 → API 推送结果     │
│     LLM 回复含 __JSON_ACTION__ 动作块 → 进程内执行对应 handler      │
└──────┬─────────────────────────────────────────────────────────────┘
       │ 每个消息派生一个 G
       ▼
┌────────────────────────────────────────────────────────────────────┐
│  G: qodercli 子进程（每个微信消息一次，超时 15 分钟兜底）             │
│     --session-id/--resume <按用户+需求稳定的会话>（对话记忆）        │
│     --settings <audit hook>（敏感 Bash 命令审计，只挂在 G 上）       │
│     spec 管理（内置规则）：注册 PRD 需求 / 按 PRD 生成 artifacts /   │
│     grill-me 澄清 / 编辑 spec.md                              │
│                                                                     │
│     __JSON_ACTION__ 动作（服务器按动作分发，不经过二次 LLM）：        │
│     approve     → 批准 + dispatch → fork B                         │
│     spec_result → 校验 spec 变更（备份/置 PARTIAL）→ 等批准          │
│     history     → 读取执行历史（runs.json）                         │
│     gray_list   → 列出待裁决灰名单草稿（带证据）                    │
│     adjudicate  → 裁决草稿；全部裁决完毕自动 approve + dispatch     │
└────────────────────────────────────────────────────────────────────┘
```

| 进程 | 身份 | 触发者 | 关键特征 |
|------|------|--------|----------|
| A | loop_engine CLI | crontab / 手动 / F | 命令分发；manual-begin/end（废弃）；scheduler.poll() 只检测不启动 |
| B | run_requirement | G (微信，即时) / A (poll 兜底) | 循环驱动；flock 锁 + 双超时 + 心跳；并发上限 max_concurrency |
| C/D/E | 每步一次性 | B | C 路由、D 干活、E 推进；D 无会话记忆，靠 previous_result 传续 |
| F | wecom_server | A (wecom start) | 常驻 :5000；LLM 分类 → JSON 动作块 → handler 进程内执行 |
| G | qodercli | F（每消息） | 按用户+需求共用会话；audit hook 审计；spec 管理（注册/生成/澄清/编辑）；5 种动作触发不同 handler |

```
~/loop_engine/                  # 代码目录（git 主仓库，开发在此进行）
├── cli.py                      # CLI 入口 + 命令处理
├── machine.py                  # 状态机路由（状态推进 + 灰名单裁决）
├── state.py                    # StateManager（原子写 + 滚动备份 + 损坏恢复）
├── directives.py               # 指令生成
├── parser.py                   # result.md 解析
├── report.py                   # 报告生成
├── spec_utils.py               # spec 工具函数 + PRD 解析 + 双层哈希
├── scheduler.py                # Layer 2 调度器（poll/dispatch/run/flock 锁）
├── scope_audit.py              # 未申报改动审计（声明 vs git 实际，可推微信）
├── setup.py                    # Phase 0 初始化
├── registry.py                 # 需求注册表
├── constants.py                # 常量（含 STATUS_TABLE 状态真值表）
├── __main__.py                 # Python -m 入口
├── pyproject.toml              # 构建配置
├── README.md                   # 使用指南
├── wecom_server/               # WeCom 机器人（F/G）
│   ├── server.py               # Flask 回调服务器（解密/验签/响应）
│   ├── router.py               # 意图分类 + JSON 动作分发 + spec 管理（平台无关，飞书复用）
│   ├── wecom_api.py            # 企业微信 API（推送/下载）
│   ├── crypto.py               # 回调消息加解密
│   └── hooks/audit_hook.sh     # 敏感 Bash 命令审计钩子
├── feishu_server/              # 飞书机器人（与 WeCom 平行，复用 router.dispatch）
│   ├── server.py               # WebSocket 长连接（官方 SDK）+ 去重 + 串行队列
│   └── feishu_api.py           # 飞书 API（app_access_token/推送）
└── tests/
    ├── test_machine.py         ├── test_state.py
    ├── test_parser.py          ├── test_directives.py
    ├── test_spec_utils.py      ├── test_setup.py
    ├── test_scheduler.py       ├── test_session_clean.py
    ├── test_audit_hook.py      ├── test_context.py
    ├── test_registry.py        ├── test_constants.py
    ├── test_router_async.py    ├── test_server.py
    ├── test_feishu_server.py   ├── test_feishu_api.py
    └── test_wecom_api.py       └── test_wecom_crypto.py

~/.qoder/loop_engine/           # 数据目录（仅数据，无代码）
├── requirements.json           # 需求注册表
├── pending.json                # poll 待执行清单
├── schedule.json               # 调度器配置（max_concurrency）
├── runs.json                   # 执行历史（requirement → 起止/轮次/结局）
├── wecom.json                  # WeCom 应用配置（密钥）
├── feishu.json                 # 飞书应用配置（密钥）
├── audit.log                   # 敏感命令审计日志
├── sessions/                   # 微信用户会话状态
└── .loop/                      # 本地循环状态

~/.qoder/skills/                # 5 个协作 Skill
├── spec-session/               # Layer 1：spec 管理会话（Dashboard/SCORE 往返）
├── prd-to-spec/                # PRD 摘要 → OpenSpec artifacts
├── grill-me/                   # 需求澄清追问
├── requirement-register/       # 注册需求 + PRD 引导（微信 G 使用）
└── manual-loop/                # 手动循环执行（已废弃，用 approve 替代）

~/.local/bin/loop_engine        # 命令行 shim
```

### 关键设计决策

- **调度器不 import 引擎核心模块**：只通过文件（`.loop/state.json`）和 CLI 子进程通信
- **跨进程锁 = fcntl.flock 内核独占锁**：`.loop/lock` 文件永久保留（绝不 unlink），进程死亡内核自动释放锁，无需死 pid 回收；锁内容只作展示/审计
- **状态持久化**：state.json 原子写（mkstemp + fsync + replace）+ 滚动 `.bak` 备份；丢失自动从备份恢复，损坏先隔离（`state.json.corrupt-<ts>`）再恢复，无备份才重建并告警
- **安全阀**：同 action 重复 3 次自动中断、200 步上限、LLM 步骤 6h / 本地命令 30s 双超时、锁心跳 60s、hard_errors 用尽 3 次转 BLOCKED
- **双层哈希**：spec_hash（内容）+ spec_norm_hash（忽略注释/格式），纯注释变更跳过 loop
- **plan_hash 变更检测**：编辑 plan 后自动从 MAKER_STEP1_RED 重跑，实现始终对齐最新方案
- **状态真值表**：状态语义收敛在 constants.py 的 STATUS_TABLE 单点定义，调度/路由/微信三处共用，消除语义漂移
- **状态隔离**：每个需求独立 `.loop/state.json`，切换 `--root` 无损
- **macOS 兼容**：pip 安装因 externally-managed-environment 被屏蔽，改用 shim 方式

---

## 十二、Agent 后端扩展调研（opencode / pi）

> 调研日期：2026-08-31。当前 LLM 后端是 qodercli 非交互模式（`--print`），本文记录接入
> opencode / pi 非交互模式的可行性结论，供后续实施参考。

### 现状耦合点

核心流程（分类 → `__JSON_ACTION__` → 执行 → 推送）与 agent 无关，qodercli 专有依赖集中在
`wecom_server/router.py`：

| 耦合点 | 位置 | 说明 |
|--------|------|------|
| 发起调用 | `_run_llm_turn` / `_classify_requirement` | `--print --session-id/--resume --model --dangerously-skip-permissions --settings` |
| 审计 hook | `_audit_settings` | 通过 `--settings` 注入 PreToolUse hook（qodercli settings.json 契约），每次 Bash/Edit/Write 前回调 `hooks/audit_hook.sh` |
| spec 编辑检测 | `_recent_spec_snapshots` | 读 audit hook 落下的 `SPEC_SNAPSHOT_<ts>_<session>_<module>` 标记文件（自造协议，非 qodercli 格式） |
| 模型读取 | `_get_model` | 读 `~/.qoder/settings.json` |

审计与 spec 纠错是**同一条依赖链**：都依赖「agent 在每次敏感工具调用前回调我们注入的脚本」。

### 结论：两家均有等价机制

| 能力 | qodercli（现状） | opencode | pi |
|------|------------------|----------|-----|
| 工具调用前回调 | PreToolUse hook（shell） | 插件 `tool.execute.before`（JS/TS，可改 args/抛错拦截） | 扩展 `tool_call` 事件（TS，可返回 `{block: true}`） |
| 装载方式 | 每次调用注入 `--settings` | `.opencode/plugins/` 自动加载，`run` 模式默认生效（`--pure` 关闭） | `~/.pi/agent/extensions/` / 项目 `.pi/extensions/` 自动发现，非交互也加载（`--no-extensions` 关闭） |
| 非交互模式 | `--print` | `opencode run` + `--continue`/`--session`、`--format json` | `pi -p` + `-c`/`-r`/`--session <path\|id>` |
| 会话归属 | `--session-id` | `--session <id>` | `--session <path\|id>` |

关键结论：**SPEC_SNAPSHOT 标记文件协议可以原样保留**——新 agent 的插件/扩展在
`tool.execute.before` / `tool_call` 里写同样的标记文件，router 端 `_recent_spec_snapshots`
与 correction 循环一行不改。审计日志同理由插件侧写入 `audit.log`。

### 建议实施路径

1. router 抽薄 backend 接口：`run_turn(session_id, prompt) -> reply` + `detect_spec_edits(session_id)`
   （qodercli 原逻辑原样包成第一个 backend）
2. 每个 agent 一个百来行的插件/扩展模块（写 audit.log + SPEC_SNAPSHOT 标记）
3. 配置项选择 backend；模型改按 backend 配置

### 注意点

- **作用域差异**：qodercli hook 每次调用注入（只影响 bot 拉起的会话）；opencode/pi 插件是
  目录自动发现，会波及用户自己的交互会话。解法：插件检查环境变量（loop_engine spawn 时
  注入 `LOOP_ENGINE_BOT=1`，否则 no-op），保住「只有 bot 会话带审计」语义
- opencode 已知 issue：`tool.execute.before` 对部分场景拦截不生效
  （github.com/anomalyco/opencode/issues/5894），接入时需实测
- pi 非交互扩展加载已确认（命令行扩展先于 trust 评估处理）；opencode `run` 默认加载插件，
  但拦截覆盖面未实测

### 参考

- OpenCode Plugins: https://opencode.ai/docs/plugins/
- OpenCode CLI: https://opencode.ai/docs/cli/
- Pi Extensions: https://badlogic-pi-mono.mintlify.app/coding-agent/extensions
- Pi Usage（-p/--session/-e）: https://pi.dev/docs/latest/usage

## 十三、Loop 执行优化路线图

### 主要矛盾

单次 run 时长剖析（示例：某 72 分钟 run，7 步流水线，模块 SCORE 95）：

| 步骤 | 耗时 |
|------|------|
| SCAN → CLASSIFY_CHANGE | 10.7 min |
| SCORE | 3.1 min |
| MAKER_STEP0 | 14.4 min |
| MAKER_STEP1_RED | 15.5 min |
| MAKER_STEP2_GREEN | 7.9 min |
| CHECKER（首次 exit 1，浪费） | 7.3 min |
| CHECKER（重试成功） | 7.0 min |
| CODE_REVIEW | 5.9 min |

**主要矛盾：LLM 冷启动固定开销 × 步骤数**。每步 spawn 新 qodercli 进程要付：Node init + hooks/skills 注册 + 全部 MCP server 拉起 + spec 从磁盘 Read + 项目结构探索，累计 ≈9 min/step 与变更大小无关。这**不是模型能力问题**（GLM/Qwen 都一样），是进程架构问题。次要矛盾：步骤串行、模型一刀切、exit 码不分类。

### 分层策略

按「改动成本 vs 收益」分四阶段。**内核（state.json 真相源 + spec_norm_hash 检测 + 强制验证闭环）不动**，只优化形态。流程有必要，但**当前形态（严格 6 步串行、每步独立 LLM 调用）不是**——它是「CLI 冷启动 ≈9 min + 模型能力不稳」这一工具成本结构下的产物。工具一变（SDK 常驻 / 快模型 / 并发），步骤切分就要重新解构：保留 spec↔code 一致性验证这个内核，否定「每 phase 一次冷启动」这个形式。

### 阶段 0 · 基线观察（进行中）

- **OBS-1** Qwen3.8-Flash 跑一周真实 run，记录：单次 run 总耗时 / 各 step 分布 / CLASSIFY magnitude 判定与实际变更规模对齐率 / exit 1 频次

### 阶段 1 · 零/微改动（对应任务 #9~#12）

不改变进程模型，不动状态机。降低主要矛盾表象烈度。

- **OPT-1 · #9 ✅ 已实现** D 侧 qodercli 加 MCP 白名单：`--strict-mcp-config --mcp-config minimal_mcp.json`。minimal 版只保留 codegraph；playwright/postgres/redis/mysql 等不再拖慢冷启动
- **OPT-2 · #10 ✅ 已实现** `_repair_result` 现真 `--resume` 上次会话（配合 OPT-3 落地），docstring 语义与实现一致
- **OPT-3 · #11 ✅ 已实现** 移除 `--no-session-persistence`，sid 派生改成 `uuid5(root:module_key)`（重试 `+ :retryN`），首步 `--session-id`、后续步 `--resume`。同模块 run 内 spec Read / 项目结构探索 只付一次。步骤成功后 `retries` 归零
- **OPT-4 · #12 待做** 模型分级：`STATUS_TABLE` 加 `model` 字段，CLASSIFY/SCORE 走 flash 档（预估 10.7 min → 2~3 min），MAKER/CHECKER 保留深度模型。风险：CLASSIFY 判错**静默走错分支**，需 2-of-3 self-consistency 或非对称复核

### 阶段 2 · SDK 迁移（未开任务，基线稳后启动）

把主要矛盾从「冷启动 × 步骤数」**降级**为「推理时间 × 步骤数」——消灭进程本身的重启动。

- **OPT-5** 接 `qoder-agent-sdk`（方案 β：单 client 串行）。每需求一 client，run 内 session 常驻多轮 `client.query()`，run 结束 close。scheduler.py:1062 `subprocess.run(cmd)` → `async with QoderSDKClient(...)`
- **OPT-6** Python bump 3.9 → 3.10+（SDK 前置要求，见「前置条件」段）
- **OPT-7** Auth 换 `access_token_from_env()` + PAT / Service Account。SDK 文档「Production checklist」推荐；不再依赖工作站登录态
- **OPT-8** audit 从 `--settings` 注入迁到 `can_use_tool` 回调 + `allowed_tools`。告别 `--dangerously-skip-permissions`；审批决策回编排侧，与 `spec_change_requires_loop` 门禁结合更实
- **OPT-9** spec 静默预注入：`query(spec_text, should_query=False)` 一次塞进 session 上下文，省每步 Read 工具调用

**关键架构事实**（docs.qoder.com/cli/sdk/how-it-works）：一个 `QoderSDKClient` = 一个 qodercli 进程 = 一个活跃 session，严格 **1:1 绑定**。运行中不能切 session；多并行 session = 多进程。`session_id`/`resume`/`fork_session` 都在 client 构造期决定。

**三条方案对比**：
- **α 多 client 并行**：每需求一 client，冷启动只付一次/需求。适合 `max_concurrency>1`
- **β 单 client 串行**：进程数受控 1，每需求 close+open 是新进程但仍付 1 次冷启动；需求内所有 step 免费。**起步最优选这个**
- **γ 单 client 单 session 服务所有需求**：不推荐，跨需求 spec 上下文会串味

### 阶段 3 · 规模/形态扩展（未开任务）

主要矛盾降级后，扩大外延、精细化斗争方式。

- **OPT-10** 并发化（方案 α + scheduler `max_concurrency > 1`）。N 需求并跑 = N 个常驻 client。前提：LLM 配额扛得住；session 目录不冲突
- **OPT-11** 会话合并（多 phase 一 turn）：**SDK 后自动吸收**——phase 保持独立但共享 session，「多 phase 一 turn」不再是必需。原任务 #8 因此已删除
- **OPT-12** STATIC_DIFF 前移：加非 LLM 步骤做纯静态一致性检查（字段名 grep / API 签名 AST 比对 / import 引用），挡在 CHECKER 前。**把矛盾暴露在成本最低处**——能用确定性发现的，别交给概率模型
- **OPT-13** exit 码分级重试：`_run_llm_turn`（`wecom_server/router.py:880`）/ machine.py commit_error 分支加错误特征。网络/429 → 短 backoff；格式错 → 单步重发 prompt；代码/环境错 → 走 fix 路径。避免 CHECKER 一次 exit 1 白烧 7 min

### 独立线（与主线无关，随时可插入）

- **OPT-14** `_detect_requirement` 关键词别名：registry 加 `keywords` 字段，「越库二期」之类中文名能命中 `cross-dock-v2-backend`。落点 `wecom_server/router.py:200`
- **OPT-15** 飞书进程 watchdog：`crontab` poll 里检查 feishu 进程存活，挂了自动重启。避免凌晨 DNS 断链那类事件的静默窗口

### 推荐节奏

1. 本周 OBS-1 建基线
2. 基线稳后 → **OPT-1 + OPT-2** 并行（零/低风险）
3. 一周效果确认 → **OPT-3**（会话持久化）
4. 再观察 → **OPT-4**（分级模型）
5. 阶段 1 全部落地跑 2 周 → 启动**阶段 2**（SDK 迁移）
6. 阶段 2 稳后按需做**阶段 3**

### 反例（曾提出但被撤回）

- **「SCORE ≥95 的轻量变更跳过 CLASSIFY_CHANGE」错误**：SCORE 衡量的是 spec 完成度，与单次变更幅度**正交**。一个 98 分 spec 完全可能加一个新 API（重量级），跳过 CLASSIFY 会直接路由错误。撤回该提议，改为 OPT-4 分级模型来压缩 CLASSIFY 的耗时