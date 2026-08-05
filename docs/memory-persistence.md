# Agent 记忆持久化：设计、实现与当前状态

## 1. 为什么需要记忆持久化

在最早的版本里，Agent 的记忆只存在于当前 Python 进程的内存中：

- `DummyAgent.history` 保存着当前会话的消息列表
- `history` 在程序退出后就会丢失
- 同一个项目重新启动时，Agent 只能从零开始

这在教学项目里可以很好展示“LLM + tools + loop”的核心结构，但对真正可用的 Agent 来说，这种状态太弱：

- 用户不得不重复输入上下文
- 之前工具调用的结果无法恢复
- Agent 无法做跨轮次、跨会话的连续推理

因此，项目需要把“当前记忆”从临时内存变成“可持久化存储”。

这一步本质上就是把浅层记忆升级为“可恢复记忆”。

## 2. 记忆持久化的目标

这次落地的目标不是一次性引入完整的高级记忆系统，而是先把最基本、最稳定、最有价值的能力做出来：

1. 让每次 `chat()` 的历史可以被保存
2. 让 Agent 退出后重新启动时，可以恢复最近的会话
3. 让用户可以通过命令查看已有会话列表
4. 让后续的 context compression、跨 Session memory、有结构的长期记忆，建立在稳定的基础设施上

也就是说：

- 先“可存”
- 再“可恢复”
- 再“可压缩”
- 再“可复用”

这条路径和项目的渐进式实现思路是对齐的。

## 3. 为什么不用单纯的 JSON 文件保存记忆

我们最容易想到的方案，是把每一轮对话直接保存成 JSON 文件，例如：

- `logs/conversation_YYYYMMDD_HHMMSS.json`

这种方案确实能工作，但它有明显局限：

1. 纯 JSON 文件难以做高效查询
   - 想查某个会话里到底有多少条消息，需要把文件整个读出来
   - 想按时间、最近活跃、会话 ID 进行索引查询，JSON 不够方便

2. JSON 适合“日志”，不适合“记忆数据库”
   - 日志更像一份顺序档案
   - 记忆系统往往要做搜索、恢复、重建上下文、结构化统计

3. 后续还要接 context compression / FTS / 向量召回
   - 这几步都非常适合数据库/索引结构，而不是单文件备份

因此，这次选用 SQLite，而不是把所有历史都写成 JSON 文件。

## 4. 这次实现的核心设计

### 4.1 数据模型概览

这次引入了一个轻量级的 SQLite 存储层，核心是两张表：

- `sessions`
  - 记录每个会话的元信息
  - 字段包括：
    - `id`
    - `created_at`
    - `updated_at`

- `messages`
  - 记录每条消息的内容
  - 字段包括：
    - `id`
    - `session_id`
    - `sequence`
    - `role`
    - `content`
    - `tool_call_id`
    - `payload`
    - `created_at`（消息首次写入时间；重写历史时保留，不被覆盖）

这套数据结构有两个关键点：

1. `session_id` 让一组消息有唯一归属
2. `sequence` 保证消息顺序稳定，恢复时可以按顺序重建 `history`

### 4.2 为什么拆成 session + message 两层

如果只维护一张“大消息表”，也可以保存对话，但逻辑上不够清晰：

- 会话元信息和消息内容耦合在一起时，后续扩展困难
- 后续想做多会话恢复、按 session 统计、按 session 搜索，就更不方便

所以采用“会话元信息表 + 消息明细表”的结构。

这也是一个典型的数据库设计思路：

- `sessions` 负责“描述一次对话是谁/什么时候开始/什么时候更新”
- `messages` 负责“这次对话具体说了什么”

## 5. 本次对代码的具体改动

### 5.1 新增 `session_store.py`

新增了 [session_store.py](../session_store.py) 模块，封装了所有 SQLite 存储逻辑：

- `_ensure_schema()`：创建 `sessions` / `messages` 两张表
- `create_session()`：创建新的 session
- `list_sessions()`：列出会话列表，并统计每个会话的 message 数量
- `save_history()`：把当前 `history` 逐条 upsert 写入数据库（`(session_id, sequence)` 为稳定键；超出新长度的旧消息会被删除，兼容未来历史缩水；`created_at` 保留首次写入时间）
- `load_history()`：从数据库恢复消息列表
- `get_latest_session_id()`：拿到最近一次更新的 session

这层抽象的目的非常明确：

- `core.py` 不需要自己拼 SQL
- 未来如果换成更复杂的存储实现（如 Redis、向量库、文件备份），也不会影响 Agent 逻辑

### 5.2 Agent 在 `chat()` 中自动落库

在 [core.py](../core.py) 中做了如下修改：

- `DummyAgent` 增加了 `SessionStore` 依赖
- 增加 `current_session_id` 字段，表示当前 Agent 绑定到哪个会话
- 在 `chat()` 的入口处，如果当前没有绑定 session，就自动创建一个新 session
- 在消息进入 history 后，会同步调用 `_persist_history()`
- 在工具调用执行中，LLM 产出的 tool result 也会一并回写到历史并持久化
- 当最终回答输出后，assistant 消息也会写进 history 并落库
- `MAX_TOOL_TURNS` 触发 fallback 时，同样会保存最后的 fallback 回复

这意味着：

- 一轮用户输入之后，Agent 能够把“当前状态”完整记录下来
- 之后即使进程退出，历史也不会丢失

### 5.3 `resume` 与 `resume <session_id>` 恢复逻辑

在 [core.py](../core.py) 里新增了两个恢复方法：

- `resume_last_session()`：恢复最新的持久化会话
- `resume_session(session_id)`：恢复指定会话

恢复逻辑的核心步骤是：

1. 从 SQLite 中读取指定 session 的所有 message
2. 反序列化为 `history` 列表
3. 用 `self.history = loaded_history`
4. 把 `current_session_id` 绑定回指定 session

这让 Agent 在新的进程中重新恢复上下文，不依赖当前内存中的旧状态。

### 5.4 CLI 交互命令增强

在 [main.py](../main.py) 中新增并增强了这些命令：

- `/resume`：恢复最近一次会话
- `/resume <session_id>`：恢复指定会话
- `/sessions`：查看所有会话列表

此外，命令帮助信息也做了补充，并把恢复反馈做得更友好：

- 显示恢复到的 session_id
- 显示恢复后的消息条数
- 列表输出带有 `message_count`、`created_at`、`updated_at`

这一步很重要，因为“能恢复”本身还不够，CLI 也需要让用户清楚知道：

- 当前有哪些 session
- 哪个是最新的
- 现在已经恢复到哪一个

### 5.5 `.gitignore` 改造

为了避免把 runtime artifact 误提交到仓库，补充了：

- `session.db`
- `logs/`

这样数据库和运行日志不会污染版本控制。

## 6. 这次做这些改动的思路与决策

### 决策 1：优先做“存储层”，不先做压缩

这次没有立刻做 context compression。原因很明确：

- 如果你连 session 都不能恢复，压缩也没有基础
- 压缩讨论的前提是：历史已经能稳定保存、能恢复、能被重新加载

所以这次的顺序是：

1. 先保存记录
2. 再恢复回历史
3. 再考虑压缩历史

这符合工程上“先解决可用性，再解决复杂度”的原则。

### 决策 2：SQLite 优于纯 JSON / 单文件日志

虽然纯 JSON 文件也能保存历史，但它并不适合作为未来 Agent 的长时记忆基础：

- 查找不方便
- 统计不方便
- 和 future FTS / summary / vector retrieval 结合时不够自然

SQLite 的优势在于：

- 关系结构清晰
- 不需要额外依赖
- Python 自带 `sqlite3` 即可使用
- 后续可以接 FTS5、索引、统计查询

### 决策 3：history 仍然保持 OpenAI 风格消息列表

为保持兼容性，数据库中的 `messages` 是以 `role/content/tool_call_id` 的形式保存的原始结构化消息。因为：

- Agent 当前的核心逻辑本来就是基于 OpenAI 风格 messages 列表
- 如果数据库存的是“原始格式”，恢复时只需要简单反序列化即可
- 这样实现成本最小，风险也最低

这是真正的“最小可用改造”：不重写 core loop，也不更改接口结构。

### 决策 4：把会话恢复能力放在 Agent 本身，而不是只放在 CLI

恢复能力不仅要显式支持 CLI 命令，还应该作为 Agent 的能力接口存在：

- `resume_last_session()`
- `resume_session(session_id)`

这样做的好处是：

- 不同入口（CLI、测试、后续 UI）都可以复用同一套恢复逻辑
- 记忆恢复从“单纯命令行为”升级为“Agent 的可复用能力”

## 7. 这次实现后的效果

现在这个项目已经从“只在当前进程内保存上下文”的模式，升级成了“有会话身份、可回放、可恢复”的模式。

也就是说，当前 Agent 可以做到：

- 运行一次对话
- 结束进程
- 再重新开启程序
- 调用 `/resume` 或 `/resume <session_id>`
- 恢复到某个历史会话

这已经是真正意义上的“记忆持久化”基础能力，而不是只停留在临时内存。 

## 8. 当前进度与后续自然演进路径

### 当前已完成

- `session.db` 结构化存储
- `sessions` 和 `messages` 两表
- 每次 `chat()` 自动持久化历史
- `/resume` 恢复最近一次会话
- `/resume <session_id>` 恢复指定会话
- `/sessions` 基础列表查询与统计
- CLI 输出增强

### 接下来最自然的下一步

下一步不是再堆更多工具，而是继续推进“记忆管理的第二层”：

1. `Context Compression`：
   - 例如：保留最近 N 轮消息
   - 对更早的消息做摘要替换

2. `跨 Session 记忆`：
   - 用户偏好
   - 项目事实
   - 常见 workflow
   - 这些可以后续在 SQLite / vector store 里进一步复用

3. `FTS / semantic search`：
   - 对历史会话做关键词搜索
   - 对长期记忆做语义召回

## 9. 一句话总结

这次的记忆持久化工作，本质上是在做一件非常重要的工程升级：

“把 Agent 的短期上下文从进程内存提升为真正可恢复、可查询、可复用的会话记忆。”

也就是说：

- 之前：Agent 只会记当前这次运行
- 现在：Agent 能记住“会话历史”并在下次启动时恢复回来

这一步完成之后，后续的 context compression、跨 Session 记忆、长期知识库，都会更容易接上。
