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

## 规划中

### Phase 2 — 持久化与记忆

目标：Agent 退出后能恢复对话，不再丢失上下文。分三个子阶段：

#### Phase 2.1 — .env 配置（已完成）

| 功能 | 状态 | 说明 |
|------|------|------|
| `DUMMY_API` 环境变量 | ✅ 完成 | 从 `DUMMY_API` / `DUMMY_AGENT_API_KEY` 读取 API Key |
| Base URL 配置 | ✅ 完成 | 支持 `DUMMY_AGENT_BASE_URL` 环境变量 |
| `.env` 文件自动加载 | ✅ 完成 | 启动时使用 `load_dotenv()` 自动读取 `.env` 文件 |

#### Phase 2.2 — Context 压缩（待设计）

| 功能 | 说明 |
|------|------|
| Token 计数 | 监控对话历史 token 用量 |
| 自动摘要 | 接近上下文上限时，自动压缩早期对话 |
| 压缩策略 | 摘要旧轮次 vs 截断 tool 结果 vs 丢弃完整 tool 调用链 |

#### Phase 2.3 — Session 持久化与搜索（已完成基础版）

| 功能 | 状态 | 说明 |
|------|------|------|
| SQLite 存储 | ✅ 完成 | `sessions` / `messages` 表，`chat()` 自动写入数据库 |
| 退出恢复 | ✅ 完成 | `/resume` 恢复最近一次会话，支持指定 `session_id` |
| Session 列表 | ✅ 完成 | `/sessions` 查看会话列表与消息数量统计 |
| 全文搜索 | ⬜ 待做 | 后续可接 FTS5 索引，支持按关键词搜索历史对话 |

### Phase 3 — 自主学习

目标：Agent 越用越聪明，不重复犯错。

| 功能 | 说明 |
|------|------|
| Skills 系统 | 把复杂任务流程固化为可复用的 SKILL.md，下次自动加载 |
| 跨 Session 记忆 | 持久化用户偏好、项目事实（SQLite + 向量 DB） |
| Curator | 自动管理技能库：合并重复、标记过时、归档废弃 |

### Phase 4 — 自主运行

目标：Agent 不再被动等待输入，能主动干活、多平台接入。

| 功能 | 说明 |
|------|------|
| Cron 调度 | 定时任务，如"每天早上 9 点检查服务器状态" |
| Gateway | 接入 Telegram / Discord / 邮件等多消息平台 |
| Sub-agent 委派 | 复杂任务拆解为子任务并行执行 |
| Plugin 系统 | 第三方工具以插件形式注册，不修改核心代码 |

### Phase 5+ — 远期方向

| 方向 | 说明 |
|------|------|
| 树形历史 | 分支对话（而非线性覆盖），每个分支绑定 git worktree |
| 多模态 | Agent 能看图、生成图、语音交互 |
| MCP 协议 | 接入标准工具协议，兼容 MCP 生态的工具 |
| 桌面端 UI | Electron / Tauri 图形界面 |
| Agent 自省 | Agent 分析自身行为日志，发现效率瓶颈并改进 |

---

## 架构原则

| 原则 | 说明 |
|------|------|
| **松耦合** | 核心循环（core.py）不依赖具体工具，通过注册表注册发现 |
| **渐进增强** | 每个 Phase 产出可用产品，不是空中楼阁 |
| **先教后交** | LLM 决定"做什么"，代码决定"怎么做" |
| **路径安全** | write_file 等工具在项目目录内无限制，目录外需用户确认 |
