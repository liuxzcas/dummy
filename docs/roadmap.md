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

### Phase 1 — 工具扩展（进行中）

| 工具 | 状态 | 说明 |
|------|------|------|
| `tools/read_file.py` | ✅ 完成 | 支持 offset/limit 翻页，LLM 可自主分页读取大文件 |
| `tools/write_file.py` | ✅ 完成 | 覆盖/追加模式、原子写入、Diff 预览、路径安全确认 |
| `tools/search_files.py` | ⬜ | 搜索文件内容和文件名 |
| `tools/web_search.py` | ⬜ | 网络搜索 |
| `tools/web_extract.py` | ⬜ | 网页内容提取 |

**dispatch 层加固（待做）：**

- 统一超时控制
- result 规范化（None → ""、异常兜底）
- 工具执行前的安全过滤

---

## 规划中

### Phase 2 — 持久化与记忆

目标：Agent 退出后能恢复对话，不再丢失上下文。

| 功能 | 说明 |
|------|------|
| Session 持久化 | SQLite 存储对话历史，支持 `/resume` 恢复 |
| .env 配置 | API Key、base_url 等配置存文件，不用每次手输 |
| Session 搜索 | 全文搜索历史对话内容 |
| Context 压缩 | 对话超出 token 限制时，自动摘要旧内容 |

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
