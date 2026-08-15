# Dummy Agent — 开发路线图

从零实现 LLM Tool-Calling Agent 的教学级项目，以最小原型为起点，逐步添加功能。

## 已完成

### Phase 0 — 最小原型

核心 Tool-Calling Loop 跑通，奠定所有后续阶段的基础。

| 组件 | 说明 |
|------|------|
| `core.py` | DummyAgent 核心循环：调 LLM → 执行工具 → 回注 → 再调 |
| `llm.py` | LLM API 封装（OpenAI 兼容协议，当前使用 DeepSeek） |
| `tools/registry.py` | Tool + ToolRegistry 注册表模式 |
| `tools/terminal.py` | terminal 工具（`subprocess.run`, `shell=True`） |
| `prompt.py` | System Prompt 构建器，动态从注册表获取工具列表 |
| `main.py` | CLI REPL 入口 |

设计文档：`docs/design.html`（英文）、`docs/design-zh.html`（中文）

### Phase 1 — 工具扩展（已基本完成）

| 工具 | 状态 | 说明 |
|------|------|------|
| `tools/read_file.py` | ✅ 完成 | 支持 offset/limit 翻页，LLM 可自主分页读取大文件 |
| `tools/write_file.py` | ✅ 完成 | 覆盖/追加模式、原子写入、Diff 预览、路径安全确认 |
| `tools/search_files.py` | ⬜ 待做 | 搜索文件内容和文件名 |
| `tools/web_search.py` | ✅ 完成 | 使用 Tavily Search API 返回结构化网络搜索结果 |
| `tools/web_extract.py` | ✅ 完成 | 提取网页正文文本，自动补协议、清洗噪音 |

**dispatch 层加固（已完成/部分完成）：**

- 统一超时控制 ✅
- result 规范化（None → ""、异常兜底） ✅
- 工具执行前的安全过滤（工具级已实现） ✅
  - `terminal`：执行前询问确认
  - `write_file`：目录外路径需用户确认
  - centralized dispatch 级安全过滤还未完全抽象化

---

### Phase 2 — 持久化与记忆（已完成 ✅）

目标：Agent 退出后能恢复对话、跨会话记住事实。四个子阶段 + 综合测试集全部落地：

| 子阶段 | 状态 | 说明 |
|--------|------|------|
| 2.1 环境配置 | ✅ | `DUMMY_API` / `DUMMY_AGENT_BASE_URL` / `.env` 自动加载 |
| 2.2 Context 压缩 | ✅ | 两级压缩（ToolResult 折叠 + 增量摘要）、质量门 8/8、真实冒烟 5/5 — [context-compression.md](./context-compression.md) |
| 2.3 Session 持久化与全文搜索 | ✅ | SQLite 持久化、`/resume` `/sessions`、FTS5 双表（英文词干 + 中文 trigram）`/search` — [fts-search.md](./fts-search.md) |
| 2.4 跨 Session 记忆 | ✅ | 方案 4 定稿：全量常驻注入 + 历史 FTS 兜底，T5 28/30 = 93% — [memory-system.md](./memory-system.md) |
| 综合测试集 | ✅ | T1-T4 pytest 72 项全绿 + T5 LongMemEval 真实模型 30 题 93% — [phase2-test-suite.md](./phase2-test-suite.md) |

> 关键决策沉淀：两级压缩 L1/L2 分工与有损边界、FTS 双表方案、记忆方案 4（四轮实验 63%→93%，见 memory-system.md）、工具确认与 /p 打断（docs/experiences/tool-interrupt-star.md）

---

## 项目定位（2026-08-14 重新定位）

> 从"从零学习的 Agent 项目"转向**专属个人助理**：真正帮用户干活、让用户愿意持续使用。
> 学习价值保留——设计沉淀已入 docs/experiences/ 与各设计文档,后续开发同时继续积累工程经验。

### 设计原则

| 原则 | 说明 |
|------|------|
| **为真实使用设计** | 每个功能服务用户的真实场景（研究、代码、写作、信息整理），不做无使用场景的功能 |
| **越用越好** | 记忆 + 技能双沉淀是核心引擎：记住偏好、固化流程，使用越久越省事 |
| **低摩擦** | 启动快、交互短、常用路径一步到位；"愿意继续用"的动力来自每次使用都有正反馈 |
| **可信** | 确认 / 打断 / 可追溯（Phase 2 已建,保持）——把关键操作交给它之前用户放心 |
| **主动** | 从"被动回答"到"主动帮忙"：定时任务、提醒、监听、完成汇报 |

### 场景驱动开发

用户列出真实使用场景 → 评估（复用度 / 工程量 / 频次）→ 加工具或流程。
每个功能必须能回答："这解决了我哪个日常痛点？"

## 规划中

### Phase 3 — 自主学习（技能沉淀）

目标：Agent 越用越聪明，不重复犯错。日常重复流程固化为可复用技能。

| 功能 | 说明 |
|------|------|
| Skills 系统 | 把复杂任务流程固化为可复用的 SKILL.md，下次自动加载——优先面向真实高频场景（文献综述 / 周报 / 代码审查等流程固化） |
| Curator | 自动管理技能库：合并重复、标记过时、归档废弃 |

### Phase 4 — 主动性（原"自主运行"分解）

目标：Agent 不再被动等待输入，能定时干活、主动汇报。

| 功能 | 说明 |
|------|------|
| Cron 定时任务 | 定时任务与提醒，如"每天早上 9 点检查服务器状态"、"每周五生成周报" |
| 文件/事件监听 | 监听指定文件、目录、Git 事件变化，触发相应处理 |
| 主动汇报 | 后台任务完成 / 异常时主动通知（不再等用户来问） |
| 后台长任务 | 大任务（批量处理、长调研）后台运行，完成汇报结果 |

### Phase 5 — 接入与随时可用（原 Phase 4 Gateway / Sub-agent + Phase 5 桌面 UI 分解）

目标："随时能用"才愿意用——多入口接入，多任务并行。

| 功能 | 说明 |
|------|------|
| 消息平台 Gateway | 接入 Telegram / Discord / 邮件等多消息平台，手机、桌面随时叫它干活 |
| 桌面快捷入口 | 轻量桌面入口（快捷启动 / 系统托盘），不打开终端也能用 |
| 并行任务（Sub-agent） | 复杂任务拆解为子任务并行执行 |

### Phase 6 — 远期方向（原 Phase 5+ 重排）

| 方向 | 说明 |
|------|------|
| Plugin 系统 | 第三方工具以插件形式注册，不修改核心代码 |
| 树形历史 | 分支对话（而非线性覆盖），每个分支绑定 git worktree |
| MCP 协议 | 接入标准工具协议，兼容 MCP 生态的工具 |
| 多模态 | Agent 能看图、生成图、语音交互 |
| 桌面端 UI（完整版） | Electron / Tauri 图形界面 |
| Agent 自省 | Agent 分析自身行为日志，发现效率瓶颈并改进 |

---

## 架构原则

| 原则 | 说明 |
|------|------|
| **松耦合** | 核心循环（core.py）不依赖具体工具，通过注册表注册发现 |
| **渐进增强** | 每个 Phase 产出可用产品，不是空中楼阁 |
| **先教后交** | LLM 决定"做什么"，代码决定"怎么做" |
| **路径安全** | write_file 等工具在项目目录内无限制，目录外需用户确认 |
