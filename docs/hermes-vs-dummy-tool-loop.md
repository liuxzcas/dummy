# Hermes 在工具循环上的特别之处 —— 与 dummy 逐项对照

> **对照基准**：Hermes 本地源码快照 `C:/Users/xlinz/AppData/Local/hermes/hermes-agent`
> （commit `c7e09f25`，`run_agent.py` 6055 行 / `cli.py` 16304 行 / `agent/` 130 个模块）
> 对照对象：dummy `D:/Engineering/dummy`（`core.py` 1330 行）
> **方法**：全部结论取自源码，附 `file:line`；推测项一律标注「未验证」。

---

## 0. 结论速览

| 维度 | Hermes | dummy 现状 | 差距性质 |
|---|---|---|---|
| 运行中输入回车 | 三态可选 `queue`/`steer`/`interrupt` | 仅认 `/p` 前缀，其余**静默丢弃** | 机制已有，**策略缺失 + 有 bug** |
| 打断实现 | 标志位 + 中止 HTTP socket + 线程级传播 | 抛 `InterruptSignal` | 同构，Hermes 覆盖面更广 |
| 停止判据 | 5 条优先级链 + 2 道收尾拦截 | 2 条（纯文本 / 轮次上限） | **结构性差距** |
| 工具护栏 | 重复失败/无进展阈值 → 循环内实时 block/halt | **有会话级错误率告警**（面向人去改工具），无循环内护栏 | 同类**不同层**，见 §2.3a |
| 收尾前证据门 | `verify-on-stop` 要求"新鲜通过证据" | 无（`write_file` 只查语法） | **缺失，且是你关心的核心** |
| 历史完整性 | 运行期补占位 + 回放期剥离 | `repair_tool_pairing` 补占位 | dummy 已对齐运行期方案 |
| 磁盘善后 | 状态锚定 HOME + 快照回滚 + 进程台账 | 无 | 缺失 |
| 预算模型 | `IterationBudget`（可退票） + 90 | `MAX_TOOL_TURNS = 40` | 量级与模型差距 |

---

## 1. 运行中输入回车：Hermes 的三态语义

这是你的**想法 1** 的直接对照物。

### 1.1 Hermes 的做法

`display.busy_input_mode` 决定"Agent 正在干活时按回车"的含义，默认 `interrupt`：

```python
# hermes_cli/config.py:1766
"busy_input_mode": "interrupt",  # interrupt | queue | steer

# cli.py:3736  语义注释（原文）
# "interrupt" (Enter interrupts current run),
# "queue"     (Enter queues for next turn),
# "steer"     (Enter injects mid-run via /steer, arriving after the next tool call)
```

三种模式的落点是三个不同的队列/通道：

```python
# cli.py:13522   steer：直接调 agent.steer()，接受失败则回退 queue
# cli.py:13543   queue：  self._pending_input.put(payload)
# cli.py:13548   interrupt：self._interrupt_queue.put(payload)
```

改默认值：`hermes config set display.busy_input_mode steer`（`/busy` 命令同源，
`hermes_cli/cli_commands_mixin.py:2580`）。

**`steer` 的关键设计**——不打断当前工具，等这一批工具跑完再注入：

```python
# run_agent.py:2766  原文注释
# this does NOT stop the current tool call. The text is stashed and the agent
# loop appends it to the LAST tool result's content once the current tool batch
# finishes
```

注入形态是 marker 包裹后**追加进最后一条 tool 消息的 content**
（`agent/prompt_builder.py:601`），drain 点共 3 处：

| 时机 | 位置 |
|---|---|
| 顺序路径，每个工具执行后 | `agent/tool_executor.py:1685` |
| 并发批末 | `agent/tool_executor.py:1018` |
| 下次 API 调用前（处理"模型思考期间"到达的 steer） | `agent/conversation_loop.py:716-740` |

**整轮没有工具调用**时（leftover steer）不会丢：`agent/turn_finalizer.py:469-471`
把残留回传，`cli.py:12754-12758` 降级为下一轮用户消息。

### 1.2 dummy 的做法与两个问题

现有机制（并不缺）：

| 组件 | 位置 | 行为 |
|---|---|---|
| `_InterruptListener` | `core.py:124-190` | **Windows 专用**（msvcrt）守护线程，20ms 轮询 stdin，字符入 buffer **不回显**，CR/LF 记行结束；非 Windows/非 console **退化为空操作** |
| `_make_confirm()` | `core.py:1214-1239` | 所有确认输入的**唯一入口**；`input()` 期间 `pause()` 轮询避免抢 stdin；命中 `/p` → `raise InterruptSignal` |
| `_drain_interrupt()` | `core.py:1241-1248` | 检查点 `take_line()` 消费完整行，**只认 `/p`** |

**问题 A（实测确认）：非 `/p` 输入被静默丢弃。**

`take_line()` 是"取出即消费"，判定失败后原行不落任何地方：

```
take_line 取到 : '等等,别删那个文件'
是否判定为打断 : False
再取一次(buffer): None      ← 原文已消失
```

**问题 B（设计层面）：一行输入到底是什么，有语义歧义。**

工具执行中可能正弹确认提问（`y/n/d`）。此时一行既可能是"我回答 y"，也可能是"打断，改做 X"。
`/p` 前缀就是为消歧引入的（`core.py:1222-1225` 记录了 2026-08-14 定稿：
确认语义归 handler、`/p` 拦截归 core，handler 不裸调 `input()`）。

所以**不能直接把任意文本放开成打断**，否则敲 `y` 会被当成打断，确认流程被砸掉。

### 1.3 可落地的方案（分两步，第二步可选）

**第一步（独立成立，建议先做）**：输入永不丢弃。

规则改为"**有确认提问时按 `/p`；无确认提问时任意非空文本即打断并作为提示词**"。
`_drain_interrupt` 消费到非 `/p` 的行时不再丢弃，而是当作打断 + 提示词，
省掉打断后再阻塞式 `input()` 收集提示词的二次交互（顺带解决"二次等待"体验问题）。

理由：`_make_confirm`（确认路径）与 `_drain_interrupt`（空闲路径）本就是两个入口，
消歧规则天然按入口划分，不需要引入新概念。

**第二步（对齐 Hermes，可选）**：把策略做成配置项 `busy_input_mode`（`interrupt`/`steer`），
`steer` 语义照 Hermes——不打断、批末注入到 tool 结果。dummy 缺并发工具批，
注入点只需一处（顺序执行，每工具后）。

---

## 2. 如何衡量任务是否完成：Hermes 是优先级链，不是单一条件

这是你的**想法 2** 的第一半。dummy 只有两条判据，"够用但粗糙"。

### 2.1 Hermes 的 5 条中止判据（按优先级）

| # | 判据 | 位置 | 退出标记 |
|---|---|---|---|
| 1 | 用户打断 | `conversation_loop.py:649-651` | `interrupted_by_user` |
| 2 | 预算耗尽 | `conversation_loop.py:663-665` | `budget_exhausted` |
| 3 | 工具护栏硬停 | `conversation_loop.py:4747` | `guardrail_halt` |
| 4 | 空响应重试耗尽 | `conversation_loop.py:5076` | `empty_response_exhausted` |
| 5 | **正常结束**：模型返回纯文本、无 tool_calls | `conversation_loop.py:5298` | `text_response(finish_reason=…)` |

循环前置条件是**双上限与**：

```python
# conversation_loop.py:643
while (api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0) \
        or agent._budget_grace_call:
```

成功完成态还要求"有最终回复且未失败"：

```python
# agent/turn_finalizer.py:150-157
completed = (
    final_response is not None
    and not failed
    and (api_call_count < agent.max_iterations or normal_text_response)
)
```

### 2.2 收尾前拦截：`verify-on-stop`（最值得 dummy 借鉴的一条）

在"即将以 text_response 结束"之前插一道**证据门**（`conversation_loop.py:5192-5198`）：

```python
if verify_on_stop_enabled():
    _verify_nudge = build_verify_on_stop_nudge(
        session_id=..., changed_paths=getattr(agent, "_turn_file_mutation_paths", set()),
        attempts=getattr(agent, "_verification_stop_nudges", 0))
```

拦截条件（`agent/verification_stop.py`）：

- 本轮**改动过**且路径是**可验证类型**（`.md/.txt/.csv` 等被过滤掉，`:24-39`、`:256`）
- `verification_status != "passed"`（`:271-273`）
- 尝试次数 `< max_attempts`（默认 2，`:250,257`）

命中则 `finish_reason="verification_required"`，追加一条**合成 user 消息**并要求 `continue`
（`conversation_loop.py:5230-5245`），把原答案降级为 `_pending_verification_response` 兜底。

一句话概括：**"你说改完了，但你没拿出改完的证据"——驳回，继续干**。
这正对应你说的"如何衡量任务是否完成"。

### 2.3 工具护栏：把"LLM 反复踩同一个坑"变成可数阈值

`agent/tool_guardrails.py`，默认**只告警不硬停**（`:72-73`
`warnings_enabled=True, hard_stop_enabled=False`），阈值：

```python
# tool_guardrails.py:74-79
exact_failure_warn_after: int = 2
exact_failure_block_after: int = 5
same_tool_failure_warn_after: int = 3
same_tool_failure_halt_after: int = 8
no_progress_warn_after: int = 2
no_progress_block_after: int = 5
```

三类信号：

| 信号 | 判据 | 动作 |
|---|---|---|
| 完全相同失败 | `tool+args` 哈希相同（`ToolCallSignature` `:127-141`） | 达 5 → `block`（`:247-261`） |
| 同一工具失败 | 不问参数 | 达 8 → `halt`（`:306-319`） |
| 幂等工具无进展 | 仅只读工具（`IDEMPOTENT_TOOL_NAMES` `:20-39`）结果哈希重复 | 达 5 → `block`（`:263-281`） |

`before_call` 判 block，`after_call` 记账并 warn/halt；block 生成**合成 tool 结果**，
halt 结束本轮。这一层直接命中你日志里"搜索质量差还反复搜"那类问题。

### 2.4 todo 与 /goal：与"完成判定"的真实关系（易误解，特此澄清）

- **todo 不参与停止判定**。`tools/todo_tool.py` 纯内存任务清单（`:22` 四态），
  唯一与流程的耦合是**压缩后重注入未完成项**（`:129-133`）。
  **未找到**任何"todo 未完成则阻止停止"的逻辑。
- **`/goal` 才是一套独立的续跑环**（`hermes_cli/goals.py`）：每轮结束调辅助模型
  `judge_goal` 判 `done/continue/wait/skipped`（`:836`，语义 `:116-148`）；
  `continue` → 注入续跑提示（`:1547-1558`）；`wait` → 挂起（`:1470-1486`）；
  独立轮预算 `DEFAULT_MAX_TURNS = 20`（`:51`），用尽转 `paused`（`:1529-1531`）；
  judge 失败 **fail-open 为 continue**（`:872-874`）。

### 2.5 预算模型：`IterationBudget`（含退票）

`agent/iteration_budget.py` 是线程安全计数器：

```python
consume()   # :37-43   _used >= max_total → False
refund()    # :45-49   给 execute_code 迭代退票
remaining   # :57-59   max(0, max_total - _used)
```

数字来源：父 agent `max_iterations` 默认 **90**（`:5-6,21`，配置名 `agent.max_turns`，
`hermes_cli/config.py:991`）；子 agent `delegation.max_iterations` 默认 **50**
（`config.py:2231`）。`max_turns` 是配置名，读入后作 `max_iterations` 建 agent
（`cli.py:3866-3877` → `cli.py:7096`）。

**耗尽后的行为值得注意**：不是直接抛错，而是
`turn_finalizer.py:82-97` `_handle_max_iterations` **剥掉工具、额外放一次调用让模型总结**。

### 2.3a 澄清：dummy 现有的"错误率检测"与 Hermes 护栏不是一回事

两者形似（都数错误次数、都用阈值），但**层次、时机、动作、施加对象全不同**，不能互相替代：

| | dummy 现有（`self_improve.py:41 detect_tool_issue`） | Hermes 护栏（`tool_guardrails.py`） |
|---|---|---|
| 统计粒度 | **按工具名**的会话累计成功率 | 按**具体调用签名**（`tool+args` 哈希）与**结果哈希** |
| 时机 | 每轮 chat 开头检查一次（`core.py:342`）——**事后** | `before_call`/`after_call` 挂钩——**循环内实时** |
| 阈值 | 调用数 ≥ `MIN_ERRORS` 且错误率 > `ERROR_RATE_THRESHOLD = 0.30`（`self_improve.py:30-32`） | 相同失败 5 次 block、同工具 8 次 halt、只读无进展 5 次 block |
| 动作 | **打印一行提示**：`⚠️ 检测到 X 错误率偏高，输入 /improve X 可分析修复`（`core.py:346` 附近） | **block**：生成合成 tool 结果塞回；**halt**：结束本轮 |
| 施加对象 | 提示**人**去改进工具实现（I4-A 定稿，链到 self_improve） | 约束 **LLM** 别再重复同一个无效动作 |
| 目的 | 工具代码质量演进 | 单次任务内的**止损** |

一句话：dummy 那层是**"这工具最近老出问题，你去改改代码"**（跨会话、面向开发者）；
护栏是**"你现在第 5 次踩同一个坑了，这条我不执行了"**（单轮内、面向模型）。
两者**都要有**，不是替换关系。

**可复用的既有范式**：dummy 的压缩器已有"连续失败达阈值 → 暂停该机制"的实现
（`compressor.py:90` `max_consecutive_failures = 3` → `paused`，`:144-147`），
护栏可以照这个已有 idiom 写，不必另创一套。

### 2.6 dummy 的现状

```python
# core.py:205
MAX_TOOL_TURNS = 40
```

两条判据：模型返回纯文本即结束（`core.py:529`）；轮次用尽返回
`[已达最大工具调用轮次 40，停止循环]`（`core.py:545`）。
**没有**：证据门、工具护栏、退票、耗尽后总结、目标续跑。

---

## 3. 善后与清理：先纠正一个认知

**Hermes 并不"自动删掉它生成的文件"。** 全库检索**未找到**任何"禁止把文件写进项目 cwd"
的成文条文，也没有对任务产物做磁盘清理。它的"善后"是三件别的事：

1. **结构化隔离**——中间产物一律锚定 HERMES_HOME，不进项目目录；
2. **可回滚**——文件系统快照，随时撤销；
3. **资源回收**——后台进程台账 + 定期维护长期产物。

### 3.1 状态锚定 HOME（靠结构，不靠提示词）

`hermes_constants.py:46-52`（Windows = `%LOCALAPPDATA%\hermes`）。启动强制创建：

```python
# hermes_cli/config.py:940-946
"cron", "sessions", "logs", "logs/curator", "memories",
"pairing", "hooks", "image_cache", "audio_cache", "skills",
```

其余：快照 `HERMES_HOME/checkpoints`（`tools/checkpoint_manager.py:72`）、
进程台账 `HERMES_HOME/processes.json`（`tools/process_registry.py:55`）、
图片 `HERMES_HOME/images`（`cli.py:6331`）。

**快照不外泄项目目录**：用 `GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE` 指向影子仓，
"no git state leaks into the user's project directory"（`checkpoint_manager.py:38-39`），
并排除 cwd 内 `.worktrees/`、`node_modules/`、`__pycache__/`（`DEFAULT_EXCLUDES` `:81-119`）。

> dummy 现状：日志在 `logs/`、库在 `session.db`、技能在 `skills/`——**已在项目目录内**，
> 且 `write_file` 无路径约束（仅"项目外需确认"）。这是与 Hermes 最根本的结构差异。

### 3.2 每轮收尾：`finalize_turn()`

`agent/turn_finalizer.py:30`，逐项独立 try，失败记入 `cleanup_errors`（`:170`）：
保存轨迹（`:175`）→ `_cleanup_task_resources()` 关 VM/浏览器（`:182`，
持久化环境跳过交 idle reaper）→ `_drop_trailing_empty_response_scaffolding` +
`close_interrupted_tool_sequence`（`:192-210`）→ 持久化会话（`:231`）→
触发后台记忆/技能复审（`:504`）→ `on_session_end` 插件钩子（`:519-533`）。

**会话级清理刻意不放在每轮**（`:512-517` 注释），交给 CLI（atexit / `/reset`）与网关（会话过期）。

### 3.3 后台进程：台账 + TTL + LRU + 崩溃恢复

```python
# tools/process_registry.py:59-60
FINISHED_TTL_SECONDS = 1800   # 完成态保留 30 分钟
MAX_PROCESSES = 64            # LRU 裁剪上限
```

- `kill_process`（`:1468`）按 PTY / Popen（Windows 用 `taskkill /T /F`）/ env / detached 分路终止，
  **带 PID 身份校验防误杀**（`:1521-1538`）
- `_prune_if_needed`（`:1813`）清过期 finished 与超限最旧项
- 崩溃恢复：`_write_checkpoint`（`:1848`）落 `processes.json` → 网关启动
  `recover_from_checkpoint`（`:1887`）把存活 PID 复活为 detached
- `/stop`（`hermes_cli/commands.py:97` → `cli_commands_mixin.py:228`）：
  `process_registry.kill_all()`（`:255`）+ `interrupt_all(reason="/stop")`（`:258`）
- 网关关闭时也全局 `kill_all()`（`gateway/run.py:8139`）

### 3.4 快照与回滚

```python
# hermes_cli/config.py:1317-1343
"enabled": False,            # 默认关，需 --checkpoints 或配置开启
"max_snapshots": 20,         # 注释：50 -> 20
"max_total_size_mb": 500, "max_file_size_mb": 10,
"auto_prune": True, "retention_days": 7,
"delete_orphans": True, "min_interval_hours": 24,
```

- 触发时机：每轮 `new_turn()` 重置去重（`conversation_loop.py:645`），
  首次 `write_file`/`patch` 前 `ensure_checkpoint`（`tool_executor.py:497,1182`），
  `terminal` 前同样（`:507,1190`）
- 超限：每个 ref 只留最后 20 个提交后 `git gc`（`checkpoint_manager.py:1054-1082`）；
  总量超 `max_total_size_mb` 按项目丢最旧提交（`:47-48`）
- 命令：`/rollback`（list / `<N>` / `diff <N>` / 单文件恢复）、
  `/snapshot`（create|restore|prune，prune 默认 keep=20）
- 启动自动维护 `cli.py:1824` → `maybe_auto_prune_checkpoints(retention_days=7, …)`（`:1838`）

### 3.5 长期产物：会话与技能

- **会话**：`auto_prune` 默认 **False**（历史有价值，需显式开启），
  `retention_days: 90`、`min_interval_hours: 24`、`vacuum_after_prune: True`
  （`hermes_cli/config.py:3026-3046`）；入口 `hermes_state.py:6990`
  `maybe_auto_prune_and_vacuum()`，内部 `prune_sessions(older_than_days=90)`（`:6299`）
  并**连带删磁盘 transcript**（`:7003-7005`）
- **技能 curator**（`agent/curator.py:70-73`）：

```python
DEFAULT_INTERVAL_HOURS = 24 * 7      # 7 天
DEFAULT_STALE_AFTER_DAYS = 30
DEFAULT_ARCHIVE_AFTER_DAYS = 90
```

active→stale→archived（`apply_automatic_transitions` `:306`），**归档而非删除**；
hub 安装的技能永不裁剪（`:198`），内置技能再闲置 90 天才归档。

> dummy 现状：`/sessions del <id>` 手动删；无 TTL、无自动 prune、无快照、无进程台账。
> dummy 有 `logs/` 轮转（超 5MB 归档）但仅限自身日志。

---

## 4. 历史完整性：两种修法并存，dummy 已对齐运行期方案

这条与 2026-09-14 修复的 400 报错直接相关，是本次对照中技术含量最高的一处。

### 4.1 Hermes 的**双层**策略

**运行期——补占位**（与 dummy 的 `repair_tool_pairing` 同构）：

```python
# tool_executor.py:344
"[Tool execution cancelled — {name} was skipped due to user interrupt]"
# tool_executor.py:1703
"[Tool execution skipped — {name} was not started. User sent a new message]"
```

并发在跑的工具：`f.cancel()` + `concurrent.futures.wait(not_done, timeout=3.0)`，
且**故意不 join 可能卡死的线程**（`:778-792`、`806-815`）；
结果缺失或 KeyboardInterrupt 时造 JSON 占位 `_cancelled_tool_result`（`:178-185` 等）。

**回放期——消毒历史**（dummy 没有这一层）`agent/replay_cleanup.py`：

| 函数 | 位置 | 语义 |
|---|---|---|
| `strip_interrupted_tool_tails` | `:41` | 删除被中断的 assistant→tool 块 |
| `strip_dangling_tool_call_tail` | `:119` | 删除**零回复**的悬空 `assistant(tool_calls)` 尾 |
| `strip_stale_dangerous_confirmations` | `:254` | 过期危险确认（60s，`:210`）就地脱敏 |

调用点（已核实）：`gateway/run.py:924`、`:931`；`tui_gateway/server.py:29`
（`sanitize_replay_history`）。设计意图见 `gateway/run.py:1031` 注释：
"Replay-tail sanitization lives in `agent/replay_cleanup.py` so every resume [path shares it]"。

**`strip_dangling_tool_call_tail` 的边界值得抄**：只处理"**零回复**"的尾，
"a completed assistant→tool pair (any tool answers present) is left untouched so
genuine mid-progress tool loops still resume"（`:137-141`）。
即**部分回复（2 声明 / 1 回复）不在它的处理范围**——那种情况由运行期占位覆盖。

还有一层**角色交替**兜底：`close_interrupted_tool_sequence`
（`agent/message_sanitization.py:282`），当尾巴停在裸 `tool` 消息时补一条合成
assistant 轮次（`"Operation interrupted."`），理由是 `… tool → user` 违反角色交替，
严格 provider（Gemini/Claude）会幻觉续写并忽略上下文，
"reads to the user as 'lost context'"（`:282-300` 注释，引用 issue #48879）。
调用点：`conversation_loop.py:1590,3032,4209`（各打断路径）与 `turn_finalizer.py:209-210`。

### 4.2 dummy 的做法与遗留

已实现：`session_store.repair_tool_pairing()`（纯函数、幂等、双向：**补**缺失 tool 回复
/ **删**孤儿 tool 消息 / **去重**重复回复），四层防线——退出 `_settle_history`、
resume `_repair_history`、循环内双保险丝、空提示词分支 + `/history del` 整组增删。
`load_history` 保持**忠实读取**（自愈放 Agent 边界，保证 `save→load` 无损往返）。

对照后有两点可补：

1. **dummy 无"回放期剥离"**。目前靠"补占位"覆盖全部情形，能跑通；
   但残缺的 `assistant(tool_calls)` 会永久留在历史里（模型每轮都会看到一次"结果缺失"）。
   Hermes 对"零回复"这种**确定已废弃**的尾巴选择删掉，不在上下文里留噪音。
   → 可考虑：**零回复**走删除，**部分回复**走补占位。
2. **`tool → user` 角色交替**未处理。用户那条坏记录里正是 `tool` 紧跟 `user`（seq 111→112），
   只是 DeepSeek 容忍、没报错。若日后换 Gemini/Claude 会踩同一个坑。
   → 可考虑移植 `close_interrupted_tool_sequence` 的等价逻辑。

---

## 5. dummy 可借鉴的优先级建议

按"收益 / 改动量"排序：

| 优先级 | 事项 | 参照 | 预估改动 |
|---|---|---|---|
| **P0** | 修输入静默丢弃；定义"有确认按 `/p`、无确认任意文本即打断" | 本文 §1.3 第一步 | `_drain_interrupt` + 打断处理点，~30 行 |
| **P1** | 工具护栏（相同失败 5 次 block / 同工具 8 次 halt / 无进展 5 次 block） | `tool_guardrails.py:74-79` | 新模块 + dispatch 挂钩，~120 行 |
| **P1** | 收尾前证据门 `verify-on-stop`（改动过代码且无通过证据 → 驳回一次，上限 2 次） | `verification_stop.py:245-310` | 新模块 + 循环挂钩，~100 行 |
| **P2** | 预算模型升级：`IterationBudget`（含 `refund`）+ 耗尽后"剥工具、让模型总结" | `iteration_budget.py` + `turn_finalizer.py:82-97` | ~60 行 |
| **P2** | 回放期剥离：零回复的 `assistant(tool_calls)` 尾删掉；补 `tool → user` 兜底 | `replay_cleanup.py:119` + `message_sanitization.py:282` | ~60 行 |
| **P3** | 结构化隔离：中间产物出项目目录（或至少落 `dummy_home/`） | `hermes_constants.py:46-52` | 涉及路径约定，需先定方案 |
| **P3** | 快照 / 回滚（git 影子仓） | `checkpoint_manager.py` | 独立子系统，成本高 |

> **注意 P0 与 P3 的性质不同**：P0 是修 bug + 补策略；P3 会改变 dummy 的目录约定，
> 属于架构决策，建议单独讨论（并且与你"架构分叉就开新项目"的习惯一致）。

---

## 附：引用索引（本机源码，commit c7e09f25）

```
cli.py:3736,13522,13543,13548,12754         busy 三态与队列
hermes_cli/config.py:991,1317-1343,1766,2231,3026-3046
hermes_cli/cli_commands_mixin.py:228,255,258,2580
hermes_cli/commands.py:93,95,97             /rollback /snapshot /stop
hermes_cli/goals.py:51,116-148,836,872-874,1470-1486,1487-1497,1529-1531,1547-1558
run_agent.py:2675-2725,2757-2760,2766,2791   interrupt/steer 主体
agent/conversation_loop.py:643,645,649-651,663-665,716-740,1590,3032,4209,4747,5076,5192-5198,5230-5245,5298
agent/turn_finalizer.py:30,82-97,150-157,170-231,469-471,504,512-533
agent/iteration_budget.py:5-6,21,37-59
agent/tool_guardrails.py:20-39,72-79,127-141,247-261,263-281,306-319
agent/verification_stop.py:1-6,24-39,245-310
agent/message_sanitization.py:282-300,466
agent/replay_cleanup.py:41,119-141,254
agent/tool_executor.py:178-185,344,497,507,778-792,1018,1182,1190,1685,1703
agent/prompt_builder.py:601
agent/agent_init.py:591-592
agent/agent_runtime_helpers.py:3152,3164-3177
tools/process_registry.py:55,59-60,1468,1521-1538,1792,1813,1848,1887
tools/checkpoint_manager.py:38-39,47-48,72,81-119,1054-1082
tools/interrupt.py:35,36
hermes_state.py:6299,6990,7003-7005
agent/curator.py:70-73,198,306
hermes_constants.py:46-52
gateway/run.py:924,931,1031-1038,8139
tui_gateway/server.py:29
```

dummy 侧：`core.py:120-190`（监听）`205`（轮次上限）`365-514`（主循环）
`521,545`（两条停止判据）`1214-1248`（确认与 `/p`）`session_store.py:repair_tool_pairing`。
