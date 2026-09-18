# 打断语义改进：实施方案

> 编写时间：2026-09-17
> 前置调研：`docs/agent-concurrency-interrupt-survey.md`
> 本文件性质：**可执行的实施方案**（含具体文件、行号、改法、验证方法、影响评估）
> 状态：**待评审**（未动手改代码）

---

## 一、要解决的问题

### 1.1 问题的来源（真实测试记录）

对话记录 `conversation_20260917_194943.json`：

```
[2] assistant 一次声明了 2 个工具调用：
      · terminal: ls -la
      · terminal: find . -maxdepth 2 -name "main.py" ...

[3] tool: [用户打断,工具未执行]              ← 第 1 个的结果
[4] tool: [工具结果缺失:该次调用未执行或被中断]  ← 第 2 个的结果

[5] assistant: "操作被中断。"
[6] user: "继续"
```

用户的评价是"很不对劲"。问题有三层：

**第一层（用户看不懂）**：为什么一个批次的两个调用给出了两种不同的占位文本？一个说"用户打断"，一个说"结果缺失"——它们其实是同一件事。

**第二层（模型判不了）**：模型无法从这两条文本判断"有没有副作用已经发生"。这两条都可能是"没执行"，也可能是"执行了一半"。

**第三层（这是 Codex 已确认的同类缺陷）**：GitHub `openai/codex` issue #13976 描述完全相同的场景——用户取消了一个命令，下一轮模型假设副作用已发生。用户对该问题的定性是：

> It is a **reliability/state-correctness issue** because Codex can incorrectly assume an external side effect happened even though the user canceled the action.
> （这是**可靠性/状态正确性问题**：模型会错误地假设一个外部副作用已经发生，而实际上用户取消了该动作。）

### 1.2 目标状态

打断发生后，每个未完成的工具调用都携带**明确的副作用风险信息**，模型据此判断下一步该做什么：

| 状态 | 副作用是否可能发生 | 模型应做什么 |
|---|---|---|
| 已执行完成 | 已发生（若有副作用） | 基于结果继续 |
| 执行到一半被打断 | **可能已发生** | **先验证，不要假设成功，也不要盲目重试** |
| 还没开始就被打断 | **确定没发生** | 可以安全重做 |

---

## 二、现状核查（改动前必须做的一步）

### 2.1 占位文本的全部产生点

按"改动前先找全同类入口"的要求，用 grep 查全部产生点：

```
$ grep -rn "MISSING_TOOL_REPLY\|工具结果缺失\|未执行" --include=*.py . | grep -v "^./tests/"

./core.py:519:      "content": "[用户打断,工具未执行]"})
./core.py:561:      "content": "[用户打断,工具未执行]"})
./core.py:812:      """给"已声明但未执行"的 tool_calls 补占位 tool 消息。
./core.py:816:        占位文案 "[工具结果缺失:...]"(强调的是"缺");
./core.py:818:        "[用户打断,工具未执行]"(让 LLM 知道是用户叫停,不是结果丢了)。
./core.py:832:      "content": "[用户打断,工具未执行]",
./lessons.py:40:   # 工具**未执行**的标记(用户取消 / 拒绝):既不是成功,也不是执行失败。
./lessons.py:48:   "[未执行]",
./session_store.py:23:   MISSING_TOOL_REPLY = "[工具结果缺失:该次调用未执行或被中断]"
./session_store.py:130:  "content": MISSING_TOOL_REPLY,
./tools/terminal.py:126: return "[用户取消] 命令未执行"
```

**结论：涉及改动的有 4 处理位置 + 2 处常量定义**（`lessons.py` 那几处是"判断工具是否成功"用的关键词表，不是占位文本，**要一起评估是否受影响**）。

### 2.2 逐处上下文分析

#### 位置 ①：`core.py:518`（`/p` 打断，发生在 dispatch 内部）

```python
# core.py:506-520
try:
    result = self.tools.dispatch(tool_name, tool_args)
except InterruptSignal:
    # 用户 /p 打断(发生在 handler 确认输入时)
    interrupt_triggered = True
    self.history.append({
        "role": "tool", "tool_call_id": tool_call_id,
        "content": "[用户打断,工具未执行]"})
    break
```

**事实分析**：
- 触发场景：用户在**工具确认环节**（例如 `terminal` 的"按 Enter 确认执行"、`read_file` 的"按 Enter 允许读取"）按了 `/p`
- 此时工具**还没有真正执行**（卡在确认步骤）
- 所以"未执行"这个描述**是准确的**

**目标状态**：这个调用应该标记为 **`skipped`**（确定没执行，无副作用，可安全重做）。

**但要注意一个细节**：用户在确认环节打断，可能有两种意图——"不要执行这个"或"整个任务停下"。**当前实现按前者处理**（只标记这一个调用，然后 `break` 停止后续）。方案不改这个语义。

#### 位置 ②：`core.py:560`（`/p` 打断，发生在工具**执行完之后**）—— **这是 bug**

```python
# core.py:544-562
result_preview = result[:300] + "..." if len(result) > 300 else result
ui.show(KIND_TOOL_RESULT, result_preview, tool=tool_name, indent="  ")

self._learn_from_tool_error(tool_name, result)

# ------------------------------------------------------
# 检查点 A:工具执行后,检查监听线程累积的 /p 触发
# (工具运行中用户敲 /p,执行完立即消费)
# ------------------------------------------------------
if not interrupt_triggered:
    interrupt_triggered = self._drain_interrupt()
if interrupt_triggered:
    self.history.append({
        "role": "tool", "tool_call_id": tool_call_id,
        "content": "[用户打断,工具未执行]"})     # ← 但 result 就在手边!
    break

# 正常路径(没有打断时):
self.history.append({
    "role": "tool",
    "tool_call_id": tool_call_id,
    "content": result,                         # ← 真实结果
})
```

**事实分析（这是一个实际缺陷，不是设计选择）**：

| 步骤 | 发生的动作 |
|---|---|
| `core.py:506` | `result = self.tools.dispatch(...)` — **工具已经执行完成** |
| `core.py:545` | `result_preview = ...`；`ui.show(...)` — **结果已经显示了** |
| `core.py:552` | `interrupt_triggered = self._drain_interrupt()` — 发现用户在工具**运行期间**敲了 `/p` |
| `core.py:559-561` | 补 `"[用户打断,工具未执行]"` — **但工具明明执行了，`result` 变量就在手边** |

**后果**：
1. 模型收到"未执行"的声明，**但工具实际上已经执行了**（可能已经改了文件、跑了命令）
2. 真实结果被**丢弃**（`result` 变量没被使用）
3. 用户的屏幕上**看到了**结果（`ui.show` 已打印），但模型看不到——**用户和模型的信息不一致**

**这一处是本方案最重要的修复点**：它同时是"状态错误"和"信息丢失"。

**目标状态**：应该把**真实结果**写进历史，**并附加"用户在你的工具运行期间请求中断"这个事实**。

#### 位置 ③：`core.py:832`（`_backfill_pending_tool_calls`）

```python
# core.py:812-837
def _backfill_pending_tool_calls(self, tool_calls) -> int:
    """给"已声明但未执行"的 tool_calls 补占位 tool 消息。

    与 _repair_history 的分工:
    - _repair_history:兜底保险丝,任何来源的历史破损都修,
      占位文案 "[工具结果缺失:...]"(强调的是"缺");
    - 本方法:打断场景下的**语义化**补位,文案
      "[用户打断,工具未执行]"(让 LLM 知道是用户叫停,不是结果丢了)。

    返回补入的条数。已回复过的 id 不会重复补(幂等)。
    """
    replied = {
        m.get("tool_call_id")
        for m in self.history if m.get("role") == "tool"
    }
    added = 0
    for tc in tool_calls:
        if tc.id not in replied:
            self.history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": "[用户打断,工具未执行]",
            })
            added += 1
    return added
```

**事实分析**：
- 调用时机：`break` 跳出工具循环之后（`core.py:500` 附近有调用）
- 处理的调用：**同一个批次里，排在被打断的那个之后的全部调用**（它们从未开始）
- 所以这些**全部**是"没开始"状态 → **`skipped`**（准确）

**这处的文案是准确的**，但可以更明确（说明"可以安全重做"）。

#### 位置 ④：`session_store.py:130`（`repair_tool_pairing` 的兜底）

```python
# session_store.py:23
MISSING_TOOL_REPLY = "[工具结果缺失:该次调用未执行或被中断]"

# session_store.py:124-133
# 声明了但没回复的 → 补占位(保持声明顺序,插在该批次末尾)
for tid in declared:
    if tid not in seen:
        out.append({
            "role": "tool",
            "tool_call_id": tid,
            "content": MISSING_TOOL_REPLY,
        })
        stats["backfilled"] += 1
```

**事实分析**：
- 这是**持久层的自愈兜底**，触发条件宽泛：加载历史时发现"有声明没回复"就补
- **它不知道场景**——可能是用户打断、可能是进程崩溃、可能是数据损坏
- 所以它**不能**断言"未执行"（因为可能是崩溃时正在执行）

**这正是 DeepSeek Harness 区分两种状态的场景**：

| DSH 状态 | 对应 dummy 的场景 | 副作用风险 |
|---|---|---|
| `TOOL_NOT_STARTED` | 明确知道"还没开始"（位置 ①③） | 确定无 |
| `TOOL_OUTCOME_UNKNOWN` | **不知道**（位置 ④ 的兜底） | **不确定** |

**目标状态**：位置 ④ 的文案应该表达**不确定性**（"不知道有没有执行"），而不是"未执行"。

---

## 三、方案设计

### 3.1 状态定义

定义四种状态（与调研报告第十一章一致）：

| 状态标识 | 中文表述 | 语义 | 副作用风险 |
|---|---|---|---|
| `executed` | 已执行 | 有真实结果 | 已发生（若工具有副作用） |
| `interrupted` | 执行中被中断 | 工具在运行期间被用户打断 | **可能已发生** |
| `skipped` | 未开始 | 轮到它时已被打断，从未启动 | **确定未发生** |
| `unknown` | 结果未知 | 不知道是否执行过 | **不确定** |

**与调研报告的差异说明**：报告里用的是 `cancelled`，本方案改用 `interrupted`。理由：`cancelled` 在中文语境里容易和"用户取消授权"混淆，而 `interrupted` 明确表达"执行过程中被打断"。

### 3.2 各位置的映射

| 位置 | 场景 | 现状文案 | 目标状态 |
|---|---|---|---|
| ① `core.py:518` | 确认环节被打断（工具未启动） | `[用户打断,工具未执行]` | **`skipped`** |
| ② `core.py:560` | 工具已执行完，期间收到打断 | `[用户打断,工具未执行]`（**错误**） | **`executed`** + 附加中断说明 |
| ③ `core.py:832` | 排在后面的调用（从未开始） | `[用户打断,工具未执行]` | **`skipped`** |
| ④ `session_store.py:130` | 持久层自愈兜底（场景未知） | `[工具结果缺失:该次调用未执行或被中断]` | **`unknown`** |

### 3.3 文案设计

**设计原则（沿用项目既有原则）：陈述事实，不下判决。**

即：告诉模型"发生了什么"，不告诉它"该怎么做"。让模型自己判断。

**① 与 ③（`skipped`，未开始）**：

```
[未执行] 用户打断时，这次调用还没有开始。它没有产生任何副作用。
```

**为什么这样写**：
- "没有开始"——事实
- "没有产生任何副作用"——这是**从"没有开始"推出的确定结论**，不是判断
- **不写**"可以安全重做"——那是**指令**，把判断权还给它

**② （`interrupted`，执行完成但有中断请求）**：

这一处的正确做法不是替换文案，而是**保留真实结果 + 附加说明**：

```
<真实结果原文>

[附注] 用户在你这次调用执行期间请求了中断。上方是这次调用的实际结果。
```

**为什么保留真实结果**：
- 结果已经产生（工具执行完了）
- 结果已经显示给用户了（`ui.show` 已打印）
- **丢弃它会导致用户看到的信息与模型看到的不一致**
- 而且"不浪费已完成的执行"符合项目既有取向（历史层选型时，用户明确否决了"牺牲 Ctrl+C 数据保留"的方案）

**④ （`unknown`，持久层兜底）**：

```
[结果未知] 这次调用的结果没有记录。它可能执行了，也可能没有——
无法确定是否产生了副作用。
```

**为什么这样写**：
- "结果没有记录"——事实
- "可能执行了，也可能没有"——如实表达不确定性
- "无法确定是否产生了副作用"——把**风险性质**说清（这正是 Codex issue #13976 缺失的东西）
- **不写**"请先检查"——那是指令（虽然模型大概率会这么做）

---

## 四、实施步骤

### 4.1 改动清单

| # | 文件 | 位置 | 改动内容 | 风险 |
|---|---|---|---|---|
| 1 | `session_store.py` | 第 23 行 | 新增状态常量（保留旧常量供兼容） | 低 |
| 2 | `core.py` | 第 518-520 行 | 位置 ① 文案改为 `skipped` 语义 | 低 |
| 3 | `core.py` | 第 559-562 行 | **位置 ② 改为"真实结果 + 中断附注"** | **中**（改变行为） |
| 4 | `core.py` | 第 832 行 | 位置 ③ 文案改为 `skipped` 语义 | 低 |
| 5 | `session_store.py` | 第 130 行 | 位置 ④ 文案改为 `unknown` 语义 | 低 |
| 6 | `lessons.py` | 第 48 行 | 评估关键词表是否要同步 | **需评估** |

### 4.2 逐步实施

#### 步骤 1：定义状态常量

**文件**：`session_store.py`，第 23 行附近

**现状**：

```python
MISSING_TOOL_REPLY = "[工具结果缺失:该次调用未执行或被中断]"
```

**改为**：

```python
# ============ 工具未完成时的占位文案(2026-09-17 改进) ============
# 为什么要分状态:不同状态的**副作用风险完全不同** ——
#   skipped    :工具从未启动       → 确定无副作用 → 重做安全
#   interrupted:工具执行期间被打断 → 可能已有副作用 → 需要先验证
#   unknown    :结果没有记录       → 不确定是否执行 → 无法判断
# 原来的单一文案把这三者混为一谈,模型无法判断风险。
# 背景:Codex issue #13976 记录了同类缺陷(用户取消后模型假设副作用已发生)。
TOOL_SKIPPED_REPLY = (
    "[未执行] 用户打断时，这次调用还没有开始。它没有产生任何副作用。"
)
TOOL_UNKNOWN_REPLY = (
    "[结果未知] 这次调用的结果没有记录。它可能执行了，也可能没有"
    "——无法确定是否产生了副作用。"
)
# 兼容旧引用(仍有调用点未迁移时使用)
MISSING_TOOL_REPLY = TOOL_UNKNOWN_REPLY
```

**为什么保留 `MISSING_TOOL_REPLY`**：避免一次性改动过大；先改语义，再清理引用。

#### 步骤 2：位置 ③（`_backfill_pending_tool_calls`）

**文件**：`core.py`，第 832 行

**现状**：

```python
self.history.append({
    "role": "tool",
    "tool_call_id": tc.id,
    "content": "[用户打断,工具未执行]",
})
```

**改为**：

```python
self.history.append({
    "role": "tool",
    "tool_call_id": tc.id,
    "content": TOOL_SKIPPED_REPLY,
})
```

**同时**：更新该方法的 docstring（第 812-819 行），说明新语义。

**为什么要改**：这处处理的是**同批次里排在被中断调用之后的调用**——它们从未启动，所以是 `skipped`。

#### 步骤 3：位置 ①（`core.py:518`）

**现状**：

```python
except InterruptSignal:
    # 用户 /p 打断(发生在 handler 确认输入时)
    interrupt_triggered = True
    self.history.append({
        "role": "tool", "tool_call_id": tool_call_id,
        "content": "[用户打断,工具未执行]"})
    break
```

**改为**：

```python
except InterruptSignal:
    # 用户 /p 打断(发生在 handler 确认输入时)
    # 此时工具还没开始执行 —— 用 skipped 文案(明确"无副作用")
    interrupt_triggered = True
    self.history.append({
        "role": "tool", "tool_call_id": tool_call_id,
        "content": TOOL_SKIPPED_REPLY})
    break
```

#### 步骤 4：位置 ②（`core.py:559`）—— **核心修复**

**现状**：

```python
if not interrupt_triggered:
    interrupt_triggered = self._drain_interrupt()
if interrupt_triggered:
    self.history.append({
        "role": "tool", "tool_call_id": tool_call_id,
        "content": "[用户打断,工具未执行]"})
    break
```

**改为**：

```python
if not interrupt_triggered:
    interrupt_triggered = self._drain_interrupt()
if interrupt_triggered:
    # 注意:走到这里说明工具**已经执行完成**(result 已在第 506 行取得),
    # 用户的 /p 是在工具运行**期间**敲下的。
    # 所以不能写"未执行" —— 那既与事实不符,也会丢掉已经产生的真实结果。
    # 正确做法:保留真实结果 + 附加一行事实说明。
    self.history.append({
        "role": "tool", "tool_call_id": tool_call_id,
        "content": (f"{result}\n\n"
                    "[附注] 用户在你这次调用执行期间请求了中断。"
                    "上方是这次调用的实际结果。")})
    break
```

**改动理由（逐条）**：

1. **事实正确性**：工具执行完了，说"未执行"是错的
2. **不丢数据**：`result` 真实产生了，用户也在屏幕上看到了；丢掉它会造成"用户看到、模型没看到"的信息不对称
3. **保留中断信号**：附注明确告诉模型"用户想中断"，这样它下一轮不会闷头继续
4. **符合项目既有取向**：历史层选型时用户明确否决过"牺牲已完成工具结果"的方案（见调研报告第七节引用的路线对比）

##### 4.2.1 这一步的依赖关系（审查时补充，避免误判为缺陷）

改动位置 ② 时，有两个下游机制会接手，**必须理解它们才能判断改法是否正确**：

**依赖一：`break` 之后会进入"检查点 B"，那里会落库**

```python
# core.py:562 我的改法在这里 break
break
    ↓
# core.py:578-585  检查点 B
if not interrupt_triggered:            # 已经是 True,跳过 drain
    interrupt_triggered = self._drain_interrupt()
if interrupt_triggered:                # True,进入
    prompt = input("  🧭 检测到 /p 打断,请输入提示词(Enter 取消): ").strip()
    self._backfill_pending_tool_calls(response_message.tool_calls)   # 补齐其余调用
    if prompt:
        self._append_user_turn(prompt, synthetic="user_interrupt")
        self._persist_history()        # ← 分支 1:落库
        continue
    # 提示词为空时
    self._persist_history()            # ← 分支 2:落库(第 613 行)
```

**结论**：**两条分支都会调用 `_persist_history()`**，所以在位置 ② 的 `break` 之前**不需要**再补一次落库。

**这一点在审查时被质疑过**（"你的改法跳过了正常路径的 `_persist_history()`，会不会丢数据？"）——核对源码后确认不会，因为检查点 B 兜住了。**但必须在方案里写清这个依赖**，否则后来修改代码的人（或审查者）会误判。

**依赖二：`_backfill_pending_tool_calls` 是幂等的，不会覆盖我已写入的真实结果**

```python
# core.py:820-824
replied = {
    m.get("tool_call_id")
    for m in self.history if m.get("role") == "tool"
}
added = 0
for tc in tool_calls:
    if tc.id not in replied:          # ← 已经回复过的 id 跳过
        ...
```

**结论**：位置 ② 写入真实结果后，该调用的 `tool_call_id` 已进 `replied` 集合，`_backfill_pending_tool_calls` 会**跳过它**，只给**同批次里其余未回复的调用**补占位。

##### 4.2.2 改动后的完整消息序列

用户按 `/p` 时（工具已执行完 + 用户在检查点 B 输入了提示词）：

```
[assistant]  tool_calls = [call_1, call_2]
[tool]       call_1 → "<真实结果>\n\n[附注] 用户在你这次调用执行期间请求了中断。上方是这次调用的实际结果。"
[tool]       call_2 → "[未执行] 用户打断时，这次调用还没有开始。它没有产生任何副作用。"
[user]       "用户的提示词"
```

**顺序说明**：
- 工具结果在前（`tool` 角色），用户插话在后（`user` 角色）——符合正常消息序列
- `call_1` 与 `call_2` 都在，**配对完整**（不会触发 400）
- 模型下一轮能看到：`call_1` 有真实结果 + 中断附注；`call_2` 确定未执行；然后才是用户的插话

**审查时确认过的语义问题**："真实结果 + 中断附注"和"用户提示词"同时出现是否合理？

判断是**合理的**，因为两者表达不同的事实：
- 中断附注：说明"这次调用被打断过"（历史事实）
- 用户提示词：说明"用户想让你改做什么"（新的意图）
- 代码里也明确注释了这个设计（`core.py:604-605`）：*"提示词作为用户消息注入，LLM 下一轮看到插话重新规划"*

#### 步骤 5：位置 ④（`session_store.py:130`）

**现状**：

```python
out.append({
    "role": "tool",
    "tool_call_id": tid,
    "content": MISSING_TOOL_REPLY,
})
```

**改为**：

```python
out.append({
    "role": "tool",
    "tool_call_id": tid,
    "content": TOOL_UNKNOWN_REPLY,     # 持久层不知道场景 → 表达不确定性
})
```

**为什么要用 `unknown` 而不是 `skipped`**：这一层是**自愈兜底**，触发条件宽泛（用户打断 / 进程崩溃 / 数据损坏都可能）。**它没有场景信息**，所以不能断言"没执行"。这正是 DeepSeek Harness 区分 `TOOL_NOT_STARTED` 与 `TOOL_OUTCOME_UNKNOWN` 的场景。

#### 步骤 6：评估 `lessons.py` 的关键词表

**文件**：`lessons.py`，第 40-48 行附近

**现状（已核对源码）**：

```python
# 工具**未执行**的标记(用户取消 / 拒绝):既不是成功,也不是执行失败。
# 单独一类,因为它必须同时满足两个相反的要求:
#   - 对 is_tool_error 而言:要算"非成功"(否则"取消的命令"会被记成通过证据)
#   - 对"是否真执行过"而言:要能区分出来(不附退出码,不是真失败)
NOOP_MARKERS = (
    "[用户取消]",
    "[用户拒绝]",
    "[用户拒绝将内容写到项目目录之外]",
    "[未执行]",
)
```

**核对结论（2026-09-17 实际查源码得到）**：

| 新文案前缀 | 是否已在 `NOOP_MARKERS` | 影响 |
|---|---|---|
| `[未执行]`（`TOOL_SKIPPED_REPLY` 用） | ✅ **已在**（第 48 行） | **无需改动** —— `is_tool_error` 自动识别为"非成功" |
| `[结果未知]`（`TOOL_UNKNOWN_REPLY` 用） | ❌ **不在** | **需要添加** |

**所以步骤 6 的实际工作**：只需给 `NOOP_MARKERS` 加一项：

```python
NOOP_MARKERS = (
    "[用户取消]",
    "[用户拒绝]",
    "[用户拒绝将内容写到项目目录之外]",
    "[未执行]",
    "[结果未知]",          # ← 新增:结果没有记录,不能算成功
)
```

**为什么 `[结果未知]` 要算"非成功"**：与 `[未执行]` 同样的理由——**工具结果未知时不能当作成功**，否则会污染错误率统计（`is_tool_error` 的下游是"教训生成"和"错误率统计"）。

**同时要检查的**：`lessons.py` 里是否还有别处需要同步（例如注释里列举的分类说明）。

**注意**：这一步**必须在步骤 2-5 之后**做，因为新文案要先存在，才能验证关键词表是否匹配。

### 4.3 改动顺序

```
1. 加常量(session_store.py)              ← 无行为变化
2. 改位置 ③(core.py:832)                 ← 纯文案,低风险
3. 改位置 ①(core.py:518)                 ← 纯文案,低风险
4. 改位置 ④(session_store.py:130)        ← 纯文案,低风险
5. **改位置 ②(core.py:559)**             ← 行为变化,高风险,单独一步
6. 评估 lessons.py                        ← 依赖前面
7. 跑全部测试
8. 真实场景验证
```

**为什么把位置 ② 放在最后**：它是唯一改变行为的一处（从"丢弃结果"变成"保留结果"），**单独一步便于出问题时定位**。

---

## 五、影响评估

### 5.1 受影响的既有机制

| 机制 | 位置 | 是否受影响 | 说明 |
|---|---|---|---|
| `repair_tool_pairing` | `session_store.py` | ⚠️ **受影响** | 它靠常量生成占位；常量值变了，但**结构不变**（仍是 `role=tool` + 正确 `tool_call_id`），所以配对逻辑**不受影响** |
| `close_interrupted_tool_sequence` | `session_store.py` | ❌ 不受影响 | 它只检查消息角色序列，不看内容 |
| `_settle_history` | `main.py` | ❌ 不受影响 | 调用链不变 |
| `_repair_history` | `core.py` | ❌ 不受影响 | 同上 |
| `format_usage_line` / 压缩器 | `core.py` / `compressor.py` | ⚠️ **可能受轻微影响** | 新文案比旧文案长（约 +30 字符/条），**会略微增加 token 占用**。压缩阈值不受影响 |
| `is_tool_error` | `lessons.py` | ⚠️ **需评估** | 见 4.2 步骤 6 |
| 护栏 `tool_guardrails` | `tool_guardrails.py` | ❌ 不受影响 | 它比对"结果指纹"，文案变了但**同一场景下文案一致**，不影响指纹判重 |

### 5.2 token 成本估算

```
旧文案 (位置①③): "[用户打断,工具未执行]"              = 12 字符
新文案 (位置①③): "[未执行] 用户打断时，这次调用还没有开始。它没有产生任何副作用。" = 34 字符
                                                        差值: +22 字符/条

旧文案 (位置④):   "[工具结果缺失:该次调用未执行或被中断]" = 21 字符
新文案 (位置④):   "[结果未知] 这次调用的结果没有记录。它可能执行了，也可能没有——无法确定是否产生了副作用。" = 49 字符
                                                        差值: +28 字符/条

位置②: 从 12 字符变成"真实结果 + 附注"(结果本身可能几百字符)
       —— 这是**增加**的,但那本来就应该在历史里(只是之前被丢弃了)
```

**量级评估**：一次打断通常影响 1-3 个调用，总增量约 50-90 字符 ≈ **30-60 token**。**相对一次任务动辄几万 token 的规模，可忽略。**

### 5.3 需要更新或新增的测试

**现有测试**（要检查是否会失败）：

| 测试文件 | 可能受影响的断言 |
|---|---|
| `tests/test_history_hygiene.py` | 可能断言了 `MISSING_TOOL_REPLY` 的具体文本 |
| `tests/test_persistence.py` | 可能断言了占位内容 |
| `tests/test_lessons.py` | 关键词表相关 |

**新增测试**（建议）：

```python
def test_interrupted_tool_keeps_real_result():
    """工具执行期间被打断 → 真实结果必须保留,不能被占位替换。

    这修复的是 core.py:559 的缺陷:工具已执行完成(result 已取得),
    但因为检测到中断请求,就用"[用户打断,工具未执行]"覆盖了真实结果。
    """
    # 构造:工具执行成功 → 期间设置 interrupt 标志 → 检查历史
    # 断言:历史里的 tool 消息 content 包含真实结果 + 中断附注

def test_skipped_calls_marked_as_no_side_effect():
    """同批次里排在被中断调用之后的 → 标记为 skipped(无副作用)。"""

def test_repair_uses_unknown_not_skipped():
    """持久层自愈兜底必须用 unknown(它不知道场景)。"""
    # 断言 MISSING_TOOL_REPLY / TOOL_UNKNOWN_REPLY 含"结果未知"
    # 且**不**含"没有产生任何副作用"(那是 skipped 才有的断言)

def test_unknown_marker_counts_as_not_success():
    """[结果未知] 必须被 is_tool_error 判为非成功。"""
```

**第 3 条测试的用意**：防止将来有人把持久层的兜底文案错误地改成 `skipped` 版本——那会**错误地断言"无副作用"**。

**审查时追加的测试要求**（针对 4.2.1 的两个依赖）：

```python
def test_interrupted_real_result_is_persisted():
    """被中断但已执行完的调用,其结果必须落库。

    审查时发现的关注点:位置 ② 的改法在 break 之前 append,
    看起来"跳过了"正常路径的 self._persist_history()。
    核对确认:break 会进入检查点 B,那里两条分支都会落库 ——
    但**必须有测试实证这一点**,否则将来重构检查点 B 时可能静默破坏。
    """
    # 构造:工具执行成功 + 中断标志已设 + 检查点 B 走"提示词为空"分支
    # 断言:磁盘上的历史(不是内存)包含真实结果

def test_backfill_does_not_overwrite_real_result():
    """_backfill_pending_tool_calls 必须跳过已回复的调用,不覆盖真实结果。

    依赖:_backfill_pending_tool_calls 用 `replied` 集合做幂等判断
    (core.py:820-824)。如果这个幂等性被破坏,它会用占位文本覆盖
    位置 ② 刚写入的真实结果 —— 那等于把 bug 换个地方重现。
    """
    # 构造:同一批次 2 个调用,第 1 个已有真实结果
    # 调用 _backfill_pending_tool_calls
    # 断言:第 1 个的 content 不变,第 2 个被补占位

def test_full_message_sequence_after_interrupt():
    """打断后的完整消息序列必须配对完整且顺序正确。

    期望序列(见方案 4.2.2):
      [assistant] tool_calls=[call_1, call_2]
      [tool] call_1 → 真实结果 + 中断附注
      [tool] call_2 → skipped 文案
      [user] 用户提示词
    断言:call_1 和 call_2 都有对应的 tool 消息(不会 400),
         且 order 为 assistant → tool → tool → user。
    """
```

### 5.4 风险清单

| 风险 | 可能性 | 后果 | 缓解 |
|---|---|---|---|
| 位置 ② 改动后，历史里出现"真实结果 + 附注"，压缩器可能切分异常 | 低 | 中 | 跑压缩相关测试 |
| `[结果未知]` 前缀未被 `lessons.py` 识别 → 判成成功 | 中 | **中**（污染统计） | 步骤 6 + 新增测试 |
| 用户看到的历史与模型看到的不一致 | **位置 ② 修复后会消除这个不一致** | — | 这是收益不是风险 |
| 新文案增加 token | 确定 | 可忽略 | 已估算（30-60 token/次） |

---

## 六、验证方法

### 6.1 单元测试层面

```bash
pytest tests/ -q
```

**期望**：全部通过（改完后）。**如果 `test_history_hygiene.py` 或 `test_persistence.py` 失败**，说明它们断言了旧文案，需要同步更新（这是预期内的，不是回归）。

### 6.2 定点验证（针对位置 ②）

**这一处必须单独验证**，因为它是唯一的行为变化。

**验证脚本设计**（伪代码）：

```python
# 1. 构造一个假 LLM,让它在第一次调用时声明 2 个工具
# 2. 让第 1 个工具正常执行(返回一个可识别的结果,如 "REAL_RESULT_XYZ")
# 3. 在工具执行期间设置 interrupt 标志(模拟用户在工具运行中敲 /p)
# 4. 检查历史:
#    - 第 1 个 tool 消息的 content 应包含 "REAL_RESULT_XYZ"(真实结果保留)
#    - 且包含 "[附注]" 字样(中断说明)
#    - 第 2 个 tool 消息应为 skipped 文案
```

**判定标准**：
- ✅ 真实结果保留
- ✅ 中断附注存在
- ✅ 第 2 个调用标记为 skipped

### 6.3 真实场景验证

**复现原始问题场景**：

```
用户: "帮我给现在这个项目写一个启动器，要求通过 windows terminal 运行 main.py"
（模型声明多个工具调用时）
用户: 按 /p 打断
```

**观察**：
1. 模型下一轮是否**知道**"哪些执行了、哪些没有"？
2. 模型是否**不再假设**未执行的调用已经完成？
3. 用户看到的与模型看到的是否一致？

**这一步是最终的判定依据**——因为本方案的目标是"改善模型对中断状态的判断"，只有真实对话能验证。

---

## 七、不在本方案范围内的事项

**明确排除**（避免范围蔓延）：

| 事项 | 为什么不做 |
|---|---|
| 工具并发执行 | 调研报告建议先做打断语义；并发会引入的状态组合与本方案强耦合（见调研报告 12.1） |
| 结构化字段（`_effect` / `_batch`） | 需要改消息结构 + `strip_meta`，风险高于收益；**等本方案验证后再评估** |
| 批次上下文告知模型"这批共几个" | 属于第二阶段的改进（调研报告 12.3 第二步） |
| `finish_reason` 截断检查 | 与本方案无关的独立风险项（调研报告 12.4），**单独评估** |
| 打断后"接着做"（而不是重来） | 语义复杂（要定义"续做"的表达方式）；先确保状态表达正确 |

---

## 八、后续可选项（本方案验证后再评估）

如果本方案有效（模型能正确理解中断状态），可以继续做：

**阶段二：批次上下文**

在 `turn_context` 的情况说明里加入批次信息（复用现有的 `RunRecord` 结构）：

```
[本轮运行情况]
工具批次 #2（共 3 个调用，因用户打断而结束）：
  [1/3] terminal: ls -la          → 已执行
  [2/3] terminal: find ...        → 被打断（可能已部分执行）
  [3/3] read_file: main.py        → 未开始
```

**阶段三：结构化字段**

给工具结果消息加 `_effect` / `_batch` 字段。**前置条件**：确认 `strip_meta` 能正确处理新字段（`core.py:441` 附近有相关逻辑，注释说明 DeepSeek 对未知顶层字段严格）。

**阶段四：并发**

前置条件是**打断语义已经做清楚**——因为并发会让"哪些在执行中"变成复数，状态组合数量级增长。

---

## 九、审查记录（2026-09-17）

> 本节记录方案写完后的自查过程。**保留它是为了让后续修改代码的人知道哪些地方已经核过、哪些依赖必须维持。**

### 9.1 审查发现并已澄清的问题

**问题一：位置 ② 的改法是否跳过了 `_persist_history()`？**

**提出**：位置 ② 的改法在 `break` 之前 `append`，而正常路径是在 `append` 之后调用 `self._persist_history()`（`core.py:566`）。改后走 `break` 分支，看起来跳过了落库。

**核对过程**：

```
core.py:562  位置 ② 的 break
    ↓
core.py:578  检查点 B 入口
core.py:582  if not interrupt_triggered:  → 已为 True,跳过
core.py:584  if interrupt_triggered:      → 进入
core.py:585  prompt = input(...)          → 问用户要提示词
core.py:597  _backfill_pending_tool_calls(...)
core.py:606  self._persist_history()      → 【分支 1:落库】
core.py:613  self._persist_history()      → 【分支 2:落库】
```

**结论**：**两条分支都会落库**，所以改法不会丢数据。

**处理**：把这条依赖写进方案 4.2.1，并追加测试 `test_interrupted_real_result_is_persisted` 实证它。**理由**：这个依赖是隐式的（靠"break 恰好会进入检查点 B"），将来若重构检查点 B，可能静默破坏。

**问题二：`_backfill_pending_tool_calls` 会不会覆盖位置 ② 刚写入的真实结果？**

**提出**：位置 ② 写入真实结果后，检查点 B 会调 `_backfill_pending_tool_calls(response_message.tool_calls)`——它遍历的是**同一个批次的所有调用**，包括刚写入真实结果的那个。

**核对过程**：

```python
# core.py:820-827
replied = {
    m.get("tool_call_id")
    for m in self.history if m.get("role") == "tool"
}
added = 0
for tc in tool_calls:
    if tc.id not in replied:      # ← 幂等判断
        ...补占位...
```

**结论**：它会**跳过**已回复的 `tool_call_id`，所以不会覆盖。

**处理**：写进方案 4.2.1，并追加测试 `test_backfill_does_not_overwrite_real_result`。**理由**：这个幂等性是隐式契约——破坏它会让位置 ② 的修复失效（bug 换个地方重现）。

**问题三："真实结果 + 中断附注"与"用户提示词"同时出现是否合理？**

**提出**：改动后，模型下一轮会同时看到"工具结果 + 中断附注"和"用户提示词"（检查点 B 注入的），这可能造成信息重复或冲突。

**核对过程**：两者表达的是不同事实——

| 内容 | 表达的事实 |
|---|---|
| 中断附注 | "这次调用期间用户请求过中断"（**历史事实**） |
| 用户提示词 | "用户想让你改做什么"（**新的意图**） |

代码里也有对应注释（`core.py:604-605`）：*"提示词作为用户消息注入，LLM 下一轮看到插话重新规划"*。

**结论**：两者不冲突，**顺序也正确**（工具结果在前、用户话在后）。

**处理**：写进方案 4.2.2 的"完整消息序列"，并追加测试 `test_full_message_sequence_after_interrupt`。

### 9.2 审查确认过的行号

| 引用 | 内容 | 核对结果 |
|---|---|---|
| `core.py:518-520` | 位置 ① 的占位 | ✅ 准确 |
| `core.py:559-562` | 位置 ② 的占位 | ✅ 准确 |
| `core.py:832` | 位置 ③ 的占位 | ✅ 准确 |
| `session_store.py:130` | 位置 ④ 的占位 | ✅ 准确 |
| `session_store.py:23` | `MISSING_TOOL_REPLY` 定义 | ✅ 准确 |
| `lessons.py:44-48` | `NOOP_MARKERS`（已含 `[未执行]`） | ✅ 准确 |
| `core.py:506` | `result = self.tools.dispatch(...)` | ✅ 准确 |
| `core.py:545` | `result_preview` + `ui.show` | ✅ 准确 |
| `core.py:566` | 正常路径的 `_persist_history()` | ✅ 准确 |
| `core.py:578-585` | 检查点 B 入口 | ✅ 准确 |
| `core.py:597` | `_backfill_pending_tool_calls` 调用 | ✅ 准确 |
| `core.py:606` / `:613` | 检查点 B 的两处落库 | ✅ 准确 |
| `core.py:820-824` | `replied` 幂等集合 | ✅ 准确 |
| `core.py:604-605` | 提示词注入的注释 | ✅ 准确 |

### 9.3 审查后仍未确认的事项

| 事项 | 状态 |
|---|---|
| `TOOL_UNKNOWN_REPLY` 的长度是否影响压缩器的切分边界 | **未验证**——需要在实施后跑压缩相关测试确认 |
| 新文案的 token 增量在真实场景中的实际影响 | **仅估算**（30-60 token/次），未实测 |
| 用户看到的历史与模型看到的历史在改动后是否完全一致 | **理论上消除**了不一致（位置 ② 保留真实结果），但**需真实场景验证** |
| `lessons.py` 是否还有其他地方依赖占位文本的具体内容 | **只查了 `NOOP_MARKERS`**，未全文件复查 |

### 9.4 本次审查的性质说明

**这不是独立审查**——是方案作者的自查。它能发现"实现细节上的依赖关系"这一类问题，但**不能替代第三方审查**，尤其不能替代对"这个方案是否值得做"的判断。

**已知的局限**：
- 我核对的是**当前代码**的逻辑，没有测试实际运行（未改代码）
- 三个"审查发现"都是**依赖关系问题**，没有发现方案方向性的错误
- 第九节的结论建立在"源码阅读正确"这一前提上——如果读写有误，结论也会错
