# 工具并发与打断机制：主流 Agent 横向调研

> 调研时间：2026-09-17
> 调研对象：Claude Code、Codex CLI、Hermes、DeepSeek Harness、OpenHands、OpenCode、OpenClaw、Mistral Vibe（含 WorkBuddy / Trae 的公开信息说明）
> 目的：为 dummy 的"工具并发执行"与"中途打断"两项能力提供设计依据
> 写作要求：不缩写、不省略逻辑与推导过程；术语首次出现时给出定义；每一个结论都标注来源

---

## 写在前面：为什么调研这两件事

dummy 在一次真实测试中出现了一个现象：

```
用户提问："帮我给现在这个项目写一个启动器"
模型第一次回复：同时声明了 2 个工具调用
    · terminal: ls -la
    · terminal: find . -maxdepth 2 -name "main.py" ...
用户按下 /p 打断
结果：
    第 1 个工具 → [用户打断,工具未执行]
    第 2 个工具 → [工具结果缺失:该次调用未执行或被中断]
模型下一轮说："操作被中断。"
用户看到这个过程的感受是："很不对劲"
```

这个现象暴露出两个问题，它们分别对应本报告的两个主题：

**问题一（并发）**：模型在一个回复里声明了多个工具调用。dummy 的代码是"遍历所有 tool_calls 依次执行"（`core.py:486` 的 `for tool_call in response_message.tool_calls:`）。这意味着 dummy **允许**并发声明，但**串行执行**。那么：

- 主流 Agent 是允许还是禁止一个回复里出现多个工具调用？
- 如果允许，它们是并发执行还是串行执行？
- 用什么规则决定某个调用能否并发？

**问题二（打断）**：用户按 `/p` 打断时，已经执行完的部分丢了、未执行的部分只留下一条占位文本，而且模型在下一轮**不知道**"这批一共几个调用、执行到第几个、为什么停下"。这引出：

- 主流 Agent 在打断时如何处理"一批执行中的工具"？
- 打断后，模型能拿到哪些信息？
- 有没有系统记录"未完成批次"的完整状态？

本报告回答这些问题，并在此基础上给出 dummy 可借鉴的方案。

---

## 一、先统一术语

不同系统对同一概念用词不同，先定义本报告使用的术语，避免后续歧义。

### 1.1 层级术语

| 术语 | 定义 | 备注 |
|---|---|---|
| **回合（Turn）** | 用户一次输入所引发的一整轮 Agent 工作：从用户消息进入，到模型不再请求工具、给出最终回答为止 | DeepSeek Harness 用的是 "Turn"，Codex 也用 "turn" |
| **步骤（Step）** | 一次模型请求，加上这次请求中模型声明的全部工具调用及其执行 | DeepSeek Harness 用的是 "Step" |
| **批次（Batch）** | 一个步骤里，模型在**同一条 assistant 消息**中声明的全部工具调用 | 本报告自定术语，便于统一描述 |

**层级关系**：一个回合包含一个或多个步骤；一个步骤包含零个或一个批次；一个批次包含一个或多个工具调用。

**为什么必须区分**：dummy 现在把"步骤"和"批次"混为一谈（代码里就是 `for turn in range(max_tool_turns)` 加内层 `for tool_call in response_message.tool_calls`），而本报告要讨论的"并发"与"打断"都发生在**批次**这一层，不是步骤层。

### 1.2 并发术语

| 术语 | 定义 |
|---|---|
| **串行执行（Sequential）** | 批次内的工具调用按顺序一个一个执行，后一个在前一个完成后才开始 |
| **并发执行（Concurrent）** | 批次内的多个工具调用同时开始执行 |
| **并发安全（Concurrency-safe）** | 某个工具调用具备"可以与其他调用同时执行而不产生错误"的性质。注意这是**单次调用**的性质，不是工具类型的性质——同一个工具，不同参数，并发安全性可能不同 |
| **准入规则（Admission rule）** | 系统用来判断"一个批次能否并发执行"的规则 |

### 1.3 打断术语

| 术语 | 定义 |
|---|---|
| **打断（Interrupt）** | 用户在 Agent 工作过程中要求它停止当前动作 |
| **中止信号（Abort signal）** | 系统内部用来传播"应当停止"这一意图的机制 |
| **不打断型工具（Non-interruptible / Block tool）** | 不支持被打断的工具（例如正在写一个文件，中断会导致文件半截） |
| **中断后状态** | 打断发生时，每个工具调用处于什么状态：已完成、执行中、未开始 |

---

## 二、总体发现（先给结论，后面逐个系统展开）

### 发现一：**全部系统都允许"一个回复里多个工具调用"**

没有任何一个调研对象在**协议层**禁止模型一次声明多个工具调用。这是模型 API 本身的能力（OpenAI 的 `tool_calls` 是数组，Anthropic 的 `tool_use` content block 也可以在一个 assistant 消息里出现多个）。

区别在于**执行层怎么处理**：有的并发，有的串行，有的按规则分区。

### 发现二：**"并发安全"是单次调用的性质，不是工具类型的性质**

这是 Claude Code 的设计核心，原文：

> Concurrency is not a global property of a tool. It is a property of a specific tool invocation with specific inputs.
> （并发不是工具的全局性质，而是"特定工具 + 特定输入"这一具体调用的性质。）
> —— claude-code-from-source.com 第 7 章

它举的例子：

```
Bash("ls -la")        → 可以并发（只读）
Bash("rm -rf build/") → 不可以并发（会改文件）
```

**同一个工具（Bash），因为参数不同，并发分类不同。** 这解释了为什么"按工具名判断能否并发"是不够的。

### 发现三：**准入规则普遍采用"fail-closed"（失败即保守）**

多个系统的做法一致：**判断不出来就当"不安全"**。

Claude Code 的实现（原文伪代码）：

```javascript
const safe = input?.success
  ? tryCatch(() => def.isParallelSafe(input.data), false)   // 抛异常 → false
  : false;                                                   // 解析失败 → false
```

也就是说三种情况都归为"不并发"：
1. 参数解析失败 → 不并发
2. 工具未声明 `isParallelSafe` → 不并发
3. `isParallelSafe` 内部抛异常 → 不并发

**这个选择的理由**：并发执行出错（两个写操作互相覆盖）的代价，远高于"本来能并发却串行执行"的性能损失。

### 发现四：**打断的粒度差异极大——从"整批丢弃"到"逐调用注入"**

| 系统 | 打断行为 |
|---|---|
| Claude Code | AbortController 层级：全局 → 兄弟级 → 单工具级；被取消的工具返回 `user_interrupted` 合成错误 |
| Codex | 按 turn 中止；**已知缺陷**：被取消的工具调用在下一轮对模型不可见（见 GitHub issue #13976） |
| Hermes | `_interrupt_requested` 标志位在**多个检查点**被读取；另有 `/steer` 机制在**单个工具之间**注入用户文本 |
| DeepSeek Harness | 不提供"半个批次续跑"；恢复时给未完成项打标记 `TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN` |
| OpenClaw | 无 harness 级并发上限；打断处理未在公开材料中详述 |

**特别注意 Codex 的 issue**——它正好是 dummy 遇到的同类问题，而且是个**已被用户报为缺陷**的问题。

### 发现五：**"未完成批次"需要显式标记状态，否则模型会误判副作用已发生**

Codex 的 issue #13976 原文描述：

> Codex attempted to create a PR with `gh pr create`
> I canceled the command
> On the next turn, Codex behaved as if the PR might already exist
> （Codex 尝试用 gh pr create 创建 PR；我取消了命令；下一轮 Codex 表现得好像 PR 可能已经存在了）

用户自己的定性：

> That makes this more than a UX paper cut. It is a **reliability/state-correctness issue** because Codex can incorrectly assume an external side effect happened even though the user canceled the action.
> （这不只是体验问题，是**可靠性/状态正确性问题**：Codex 会错误地假设一个外部副作用已经发生，而实际上用户取消了该动作。）

**这正是 dummy 面临的同一个问题的另一个实例**——而 DeepSeek Harness 的解法是显式的状态标记（见 5.4 节）。

---

## 三、Claude Code：按调用分区的并发 + 三层中止控制器

### 3.1 并发：两阶段设计

**阶段一：批处理编排（Batch orchestration）**

模型回复**完整接收后**，把工具调用分区成"可并发组"和"串行单元"。

入口函数：`partitionToolCalls()`（位于 `toolOrchestration.ts`）。

算法（论文给的伪代码，逐行说明）：

```javascript
function groupBySafety(calls, registry) {
  return calls.reduce((groups, call) => {
    // 第 1 步：按工具名查定义
    const def = registry.lookup(call.name);
    // 第 2 步：用该工具自己的 schema 解析参数
    const input = def?.schema.safeParse(call.input);
    // 第 3 步：fail-closed —— 解析失败、未声明、抛异常都算"不安全"
    const safe = input?.success
      ? tryCatch(() => def.isParallelSafe(input.data), false)
      : false;
    // 第 4 步：与上一组合并，或新开一组
    if (safe && groups.at(-1)?.parallel) {
      groups.at(-1).calls.push(call);        // 合并进上一个并发组
    } else {
      groups.push({ parallel: safe, calls: [call] });  // 新开一组
    }
    return groups;
  }, []);
}
```

**算法性质**：
- **贪心**：遇到可并发的调用就尽量并入当前组
- **保序**：不改变调用顺序，只在连续可并发的调用之间合并
- **不可并发的调用会切断连续段**

**具体例子**（论文给的）：

```
模型请求：[Read, Read, Grep, Edit, Read]

步骤 1: Read  → 可并发 → 新组 {并发, [Read]}
步骤 2: Read  → 可并发 → 并入 {并发, [Read, Read]}
步骤 3: Grep  → 可并发 → 并入 {并发, [Read, Read, Grep]}
步骤 4: Edit  → 不可并发 → 新组 {串行, [Edit]}
步骤 5: Read  → 可并发 → 新组 {并发, [Read]}

结果：3 组
  组 1: [Read, Read, Grep]  → 并发执行
  组 2: [Edit]              → 单独执行
  组 3: [Read]              → 并发执行（虽然只有一个）
```

**推论（重要）**：模型写工具调用的**顺序会影响性能**。如果它在两个 Read 之间插了一个 Write，就会从 2 组变成 3 组。论文提到"实际上模型倾向于把读操作聚在一起"，也就是说这个算法是**按常见情况优化**的。

**阶段二：推测执行（Speculative execution）**

在模型**还在流式输出回复时**就开始执行工具，在回复结束前就拿到部分结果。对应组件是 `StreamingToolExecutor`。

**为什么这对 Agent 特别有效**（论文原话）：

> Any time your agent loop involves a "think, then act" cycle where the thinking phase produces multiple independent actions, you can overlap the tail of thinking with the beginning of acting. The savings are proportional to the ratio of think-time to act-time. For language model agents, where think-time (API response generation) dominates, the savings are substantial.
> （只要 Agent 循环是"先想后做"，且"想"的阶段会产出多个独立动作，就可以让"想的尾部"与"做的开头"重叠。节省量与"思考时间/动作时间"之比成正比。对语言模型 Agent 来说，思考时间（即 API 生成回复的时间）占主导，所以节省是可观的。）

### 3.2 并发上限

默认 **10**，可用环境变量 `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` 配置。

论文对这个数字的评价：

> Ten is generous — you rarely see more than five or six tool calls in a single model response. The limit exists as a safety valve for pathological cases, not as a typical constraint.
> （10 很宽裕——单条模型回复里很少超过五六个工具调用。这个上限是给病态情况兜底的安全阀，不是常规约束。）

### 3.3 结果顺序：缓冲后按请求顺序释放

论文特别强调这一点：

> **Preserve submission order in results.** Yielding results in completion order is tempting — it minimizes latency to first result. But if the consumer (in this case, the language model) expects results in a specific order, reordering them creates confusion that costs more time to resolve than the latency savings. Buffer completed results and release them in the order they were requested. The implementation cost is a simple array walk; the correctness benefit is absolute.
> （**保持结果的提交顺序。** 按完成顺序产出结果很诱人——它能把"第一个结果的延迟"降到最低。但如果消费方（这里是语言模型）期望特定顺序，打乱顺序造成的困扰，解决起来花的时间比省下的延迟更多。所以：缓冲已完成的结果，按请求顺序释放。实现成本只是一次数组遍历；正确性收益是绝对的。）

**这条对 dummy 直接适用**：dummy 的 `session_store` 要求 `tool_calls` 与 `tool` 消息严格配对且顺序对应，所以**即使将来做并发，也必须按声明顺序写回历史**。

### 3.4 打断：三层 AbortController

**层级结构**（论文与第 7 章）：

```
查询级控制器（query controller）
    └── 兄弟级控制器（sibling controller）
            └── 每个工具一个子控制器（per-tool controller）
```

**各层作用**：
- 查询级：整个查询（回合）
- 兄弟级：**同一个批次内的所有工具**——任一工具出错时触发，立即终止其他在飞的子进程
- 单工具级：单个工具

"兄弟级控制器"的设计意图（论文原话）：

> **Sibling abort controller.** Fires when any Bash tool errors, immediately terminating other in-flight subprocesses rather than letting them run to completion.
> （**兄弟中止控制器。** 当任一 Bash 工具出错时触发，立即终止其他正在执行的子进程，而不是让它们跑完。）

**为什么 Bash 出错要连坐**（论文解释）：

> Bash errors cascade to siblings because shell commands often form implicit pipelines. Read and search errors are isolated because they are independent operations.
> （Bash 的错误会连累兄弟调用，因为 shell 命令之间常常构成隐式管道。而读和搜索的错误是隔离的，因为它们是互相独立的操作。）

**打断时的行为**（第 7 章）：

> When the user does interrupt and all tools are cancellable, the abort controller fires with reason `'interrupt'`. The executor's `getAbortReason()` method checks each tool's interrupt behavior individually — a `'cancel'` tool gets a synthetic `user_interrupted` error, while a `'block'` tool (which would not be present in a fully interruptible set, but the code handles the edge case) continues running.
> （用户打断且所有工具都可取消时，中止控制器以原因 `'interrupt'` 触发。执行器的 `getAbortReason()` 会**逐个检查**每个工具的中断行为——标记为 `'cancel'` 的工具收到一个合成的 `user_interrupted` 错误；标记为 `'block'` 的工具（在完全可中断的集合里本不该出现，但代码处理了这个边界情况）**继续运行**。）

**UI 层的保守设计**：

> The UI only shows an "interruptible" indicator when ALL executing tools support cancellation. If even one tool is `'block'`, the entire set is treated as non-interruptible. This is conservative but correct: you cannot meaningfully interrupt a batch where one tool would keep running anyway.
> （只有在**所有**执行中的工具都支持取消时，UI 才显示"可打断"指示。哪怕只有一个工具是 `'block'`，整个集合就被当作不可打断。这是保守但正确的：一个批次里如果有一个工具无论如何都会继续跑，你就没法有意义地打断它。）

### 3.5 其他相关事实

- **工具安全声明是逐工具的**：`FileReadTool.isConcurrencySafe = () => true`、`FileWriteTool.isConcurrencySafe = () => false`（会改文件）、`BashTool.isConcurrencySafe = (input) => !inputMutatesFiles(input)`、`AgentTool.isConcurrencySafe = () => false`、`MCPTool.isConcurrencySafe = () => true`
- **另一种批量工具**：Claude Code 还提供一个 `BatchTool`——"在一个请求里跑多个工具调用，能并行就并行，否则串行"，返回全部结果
- **Ctrl+C 的传播**：通过 AbortController 层级传播给所有在飞的 Agent。**例外是后台任务**——带 `backgroundTaskId` 的任务会在中止信号下存活，需要用 `TaskStopTool` 显式杀死。原话结论：*"In a multi-agent setup, interrupting the parent doesn't kill backgrounded children."*（在多 Agent 场景里，打断父级不会杀死后台子任务。）
- **后台任务的终止需求**先记录，与 dummy 无关（dummy 没有后台任务），但值得知道有这个设计维度

---

## 四、Codex CLI：FuturesOrdered + 逐工具并行标志

### 4.1 循环结构

Codex 的循环用 **Rust 实现，基于 Tokio 的异步状态机**。

论文原文：

> Codex's loop is implemented in Rust as a Tokio-based async state machine. A Session struct orchestrates turns via streaming ResponseItem events from the OpenAI Responses API (deserialized from SSE or WebSocket stream frames), with tool invocations processed through **FuturesOrdered** for ordered parallel execution, now factored through a dedicated **ToolCallRuntime**.
> （Codex 的循环用 Rust 实现，是一个基于 Tokio 的异步状态机。Session 结构体通过 OpenAI Responses API 的流式 ResponseItem 事件编排回合（从 SSE 或 WebSocket 流帧反序列化），工具调用通过 **FuturesOrdered** 处理以实现**有序并行执行**，现在这部分被抽取到专门的 **ToolCallRuntime** 里。）

**关键点**：`FuturesOrdered` 是 Rust 异步生态里的一个组合器，语义是"并发执行、按插入顺序产出结果"。也就是说 **Codex 的并发是"结果保序的并发"**——和 Claude Code 的"缓冲后按序释放"是同一个设计目标，只是实现层面不同。

**入口点**（论文列出，保留至今）：
- `Codex::spawn()` — 创建新会话
- `submit_with_id()` — 提交操作
- `next_event()` — 非阻塞读取流式响应事件

### 4.2 并发准入：`supports_parallel` 标志

论文的对比表（Table）：

> **Tool dispatch**: FuturesOrdered through a dedicated ToolCallRuntime; per-tool `supports_parallel` flag (default **false**) gates an execution lock—concurrency-unsafe tools run exclusively.
> （**工具分发**：通过专门的 ToolCallRuntime 使用 FuturesOrdered；逐工具的 `supports_parallel` 标志（**默认 false**）控制一把执行锁——并发不安全的工具独占执行。）

**与 Claude Code 的对比**：

| | Claude Code | Codex |
|---|---|---|
| 判断粒度 | **每次调用**（检查参数） | **每个工具**（布尔标志） |
| 判断方式 | 工具实现的 `isConcurrencySafe(input)` 方法 | 工具声明的 `supports_parallel` 字段 |
| 默认值 | false（不安全） | false（不安全） |
| 执行机制 | 分区成批，每批并发 | 执行锁——不安全的工具拿锁独占 |

**推论**：Codex 的粒度比 Claude Code 粗。Claude Code 能区分 `Bash("ls")` 和 `Bash("rm -rf")`，而 Codex 的 `supports_parallel` 是工具级的布尔值——**除非 Codex 在别处对 bash 做了额外判断**，否则它只能做到"整个 bash 工具并行或不并行"。

**注意**：这一点我未能从公开材料中确认 Codex 是否对 bash 做了参数级判断。**如实标注：不确定。**

### 4.3 打断：turn 级中止 + 已知缺陷

**中止机制**：Codex 支持 "abort turns when budget exhausted"（预算耗尽时中止回合）——来自 2026 年的版本更新说明：

> track usage across agent threads, provide remaining-budget reminders, and abort turns when exhausted
> （跨 Agent 线程跟踪用量，提供剩余预算提醒，并在耗尽时中止回合。）

**已知缺陷（重要）**：GitHub issue #13976，标题是 *"Codex should treat canceled or denied tool calls as first-class context"*（Codex 应当把被取消或被拒绝的工具调用当作一等上下文）。

**问题描述（原文摘要）**：

> If a user cancels a command or rejects a tool call, the model should be able to see that event on the next turn, including that the attempted action did not complete successfully. It should not assume side effects happened unless it later verifies them.
> （如果用户取消了一个命令或拒绝了一次工具调用，模型应当能在下一轮看到这个事件，**包括"所尝试的动作没有成功完成"这一信息**。除非它后来验证过，否则不应当假设副作用已经发生。）

**用户的真实案例**：

```
Codex 尝试执行 gh pr create 创建 PR
用户取消了该命令
下一轮，Codex 表现得好像 PR 可能已经存在
用户不得不手动纠正方向
```

**用户的诉求（值得 dummy 直接借鉴）**：

> It would also help if the UI optionally let the user provide a short reason when rejecting or canceling something, and passed that reason through to Codex as structured feedback. That would let the model pivot directly to the right resolution instead of wasting a turn recovering context.
> （如果 UI 能让用户在拒绝或取消时**可选地提供一个简短理由**，并把该理由作为**结构化反馈**传给 Codex，那会很有帮助。这样模型就能直接转向正确的解决方案，而不是浪费一轮来恢复上下文。）

**这条诉求的两个要点**：
1. 取消/拒绝应当作为**一等上下文**进入下一轮
2. 用户给的理由应当**结构化传递**（不是塞进某段文本里）

### 4.4 其他相关事实

- Codex 的系统提示里明确鼓励并行：*"Parallelize tool calls whenever possible—especially file reads."*（尽量并行工具调用——尤其是文件读取。）
- Codex 的权限模型分 4 层：ExecPolicy（Starlark 规则）、生命周期 hooks、Guardian LLM 审批员（fail-closed）、OS 级沙箱
- Codex 的 turn 状态通过 HTTP header `x-codex-turn-state` 传递（这是协议层细节，与并发无关，但说明它的回合是有显式状态的）

---

## 五、Hermes：同步 Python 循环 + 线程管理的并发 + 多处打断检查点

> 本节材料来自**本机 Hermes 源码**（`C:\Users\xlinz\AppData\Local\hermes\hermes-agent`），因此比其他系统的材料更精确。

### 5.1 并发：判定函数 `_should_parallelize_tool_batch`

位置：`agent/tool_dispatch_helpers.py:105`

**函数签名与前置检查**：

```python
def _should_parallelize_tool_batch(tool_calls) -> bool:
    """Return True when a tool-call batch is safe to run concurrently."""
    if len(tool_calls) <= 1:
        return False                                    # ① 单个调用 → 串行
    tool_names = [tc.function.name for tc in tool_calls]
    if any(name in _NEVER_PARALLEL_TOOLS for name in tool_names):
        return False                                    # ② 含"永不并行"工具 → 串行
    ...
```

**① 单个调用直接串行**：没有并发的意义，也避免走并发路径的开销。

**② 黑名单 `_NEVER_PARALLEL_TOOLS`**：

```python
# Tools that must never run concurrently (interactive / user-facing).
# When any of these appear in a batch, we fall back to sequential execution.
_NEVER_PARALLEL_TOOLS = frozenset({"clarify"})
```

**当前只有 `clarify` 一个**——理由是它是**交互式、面向用户**的工具（要弹问题给用户，并发会冲突）。注释写得很清楚：只要批次里出现任何一个这类工具，**整个批次**退回串行。

**逐调用检查（循环体）**：

```python
reserved_paths: list[Path] = []
for tool_call in tool_calls:
    tool_name = tool_call.function.name
    try:
        function_args = json.loads(tool_call.function.arguments)
    except Exception:
        logging.debug("Could not parse args for %s — defaulting to sequential; raw=%s", ...)
        return False                                # ③ 参数解析失败 → 串行
    if not isinstance(function_args, dict):
        logging.debug("Non-dict args for %s (%s) — defaulting to sequential", ...)
        return False                                # ④ 参数不是对象 → 串行

    if tool_name in _PATH_SCOPED_TOOLS:
        scoped_path = _extract_parallel_scope_path(tool_name, function_args)
        if scoped_path is None:
            return False                            # ⑤ 路径类工具但取不到路径 → 串行
        if any(_paths_overlap(scoped_path, existing) for existing in reserved_paths):
            return False                            # ⑥ 路径与已保留的冲突 → 串行
        reserved_paths.append(scoped_path)
        continue

    if tool_name not in _PARALLEL_SAFE_TOOLS:
        # Check if it's an MCP tool from a server that opted into parallel calls.
        if not _is_mcp_tool_parallel_safe(tool_name):
            return False                            # ⑦ 不在白名单且不是并行安全的 MCP → 串行
```

**③④ 同样是 fail-closed**：参数解析失败、参数不是字典，都判串行——和 Claude Code 的做法一致。

**⑤⑥ 路径冲突检测**：这是 Hermes 相对 Claude Code 的一个额外机制。它维护一个 `reserved_paths` 列表，逐个检查"这次调用的目标路径"是否与"已经保留的路径"重叠。重叠则整个批次串行。

**⑦ 白名单机制**：

```python
# Read-only tools with no shared mutable session state.
_PARALLEL_SAFE_TOOLS = frozenset({
    "ha_get_state", "ha_list_entities", "ha_list_services",
    "read_file", "search_files", "session_search",
    "skill_view", "skills_list", "vision_analyze",
    "web_extract", "web_search",
})
```

**白名单的含义**：只有**只读、且不共享可变会话状态**的工具能并行。注意最后一个条件——"不共享可变会话状态"排除了那些虽然只读但会写日志/改全局状态的工具。

**路径类工具集合**：

```python
# File tools can run concurrently when they target independent paths.
_PATH_SCOPED_TOOLS = frozenset({"read_file", "write_file", "patch"})
```

**注意**：`write_file` 和 `patch` **在路径不重叠时可以并发**——这比 Claude Code 保守（Claude Code 的 `FileWriteTool.isConcurrencySafe` 恒为 false）。但因为有路径冲突检测，两个写不同文件的调用可以并行。

**额外的破坏性命令检测**（用于 terminal 工具的参数判断）：

```python
_DESTRUCTIVE_PATTERNS = re.compile(
    r"""(?:^|\s|&&|\|\||;|`)(?:
        rm\s|rmdir\s|
        cp\s|install\s|
        mv\s|
        sed\s+-i|
        truncate\s|
        dd\s|
        shred\s|
        git\s+(?:reset|clean|checkout)\s
    )""",
    re.VERBOSE,
)
# Output redirects that overwrite files (> but not >>)
_REDIRECT_OVERWRITE = re.compile(r'[^>]>[^>]|^>[^>]')

def _is_destructive_command(cmd: str) -> bool:
    """Heuristic: does this terminal command look like it modifies/deletes files?"""
    if not cmd:
        return False
    if _DESTRUCTIVE_PATTERNS.search(cmd):
        return True
    if _REDIRECT_OVERWRITE.search(cmd):
        return True
    return False
```

**这是一个启发式检测**（正则匹配危险命令模式），设计意图写在模块文档里：

> `_is_destructive_command` — terminal-command heuristic used to gate parallel batch dispatch.
> （terminal 命令的启发式检测，用于控制并行批次分发。）

**输出重定向的判定**：`>` 会覆盖文件（危险），`>>` 是追加（也危险但模式不同）——正则是 `[^>]>[^>]|^>[^>]`，即"前面不是 `>`、后面不是 `>` 的单个 `>`"，以及"开头的单个 `>`"。这样 `>>` 不会被匹配（因为第二个字符是 `>`，不满足 `[^>]`）。

### 5.2 并发实现：线程池

论文的定性描述：

> Hermes's loop a peer to Claude Code's at the runtime level but shape-different in implementation, **a synchronous Python loop with thread-managed concurrency** rather than an async generator that yields.
> （Hermes 的循环在运行时层面与 Claude Code 同级，但实现形态不同——**同步 Python 循环 + 线程管理的并发**，而不是产出结果的异步生成器。）

**执行入口**（本机源码）：

```python
def execute_tool_calls_concurrent(agent, assistant_message, messages: list,
                                  effective_task_id: str, api_call_count: int = 0) -> None:
    """Execute multiple tool calls concurrently using a thread pool.

    Results are collected in the original tool-call order and appended to
    messages so the API sees them in the expected sequence.
    （用线程池并发执行多个工具调用。结果按**原始调用顺序**收集并追加到 messages，
    这样 API 看到的是期望的序列。）
    """
```

**模块头部导入**：`import concurrent.futures`

**注意文档里的这句话**——"Results are collected in the original tool-call order"（结果按原始调用顺序收集）。这**第三次**印证了"并发执行但保序写回"这个设计原则（前两次是 Claude Code 的缓冲释放和 Codex 的 FuturesOrdered）。

**并发工作线程数**：源码里有相关注释与常量（`# Maximum number of concurrent worker threads for parallel tool execution.`），我未能定位到确切数值。**如实标注：未确认具体上限。**

### 5.3 打断：标志位 + 多个检查点

**核心机制**：一个布尔标志 `agent._interrupt_requested`，在多个位置被读取。

**检查点位置**（`agent/tool_executor.py` 行号）：

| 行号 | 上下文 |
|---|---|
| 339 | **执行前预检**（`# ── Pre-flight: interrupt check ──`） |
| 579 | 并发路径中 |
| 778 | 执行循环中 |
| 851 | 收集结果时 |
| 1030 | 串行路径中 |

**执行前预检的具体行为**（源码）：

```python
# ── Pre-flight: interrupt check ──────────────────────────────────
if agent._interrupt_requested:
    print(f"{agent.log_prefix}⚡ Interrupt: skipping {num_tools} tool call(s)")
    for tc in tool_calls:
        messages.append(make_tool_result_message(
            tc.function.name,
            f"[Tool execution cancelled — {tc.function.name} was skipped due to user interrupt]",
            tc.id,
            effect_disposition="none",
        ))
        _flush_session_db_after_tool_progress(
            agent, messages,
            stage=f"cancelled tool result {tc.function.name}",
        )
    return
```

**这段代码有几个值得注意的设计**：

1. **每个被跳过的调用都有一条对应的 tool 结果消息**（`make_tool_result_message(..., tc.id, ...)`）——保证 `tool_calls` 与 `tool` 的配对完整，不会因为跳过而产生孤儿调用
2. **结果文本明确说明"被跳过"**：`[Tool execution cancelled — {name} was skipped due to user interrupt]`
3. **带 `effect_disposition="none"` 参数**——这是一个结构化字段，声明"这次调用没有产生副作用"。**这正是 Codex issue #13976 所要求的东西**
4. **逐条落库**（`_flush_session_db_after_tool_progress`），不是最后一次性写——这样即使进程在此刻崩溃，已写入的部分也在磁盘上

**`effect_disposition` 字段的意义**：它把"这个工具调用有没有产生副作用"作为**结构化数据**记录下来，而不是让模型从文本里猜。取值至少有 `"none"`（无副作用）。

### 5.4 `/steer` 机制：在工具之间注入用户输入

这是 Hermes 特有的机制，设计意图在注释里写得很清楚（`tool_executor.py:1002-1018`）：

```python
# ── Per-tool /steer drain ───────────────────────────────────────
# result so the steer lands as early as possible.
agent._apply_pending_steer_to_tool_results(messages, 1)
...
# ── /steer injection ──────────────────────────────────────────────
# Append any pending user steer text to the last tool result so the
# so the steer marker is never truncated. See steer() for details.
    agent._apply_pending_steer_to_tool_results(messages, num_tools)
```

以及 `:1681-1682`：

```python
# ── Per-tool /steer drain ─────────────────────────────────────
# Drain pending steer BETWEEN individual tool calls so the
```

**机制解释**：用户输入的"引导文本"（steer）不会等到整个回合结束，而是在**单个工具调用之间**被注入——具体做法是把它**追加到上一个工具结果文本的末尾**。

**为什么这样做**（从注释推断的设计意图）：
1. 工具结果本身就是模型下一轮一定会看到的内容，附在那里**保证可见**
2. 在**工具之间**注入（而不是回合结束后），让用户的话能**尽早**影响模型接下来的决策
3. 借用"工具结果"这个载体，不需要**新增消息类型**（避免破坏消息序列的协议结构）

**与 dummy 的对比**：dummy 的 `/p` 打断是"中断当前工具 → 追加一条合成的 user 消息"。Hermes 的 `/steer` 是"不中断，把话附在工具结果上"。**这是两种不同的交互模型**（见第七节的分析）。

---

## 六、DeepSeek Harness：显式状态标记 + 不续跑半个批次

> 材料来源：一篇两万字的技术解析文章（Russell，2026）。**注意：这不是官方文档，是第三方深度分析。** 其中引用的机制名称（`TOOL_NOT_STARTED`、`TOOL_OUTCOME_UNKNOWN`）来自作者对系统行为的描述。**标记为"第三方分析"，未经官方文档核对。**

### 6.1 基础结构：Turn / Step / Session Log

**两层循环**：
- **Turn（回合）**：用户一次输入引发的一整轮工作
- **Step（步骤）**：一次模型请求 + 它触发的工具执行

**一个 Turn 的完整流程**（原文给的流程图）：

```
用户消息进入 Inbox
    ↓
写入 turn/start
    ↓
组装本 Step 的 System Prompt 和工具列表
    ↓
agent/pre-step 检查、补充或拒绝输入
    ↓
从 Session Log 推导模型历史
    ↓
请求模型，流式接收回答
    ↓
模型输出 read_file 工具调用
    ↓
工具通过权限、沙箱和 Hook 管线
    ↓
执行读取，把结果写入 Session Log
    ↓
模型根据文件内容发起下一 Step
    ↓
模型输出 write_file，执行并记录结果
    ↓
没有后续工具或待处理输入，写入 turn/end
```

### 6.2 唯一的强约束：Model-visible means logged

原文引用架构文档：

> **Model-visible means logged**（模型看到的内容必须被记录）

这个约束的含义：凡是进入模型上下文的内容，都必须能在 Session Log 里找到对应记录。**推论**：如果一条信息没有落日志，它就不该出现在模型请求里。

**与 dummy 的对比**：dummy 没有这条约束——例如 `turn_context` 生成的情况说明会进历史，但它**不是**从持久化层读出来的（是运行时构造的）。这在 dummy 的场景下没问题（因为 dummy 的历史会整体落库），但**没有显式保证**。

### 6.3 两类事件：持久事实 vs 运行时扩展点

原文的区分（这个区分很重要）：

| 类别 | 例子 | 性质 |
|---|---|---|
| **Session Event** | `turn/start`、`tool/call`、`tool/result` | **已经发生的事实**，进入日志，重启后保留 |
| **Cordis Event** | `agent/pre-step`、`agent/request`、`tools/pre-execute` | **运行中的扩展点**，插件可观察/改写/阻止，本身不等于持久记录 |

原文的比喻：

> 可以把 Session Event 看成行车记录仪，把 Cordis Event 看成路口的信号灯和交警。前者记录车辆经过了哪里，后者在车辆行驶时改变它接下来怎么走。
> （Session Event 是行车记录仪，Cordis Event 是信号灯和交警。前者记录车辆经过了哪里，后者在车辆行驶时改变它接下来怎么走。）

**部分 Cordis Event 使用 Waterfall 调度**：每个监听器收到一个 `next()`，调用它表示把控制权交给下一层，不调用则可以中止或替换后续结果。

### 6.4 崩溃恢复：显式状态标记（**本节是重点**）

原文（第 298-302 行）：

> ### 崩溃恢复也有明确边界
>
> 如果程序在工具调用中间崩溃，持久化层会在恢复时补充状态标记。**模型请求已经产生、工具却没开始时**，系统标记 `TOOL_NOT_STARTED`；**工具调用已经记录但没有结果时**，系统标记 `TOOL_OUTCOME_UNKNOWN`，提醒模型不要盲目重复可能产生副作用的操作。
>
> 当前实现**不会从半个 Turn 的中间无缝续跑**。恢复逻辑会先为未闭合的调用、Step 和 Turn 补上结束记录，再从新回合继续。这是一个值得在文章里说清楚的限制。

**这段给出的两个状态**：

| 状态 | 触发条件 | 语义 |
|---|---|---|
| `TOOL_NOT_STARTED` | 模型请求已产生，但工具**还没开始**执行 | 这个调用肯定没有副作用——可以安全重试 |
| `TOOL_OUTCOME_UNKNOWN` | 工具调用**已记录但没有结果** | 副作用可能已发生——**不要盲目重复** |

**这是本报告最有价值的一个设计**：

它把"未完成的工具调用"分成了**两种**，而不是一种。区别在于**副作用是否可能已经发生**：

```
NOT_STARTED       → 确定没发生 → 重试安全
OUTCOME_UNKNOWN   → 可能发生了 → 重试危险,模型需要先验证
```

**对照 Codex 的 issue #13976**：Codex 的问题正是"没有这个区分"——用户在 `gh pr create` 执行中取消，Codex 下一轮"表现得好像 PR 可能已经存在"。而 DSH 会标记 `TOOL_OUTCOME_UNKNOWN`，**明确告诉模型不要假设**。

**另一个重要事实**：DSH **不支持从半个 Turn 中间续跑**。它的恢复策略是"先把未闭合的记录补完整，然后开新回合"。这是**明确记录的限制**，不是缺陷——作者专门标注为"值得说清楚的限制"。

### 6.5 并发：只读可并行

原文（第 392 行，关于子 Agent/Workflow 的场景）：

> 独立的只读调用可以并行，有先后依赖的操作使用 `await` 顺序执行。中间工具结果留在程序内部，只有模型主动 `return` 或 `console.log()` 的内容回到对话上下文。

原文（第 507-511 行，关于 Workflow 脚本）：

> `parallel()`：并行启动一组任务，再在同一个屏障处等待；
> ...
> Workflow 脚本本身不能访问文件、网络、定时器或 Node API。它只负责排班，真正读文件和查资料的是子 Agent；**并发数和 Agent 总数也受配置上限控制**。

**DSH 的并发特点**：
- 有并行原语（`parallel()`）
- **受配置上限控制**（并发数 + Agent 总数）
- 区分"只读可并行"与"有依赖须串行"

**注意**：这两处描述的是**子 Agent/Workflow 层面**的并发，不是"一个批次内多个工具调用"层面的并发。**DSH 在主循环里是否并发执行同一批次的多个工具调用，我没有找到明确材料。如实标注：不确定。**

---

## 七、开源同类补位：OpenHands、OpenCode、OpenClaw、Mistral Vibe

> 说明：WorkBuddy（腾讯）与 Trae（字节）的并发/打断机制**未公开**，无法调研（见第八节）。为了让对比不出现空缺，本节补充三个**有开源源码**的同类系统，它们的机制已被同一篇源码级论文分析。

### 7.1 OpenHands：资源锁管理的并行

**循环结构**（论文原文）：

> Each `step()` makes one LLM call but executes *all* tool calls of the response as an action batch—optionally in parallel through a `ParallelToolExecutor` governed by a `tool_concurrency_limit` and a **resource-lock manager keyed on each tool's declared resources** (files, terminal session, browser), so **only same-resource calls serialize**.
> （每个 `step()` 做一次 LLM 调用，但把该响应的**所有**工具调用作为一个动作批次执行——可选并行，通过 `ParallelToolExecutor`，它由 `tool_concurrency_limit` 和一个**资源锁管理器**控制；资源锁以每个工具**声明的资源**为键（文件、终端会话、浏览器），因此**只有同资源的调用会串行化**。）

**机制解释（逐步）**：
1. 每个工具**声明**自己需要哪些资源（例如 `read_file` 声明"文件：/path/a.py"，`terminal` 声明"终端会话"）
2. 执行前，系统按资源加锁
3. 两个调用**只要资源不同就能并行**；资源相同则串行

**与 Claude Code / Hermes 的对比**：

| | 判断维度 | 粒度 |
|---|---|---|
| Claude Code | 布尔值（`isConcurrencySafe`） | 调用级，但结论只有"能/不能" |
| Hermes | 路径重叠检测 + 白名单 | 调用级，路径类工具做真正的位置比较 |
| **OpenHands** | **资源声明 + 锁** | **多资源维度**（文件、终端、浏览器） |

**OpenHands 的粒度最细**：它能表达"这两个调用都碰文件但碰的不是同一个文件"（可并行），也能表达"这两个调用都用终端"（串行）——而 Claude Code 的布尔分区做不到前者（一旦判定"不安全"就完全串行）。

**StuckDetector（卡死检测）**——论文提到它经过了重构但保留：

> The StuckDetector survives the rearchitecture intact: the same five failure scenarios as in the V0 codebase (**repeating action-observation pairs, repeating actions with errors, monologue loops, alternating patterns, and context-window error loops**), now with configurable thresholds and enabled by default.
> （StuckDetector 在重构中完整保留：与 V0 代码库相同的五种失败场景（**重复的动作-观察对、重复的带错动作、独白循环、交替模式、上下文窗口错误循环**），现在阈值可配置，且默认启用。）

**为什么在这里提 StuckDetector**：它和"打断"是同一个问题的两面——**用户打断是外部中止，卡死检测是内部中止**。dummy 已有类似机制（`tool_guardrails.py` 的重复检测），但只覆盖了"重复的动作-观察对"一种，OpenHands 列了五种。

**其他相关事实**：
- OpenHands 支持"把对手的 harness 当后端"（ACP），即可以把 Claude Code、Codex 或 Gemini CLI 当作可互换的后端跑
- 并行子会话：OpenHands 的 `task` 和 `delegate` 工具会把子任务作为**独立会话在多个线程里并发运行**

### 7.2 OpenCode：默认并行 + 真路径键的写队列

**工具执行**（论文原文）：

> And tool calls execute in parallel by default via `Promise.all`, with a **realpath-keyed per-file mutation queue** serializing edits and a distinctive ***truncation-poisoning guard***: when the assistant message was cut off at the length limit, ***all* of its tool calls are failed unexecuted**, because salvage-parsed streaming arguments can validate while being silently incomplete.
> （工具调用**默认通过 `Promise.all` 并行执行**，配一个**以 realpath 为键的按文件变更队列**来串行化编辑操作；还有一个独特的**截断中毒防护**：当 assistant 消息在长度上限处被截断时，它的**全部**工具调用都以"未执行"失败，因为从被截断的流里抢救解析出来的参数可能通过校验、但实际上是静默不完整的。）

**三个值得注意的点**：

**① 默认并行**（而不是默认串行）——与 Claude Code / Codex / Hermes 的 fail-closed 哲学相反。它的保护靠：
- **realpath 为键的写队列**：注意是 `realpath`（解析符号链接后的真实路径），所以 `/a/b.py` 和 `/a/../a/b.py` 会命中同一个键——比字符串比较更正确
- 队列只串行化**写**操作，读仍然并行

**② 截断中毒防护（Truncation-poisoning guard）**——这个机制 dummy **完全没有**，值得单独说明：

```
场景：模型生成的 assistant 消息因为 max_tokens 用尽被截断
     截断位置恰好在一个 tool_call 的参数 JSON 中间
问题：如果从残缺的流里"抢救"出参数，它可能恰好是合法 JSON
     （例如 {"path": "a.py"} 后面还有内容没传完，但前半段已经能解析）
结果：工具会用一个"看起来完整但其实不完整"的参数执行 → 静默错误

OpenCode 的解法：检测到截断 → 这一批工具调用全部标记为"未执行失败"
               不尝试抢救任何一个
```

**对 dummy 的直接价值**：dummy 的 `_parse_tool_arguments`（`core.py:1288` 附近）有**三层容错**（`json.loads` → `json5.loads` → 正则提取），**而且它就是"抢救式解析"**。如果 `max_tokens` 在工具调用参数中间耗尽，第三层正则可能提取出一个合法但不完整的结果。**这是一个 dummy 尚未覆盖的风险**。如实记录，供后续评估。

**③ 默认无并发上限**（论文原文）：

> Parallel tool calls execute concurrently with **no harness cap**, and retry is harness-level with retry-after-aware exponential backoff; context overflow is never retried—it flips to compaction.
> （并行工具调用并发执行，**没有 harness 级上限**；重试是 harness 级的，带"retry-after 感知"的指数退避；上下文溢出**从不重试**——它切换为压缩。）

### 7.3 OpenClaw：把并发开关交给模型

**调度器**（论文原文）：

> The scheduler executes tool calls in parallel and, since v0.45, partitions by concurrency safety: file-mutating tools (`edit`, `write_file`) and `update_topic` are **hard-forced to sequential execution**, and—**uniquely in the corpus**—**the model itself is given a concurrency knob**, an auto-injected `wait_for_previous` boolean on every tool schema through which it can serialize any call.
> （调度器并行执行工具调用；自 v0.45 起按并发安全性分区：改文件的工具（`edit`、`write_file`）和 `update_topic` **被硬性强制串行执行**；而且——**在语料库中是唯一的**——**模型自己拿到了一个并发旋钮**：每个工具 schema 上自动注入一个 `wait_for_previous` 布尔参数，模型可以通过它把任意调用串行化。）

**这是本报告里唯一一个"把并发决定权交给模型"的设计**：

```
其他系统：harness 判断并发安全性（代码决定）
OpenClaw：工具 schema 上注入 wait_for_previous 参数
          → 模型可以在调用里写 wait_for_previous: true
          → 表示"这个调用要等前面的完成"
```

**为什么值得注意**：它把"这两个操作有依赖关系"这个**语义判断**交给模型（模型知道自己在做什么），而 harness 只提供**表达手段**（那个布尔参数）和**兜底规则**（改文件的工具硬性串行）。

**这正好对应 dummy 在讨论的原则**：*语义判断交给模型，代码只提供机制*。OpenClaw 的 `wait_for_previous` 是一个具体的实现范例。

### 7.4 Mistral Vibe：用户取消作为内联检查（非中间件）

论文原文：

> Six middlewares ship out of the box: `TurnLimitMiddleware`, `PriceLimitMiddleware` (cost cap per session), `TokenLimitMiddleware` (session-total token cap, added in the 2.9 line), `AutoCompactMiddleware` (triggers summarization at a token threshold), `ContextWarningMiddleware` (warns when the conversation approaches the context-window limit), and `ReadOnlyAgentMiddleware` (gates write tools for read-only agent profiles); **user-cancellation is handled inline via an `is_user_cancellation_event()` check rather than as a middleware**.
> （随附六个中间件：回合数上限、价格上限（每会话成本封顶）、token 上限（会话累计 token 封顶，2.9 版加入）、自动压缩（在 token 阈值处触发摘要）、上下文警告（对话接近上下文窗口上限时警告）、只读 Agent（为只读 Agent 配置拦截写工具）；**用户取消是通过 `is_user_cancellation_event()` 内联检查处理的，不是中间件**。）

**值得注意的点**：作者**明确选择了不用中间件处理取消**。论文没有解释原因，但从架构上推断：中间件是"每轮迭代前走一遍检查栈"的机制，而取消需要**更细的粒度**（可能在一次工具执行中间发生），所以放在内联路径上。

**这印证了一个设计选择**：**中断检查的位置需要比"每轮"更细**——Hermes 也是这么做的（5 个检查点，见 5.3）。

### 7.5 并发机制总表

| 系统 | 默认 | 判断粒度 | 判断依据 | 并发上限 | 结果顺序 |
|---|---|---|---|---|---|
| **Claude Code** | 保守（默认不安全） | **调用级** | 工具实现的 `isConcurrencySafe(input)` | 10（可配置） | 缓冲后按请求顺序释放 |
| **Codex** | 保守（默认不安全） | **工具级** | `supports_parallel` 布尔字段（默认 false） | 未公开 | `FuturesOrdered`（有序并行） |
| **Hermes** | 保守（默认不安全） | **调用级** | 白名单 + 路径重叠检测 + 破坏性命令正则 | 未确认具体值 | 按原始调用顺序收集 |
| **OpenHands** | 保守 | **资源级** | 工具声明的资源（文件/终端/浏览器）+ 锁 | `tool_concurrency_limit` | 未在材料中说明 |
| **OpenCode** | **并行（默认开）** | **文件路径级** | realpath 为键的写队列（只串行化写） | 无上限 | 未在材料中说明 |
| **OpenClaw** | 并行 | 混合 | 改文件工具硬性串行 + **模型可通过 `wait_for_previous` 自行串行化** | 未在材料中说明 | 未在材料中说明 |
| **Mistral Vibe** | 未在材料中说明 | 未在材料中说明 | 未在材料中说明 | 未在材料中说明 | 未在材料中说明 |
| **DeepSeek Harness** | 未在材料中说明（主循环层） | 只读可并行 | 材料描述的是子 Agent/Workflow 层 | 受配置上限（并发数 + Agent 总数） | 未在材料中说明 |
| **dummy（现状）** | **串行** | 无（不存在判断） | 无 | 无 | 顺序执行，天然保序 |

**从这张表能读出的规律**：

1. **七成系统选择"保守默认 + 显式声明可并发"**（fail-closed）。只有 OpenCode 和 OpenClaw 默认并行。
2. **判断粒度从粗到细的排序**：工具级（Codex）< 调用级（Claude Code / Hermes）< 资源级（OpenHands）< 路径级（OpenCode）。
3. **所有明确说明结果顺序的系统，都选择"保序"**（Claude Code 缓冲、Codex FuturesOrdered、Hermes 按原始顺序收集）。**没有例外。**
4. **并发上限普遍宽松或不存在**：Claude Code 10、OpenHands 有配置项、OpenCode 明确说"无上限"。这说明**并发不是性能瓶颈点**——真正的理由是"避免冲突"，不是"限制资源"。
5. **Mistral Vibe 和 DeepSeek Harness 在"批次内工具并发"这一层没有明确材料**——如实标注，不臆测。


---

## 八、WorkBuddy 与 Trae：公开信息的边界

> **本节的存在本身就是结论**：这两个系统**无法调研到并发/打断机制**。把"查不到"如实写出来，比编造或含糊带过更有价值。

### 8.1 WorkBuddy（腾讯）

**能确认的事实**（来自多家媒体报道，非官方架构文档）：

| 项 | 内容 |
|---|---|
| 产品定位 | AI 原生桌面智能体工作台，被称为"腾讯版 OpenClaw"或"腾讯版小龙虾" |
| 发布时间 | 2026 年 3 月 |
| 核心能力 | 直接操作本地电脑文件，自主规划并执行多步骤复杂任务 |
| 数据 | 2026 年 Q2 PC 端月访问量 2097 万，国内办公智能体平台第一 |
| 技术路线 | "多模型调度 + **Agent 并行**"（来自一篇产品对比文章的表述） |
| 开源情况 | **非开源** |
| 相关产品 | CodeBuddy（编程，2025 年起）、WorkBuddy Enterprise（企业版，2026 年 6 月）、QClaw（内部"龙虾产品"） |

**关于"Agent 并行"这个词**：媒体里出现了"多模型调度 + Agent 并行"的提法，但：
- 这是**产品宣传层面的描述**，不是技术文档
- "Agent 并行"指**多个 Agent 并行**还是"一个 Agent 内多个工具调用并行"，**无法从公开材料中区分**
- 没有任何公开材料说明它的**并发准入规则**、**打断行为**、**中断后状态处理**

**结论**：WorkBuddy 在"工具并发与打断"这个题目上**没有可用的公开技术信息**。任何具体描述都会是猜测。

### 8.2 Trae（字节跳动）

**能确认的事实**：

| 项 | 内容 |
|---|---|
| 产品定位 | 基于 VS Code 的 AI IDE（不是 CLI Agent） |
| 发布时间 | 2025 年初 |
| 模式 | IDE 模式 + SOLO 模式（AI 主导开发流程） |
| 分发 | 通过字节新加坡子公司 SPRING(SG)PTE.LTD |
| 开源情况 | **非开源** |
| 相关产品 | Kimi CLI（Moonshot）、Qwen Code（阿里，Gemini CLI 的分支）、Trae Agent |

**关于 Trae 的公开信息**：主要是产品功能与隐私争议（有安全公司分析其数据收集），**没有关于工具调用并发或打断的公开技术文档**。

论文里提到 "ByteDance's Trae Agent" 是 2026 年出现的中文厂商 harness 之一，但**只到"存在"这一层**，没有机制细节。

**结论**：同 WorkBuddy——无法调研。

### 8.3 为什么"查不到"是可接受的结果

**闭源产品在架构层面的可调研性，取决于厂商是否发布架构文档。** 本次调研中：

| 系统 | 可调研程度 | 原因 |
|---|---|---|
| Claude Code | 高 | 有第三方源码分析（含专门的并发章节）+ 学术论文 |
| Codex | 中高 | 有源码级论文分析 + 公开 issue 讨论具体行为 |
| Hermes | **最高** | **开源，本机就有源码** |
| DeepSeek Harness | 中 | 有第三方万字技术解析（但非官方文档） |
| OpenHands / OpenCode / OpenClaw | 中高 | 开源 + 论文分析 |
| **WorkBuddy** | **无** | 闭源，且无架构文档 |
| **Trae** | **无** | 闭源 IDE，无架构文档 |

**这个分布本身有信息量**：**闭源的消费级产品，其内部并发/打断机制是不透明的**。对 dummy 而言，这意味着——**参考对象应该集中在有源码可查的系统上**，而 Claude Code / Hermes / OpenHands 是三个最好的范本（分别代表"调用级布尔分区"、"白名单 + 路径检测"、"资源锁"三种流派）。

---

## 九、打断机制横向对比

### 9.1 各家打断机制总表

| 系统 | 触发方式 | 作用范围 | 已在执行的工具 | 未开始的工具 | 模型下一轮能看到什么 |
|---|---|---|---|---|---|
| **Claude Code** | Ctrl+C | AbortController 三级层级 | 收到合成的 `user_interrupted` 错误（可取消的工具）；`'block'` 工具继续跑 | 未开始 | 每个被取消的工具都有一条错误结果 |
| **Codex** | 用户取消命令/拒绝调用 | turn 级中止 | 未在材料中说明 | 未在材料中说明 | **已知缺陷**：取消事件对模型不可见，模型可能误判副作用已发生（issue #13976） |
| **Hermes** | `_interrupt_requested` 标志 | 5 个检查点 | 检查点处响应 | 每个都补一条 tool 结果，文本为 `[Tool execution cancelled — X was skipped due to user interrupt]`，**带结构化字段 `effect_disposition="none"`** | 完整的工具结果序列（含"已跳过"标记） |
| **DeepSeek Harness** | 未在材料中说明 | 崩溃恢复场景 | 标记 `TOOL_OUTCOME_UNKNOWN` | 标记 `TOOL_NOT_STARTED` | 显式的状态标记，**提醒模型不要盲目重复** |
| **Hermes `/steer`** | 用户输入引导文本 | 逐工具（工具之间） | 不打断 | 不打断 | 引导文本附在**上一个工具结果末尾** |
| **Mistral Vibe** | `is_user_cancellation_event()` 内联检查 | 内联路径 | 未在材料中说明 | 未在材料中说明 | 未在材料中说明 |
| **OpenCode** | 未在材料中说明 | 未在材料中说明 | 未在材料中说明 | 未在材料中说明 | 未在材料中说明 |
| **dummy（现状）** | `/p` + Ctrl+C | 当前工具 | 丢弃（不记录结果）；剩余补占位文本 | 补占位文本 | 只能看到占位文本，**不知道批次总量、执行到第几个、为什么停** |

### 9.2 三个设计维度

把上表拆解成三个维度，可以看清各家的差异在哪：

**维度一：打断的"作用范围"**

```
整批丢弃（Codex 的显式行为、dummy 的现状）
    ↓
逐工具判断（Claude Code：可取消的取消、block 的继续）
    ↓
不打断，只注入（Hermes /steer）
```

**维度二：每个"未完成调用"的状态精度**

```
无状态（dummy 现状：只有一句"缺失"）
    ↓
二态（DeepSeek Harness：NOT_STARTED / OUTCOME_UNKNOWN）
    ↓
三态以上（Claude Code：已完成 / 被取消 / 继续运行）
```

**维度三：状态是否结构化**

```
纯文本（dummy 现状："[工具结果缺失:该次调用未执行或被中断]"）
    ↓
**结构化字段**（Hermes 的 `effect_disposition="none"`）
```

**Hermes 的 `effect_disposition` 是这三格里最值得直接借鉴的**——它是一个显式的结构化字段，声明"这次调用有没有产生副作用"。这正是 Codex issue #13976 里用户要求的"把取消作为一等上下文"。

### 9.3 一个关键对照：Codex 的缺陷 vs DeepSeek 的解法

这两个案例放在一起看，能看清"为什么必须显式标记状态"：

**Codex 的失败案例**（issue #13976）：

```
1. Codex 尝试执行 gh pr create
2. 用户取消
3. 下一轮，Codex 表现得好像 PR 可能已经存在
4. 用户手动纠正
```

**DSH 的对应设计**：

```
1. 工具调用已记录但没有结果
2. 持久化层在恢复时补标记 TOOL_OUTCOME_UNKNOWN
3. 该标记的语义："提醒模型不要盲目重复可能产生副作用的操作"
4. 模型下一轮看到这个标记 → 知道"PR 可能创建了，也可能没有"
   → 它的正确动作是"先检查 PR 是否存在"，而不是"假设已存在"或"再创建一次"
```

**两者的差别就是"有没有把不确定性显式传递给模型"。**

**dummy 的现状更接近 Codex**：

```
dummy 现在的占位文本："[工具结果缺失:该次调用未执行或被中断]"

这句话的问题：
  · 它把两种情况混为一谈："未执行"（肯定没副作用）和"被中断"（可能已执行）
  · 它是纯文本，模型要从这句话里"读"出含义
  · 它不说明批次的总量（模型不知道还有没有别的调用）
```

**这正是可以在 dummy 里改进的地方**（见第十一节）。

---

## 十、dummy 的现状与差距

### 10.1 dummy 现在的实现（逐处核对）

**① 批次执行：串行遍历**

`core.py:481-486`：

```python
# 2c. 遍历所有 tool_calls 并执行
# 一个 LLM 响应可能包含多个 tool_calls（虽然 Phase 0 的模型通常一次只调一个）
# 每个 tool_call 独立执行
# -----------------------------------------------------------
interrupt_triggered = False
for tool_call in response_message.tool_calls:
```

**事实**：
- dummy **允许**一个回复里有多个 `tool_calls`（代码注释也承认这一点）
- 执行方式是**严格串行**（`for` 循环，逐个 `dispatch`）
- **没有任何并发判定**——不存在"哪些能并行"的概念

**② 打断处理**

从对话记录 `conversation_20260917_194943.json` 观察到的事实：

```
[2] assistant 声明了 2 个工具调用
[3] tool: [用户打断,工具未执行]              ← 第 1 个的结果
[4] tool: [工具结果缺失:该次调用未执行或被中断]  ← 第 2 个的结果（占位）
```

**核对源码**：`session_store.py` 的 `repair_tool_pairing` 里的占位文案就是：

```python
"[工具结果缺失:该次调用未执行或被中断]"
```

**事实**：
- 打断时，**未执行的调用**会被补一条占位（这是 P2b 历史卫生机制的作用，保证 `tool_calls` 与 `tool` 的配对完整）
- 占位文本**不区分**"未执行"和"被中断"
- 占位文本**不包含**批次总量信息
- 占位文本**不含结构化字段**

**③ 打断的触发路径**

查源码，dummy 的打断有两条路径：

| 路径 | 触发 | 处理 |
|---|---|---|
| `/p` 打断 | 用户在工具执行期间输入 `/p` | `InterruptSignal` 抛出，当前工具标记未执行 |
| Ctrl+C | 键盘中断 | `KeyboardInterrupt`，走 `chat()` 的 `finally` 做历史收口 |

**④ 已有的相关机制**（不是空白）

dummy 已经有一些与"中断后状态"相关的机制：

| 机制 | 位置 | 作用 |
|---|---|---|
| `_settle_history()` | `main.py` | 退出前补齐残缺的 tool 配对 |
| `repair_tool_pairing()` | `session_store.py` | 补缺失 / 删孤儿 / 去重 / 删尾部悬空块 |
| `close_interrupted_tool_sequence()` | `session_store.py` | 尾巴停在裸 tool 上时补合成 assistant 轮次 |
| `_repair_history()` | `core.py` | chat 循环两处保险丝（压缩前、发请求前） |
| `turn_context` | `turn_context.py` | 记录本轮改动与执行（收尾时附情况说明） |

**关键判断**：dummy 在"**历史结构合法性**"上做得比较完整（配对、角色交替、尾部悬空都有处理），但在"**中断语义的表达**"上是空白——占位文本只说"缺失"，不说"为什么缺、缺的是什么状态"。

### 10.2 差距清单

对照第九节的三个维度：

| 维度 | 主流做法 | dummy 现状 | 差距 |
|---|---|---|---|
| **并发准入** | 有明确规则（白名单/路径/资源/fail-closed） | **无**（全串行，无判定） | 缺"能力"；但**不一定是缺陷**（见 11.1） |
| **打断作用范围** | 逐工具判断（Claude Code）或整批丢弃（Codex） | 整批丢弃 | 与 Codex 同级 |
| **未完成调用的状态精度** | 二态（DSH）或三态（Claude Code） | **一态**（"缺失"） | **缺"未执行"与"结果未知"的区分** |
| **状态是否结构化** | Hermes 用 `effect_disposition` 字段 | **纯文本** | 缺结构化字段 |
| **批次总量是否告知模型** | 未在材料中明确（各家做法不明） | **不告知** | 缺"批次上下文" |
| **截断中毒防护** | OpenCode 有（截断则整批判失败） | **无** | **潜在风险**（见 10.3） |

### 10.3 dummy 的特有风险：抢救式解析 + 截断

**dummy 的 `_parse_tool_arguments` 有三层容错**：

```
第 1 层：json.loads        （标准 JSON）
第 2 层：json5.loads       （容错 JSON：单引号、尾逗号、注释）
第 3 层：正则提取 + unescape （最坏情况兜底）
```

**容错策略本身是好的**（模型的 JSON 经常不规范），但**第 3 层的存在引入了一个风险**：

```
场景：max_tokens 用尽，assistant 消息在工具调用参数中间被截断
     例如模型想调用 write_file(path="a.py", content="很长的内容...")
     但 token 用尽，消息停在 content 参数中间

风险：第 3 层的正则可能从残缺文本里提取出一个"合法但内容不完整"的参数
     → 工具拿着不完整的内容执行 → 静默写入错误内容
```

**OpenCode 的对应防护**（7.2 节）：检测到截断 → **该批次所有工具调用都判为"未执行失败"**，不做任何抢救。它的理由（论文原文）：

> because salvage-parsed streaming arguments can validate while being silently incomplete
> （因为从流里抢救解析出来的参数可能通过校验，但实际上是静默不完整的）

**dummy 需要评估的点**：dummy 的 `llm.chat()` 是否检查 `finish_reason == "length"`？如果没有，这个风险就实际存在。**如实记录为待评估项，不在本报告中下"必须修改"的结论。**

---

## 十一、"打断追溯链"的设计：为什么不需要链表或树

> 这一节回答一个具体的设计问题：**要用链表或树来记录打断的追溯链吗？**
> 结论是**不需要**，理由如下。

### 11.1 先明确"追溯链"要解决什么问题

从用户的实际体验出发，被打断后有**三个层次的需求**：

**需求一（事后追溯）**：打断之后，用户或开发者想知道——
- 这批一共声明了几个调用？
- 执行到第几个被打断？
- 哪些执行完了、哪些没有？
- 为什么停的（用户打断 / 出错 / 卡死检测）？

**需求二（模型恢复）**：模型下一轮需要知道——
- 我上次声明的那些调用，哪些真的执行了？
- 有没有"可能已经产生副作用但结果未知"的调用？
- 我是该重试、该先验证、还是该跳过？

**需求三（跨会话追溯）**：如果一个回合被打断后进程退出，恢复会话时需要知道——
- 这个残缺的回合是怎么回事？
- 能不能从这个残缺状态继续？

### 11.2 为什么不需要链表

**理由：批次是有界的小数组，不是动态链。**

```
一个批次的规模：通常 1~5 个工具调用（论文提到 Claude Code 实测很少超过 5-6 个）
生命周期：一个步骤内（从模型回复接收完，到全部执行完）
```

**链表适合的场景是**：长度未知、需要频繁在中间插入/删除、需要共享尾部。**批次记录一条都不满足**：

| 链表特长 | 批次记录的实际情况 |
|---|---|
| 长度动态增长 | 长度在批次开始时**就确定了**（等于 `len(tool_calls)`） |
| 频繁中间插入 | **不插入**，只是从头到尾填状态 |
| 共享尾部（结构共享） | 不需要 |

**用链表反而带来成本**：
- 每个节点一个对象（内存开销）
- 遍历要跟指针（代码复杂度）
- **序列化麻烦**（`turn_context` 的记录将来可能落库，数组是天然可序列化的）

**数组 + 每项一个状态字段**就够：

```python
@dataclass
class ToolCallRecord:
    index: int          # 批次内序号（从 1 开始，给模型看的）
    total: int          # 这批共几个（让模型知道有没有"后面还有"）
    tool: str           # 工具名
    status: str         # "executed" | "unknown" | "skipped" | "cancelled"
    reason: str         # 为什么是这个状态
```

### 11.3 为什么不需要树

**理由：一个批次内的工具调用是平级的，不是嵌套的。**

有人可能会想用树，是因为想到这些场景：
- 工具 A 执行时触发了工具 B（嵌套）
- 用户打断工具 B，但工具 A 还在跑

**但 dummy 的架构里不存在这种嵌套**：

```
dummy 的工具执行是【扁平】的：
  for tool_call in response_message.tool_calls:   ← 平级循环
      dispatch(tool_call)                          ← 工具内部不会再调用别的工具

对照：Claude Code 有 AgentTool（工具可以启动子 Agent，子 Agent 里又有工具）
     → 那种场景确实需要树
```

**所以"要不要树"取决于"工具有没有嵌套调用能力"**：

| 系统 | 工具能否嵌套调用 | 数据结构需求 |
|---|---|---|
| Claude Code | ✅ 能（AgentTool 启动子 Agent） | 需要层级（它的 AbortController 就是三级的） |
| OpenHands | ✅ 能（task/delegate 启动子会话） | 需要层级 |
| Hermes | ✅ 能（delegate_task） | 需要层级 |
| **dummy** | ❌ **不能**（工具是叶子节点） | **扁平数组足够** |

**如实标注一个未来条件**：**如果 dummy 将来加了"子 Agent 委派"（Phase 4 的 `Sub-agent 委派` 在 roadmap 里），那么树结构就会变得必要**——因为那时一个工具调用会产生一棵子树，打断需要能表达"打断父级但子级继续"（Claude Code 的后台任务就是这种情况）。**现在不需要，将来可能。**

### 11.4 推荐的形态：批次记录 + 显式状态

**核心设计**：把"批次"作为一等记录单位，每个批次带一组调用状态。

```python
@dataclass
class BatchRecord:
    """一个工具批次（= 模型一次回复里的全部 tool_calls）的执行记录。"""
    batch_id: int                  # 本轮第几个批次
    total: int                     # 这批共几个调用
    stop_reason: str               # 为什么结束："completed" | "interrupted" | "error" | "guardrail"
    calls: list[ToolCallRecord]    # 每个调用的状态

@dataclass
class ToolCallRecord:
    index: int                     # 批次内第几个（1-based）
    tool: str
    status: str                    # 见下方状态定义
    reason: str = ""               # 补充说明
```

**状态取值（借鉴 DeepSeek Harness 的二态 + 补充 dummy 需要的两态）**：

| 状态 | 含义 | 副作用是否可能已发生 | 模型应该怎么做 |
|---|---|---|---|
| `executed` | 执行完成，有结果 | **已发生**（如果该工具有副作用） | 正常基于结果继续 |
| `unknown` | 调用已发出但没有结果（进程崩溃 / 强制退出） | **不确定** | **不要假设**，先验证（对应 DSH 的 `TOOL_OUTCOME_UNKNOWN`） |
| `cancelled` | 用户打断，**执行到一半** | **可能已发生** | 先确认再决定（对应 Claude Code 的 `user_interrupted`） |
| `skipped` | 用户打断，**还没开始** | **确定没发生** | 可以安全地重试（对应 DSH 的 `TOOL_NOT_STARTED`） |

**为什么把 `cancelled` 和 `skipped` 分开**：

```
两个都是"被用户打断"，但风险完全不同：

skipped  → 工具根本没开始跑 → 肯定没副作用 → 重试安全
cancelled → 工具跑了一半 → 可能已经改了文件/发了请求 → 重试危险
```

**dummy 现状的问题正是把这两者合并**成了一句"`[工具结果缺失:该次调用未执行或被中断]`"——**"未执行"和"被中断"写在同一句里，模型无法区分风险**。

### 11.5 呈现给模型的形式

**形式一：附在情况说明里**（复用 `turn_context` 的现有机制）

```
[本轮运行情况]
工具批次 #2（共 3 个调用，因为用户打断而结束）：
  [1/3] terminal: ls -la          → 已执行（有结果）
  [2/3] terminal: find ...        → 被打断（可能已部分执行）
  [3/3] read_file: main.py        → 未开始（可以安全重做）
```

**形式二：作为工具结果的补充字段**（更接近 Hermes 的 `effect_disposition` 做法）

每个工具结果消息带一个结构化字段：

```python
{
    "role": "tool",
    "tool_call_id": "...",
    "content": "...",              # 原有的结果文本
    "_effect": "none",             # 新增：本次调用有没有产生副作用
    "_batch": "2/3",               # 新增：批次位置
}
```

**但注意**：dummy 的历史要发给 API，而 **DeepSeek 对未知的顶层字段是严格的**（`core.py:441` 附近有注释说明 `strip_meta` 就是为了在发 API 前剥离内部元数据）。所以**形式二必须配合 `strip_meta` 的扩展**（把 `_effect` / `_batch` 也列入要剥离的字段），否则会 400。

**两个形式的取舍**：

| | 形式一（情况说明） | 形式二（工具结果字段） |
|---|---|---|
| 时机 | 只在收尾时给一次 | **每次工具结果都带** |
| 对模型的影响 | 弱（收尾时才知道） | **强（每轮都能看到）** |
| 实现成本 | 低（扩展 `turn_context`） | 中（要改消息结构 + `strip_meta`） |
| 风险 | 无 | 如果 `strip_meta` 漏了字段 → 400 |

**建议路径**：**先做形式一**（改动小、无风险、能验证效果），**观察后再评估是否升级到形式二**。理由是形式一已经能让模型在收尾时"知道自己漏了什么"，而形式二的价值主要在"执行中途就纠正"——那是个更靠后的优化。

### 11.6 与"需求三（跨会话追溯）"的关系

**如果被打断后进程退出了**，恢复会话时：

- **dummy 现状**：`repair_tool_pairing` 会补占位，然后开新回合（与 DSH 的"不续跑半个 Turn"策略**相同**）
- **DSH 的做法**：补标记（`TOOL_NOT_STARTED` / `TOOL_OUTCOME_UNKNOWN`），然后开新回合
- **差异**：DSH 的标记**携带语义**（告诉模型副作用是否可能发生），dummy 的占位**不携带**

**所以"跨会话追溯"的改进点与"需求一、二"是同一个**：**让占位文本携带状态语义**，而不是新增一套跨会话机制。**一处改动同时覆盖三个需求**。

---

## 十二、结论与建议

### 12.1 关于"工具并发"的结论

**事实**：7 个有材料的系统里，6 个支持批次内并发执行，只有 dummy 是纯串行。

**但"要不要给 dummy 加并发"不是本报告能直接下结论的事**，因为：

| 支持并发的理由 | 不支持的理由 |
|---|---|
| 模型确实会一次声明多个调用（实测已发生） | dummy 是单用户本地 Agent，延迟不敏感 |
| 多个只读调用并行能省时间 | 串行更容易保证顺序和可预测性 |
| 主流系统都做了 | 并发会引入新的一类 bug（冲突、顺序、部分失败） |
| | **并发不是"能力缺失"——它是一个权衡** |

**可借鉴的三条规则**（如果将来要做）：

1. **准入判定用 fail-closed**：解析失败、未声明、抛异常，一律判为不可并发（Claude Code / Codex / Hermes 三家一致）
2. **判断粒度用"调用级"而非"工具级"**：Claude Code 的 `Bash("ls")` vs `Bash("rm -rf")` 证明了工具级不够
3. **结果必须保序写回**：三家系统都这么做，没有例外。对 dummy 而言这是**硬约束**（历史配对要求 `tool_calls` 与 `tool` 顺序对应）

**一个更轻的替代方案**（不需要真并发）：**在 prompt 里要求模型一次只调一个工具**（这是 dummy 刚刚采取的措施，见 `prompt.py` 第 4 条）。**代价是零，效果待观察。**

### 12.2 关于"打断"的结论

**dummy 的差距是明确的、可量化的**：

| 差距 | 现状 | 建议 |
|---|---|---|
| 未完成调用的状态精度 | 一态（"缺失"） | **四态**（executed / unknown / cancelled / skipped） |
| 状态是否结构化 | 纯文本 | 结构化字段（但注意 `strip_meta`） |
| 批次上下文 | 不告知总量 | 告知 "N/M" |
| 中断原因 | 不记录 | 记录 stop_reason |

**这些差距的根源是同一个**：**dummy 的占位文本把"未执行"和"被中断"混为一谈**，而这两者的**副作用风险完全不同**。

### 12.3 建议的行动顺序

**第一步（最小改动，先验证价值）**：改进打断时补的占位文本，让它携带状态语义。

```
现状：
  "[工具结果缺失:该次调用未执行或被中断]"

改为（示意）：
  "[未执行] 用户打断时这个调用还没开始。可以安全重做。"
  或
  "[结果未知] 用户打断时这个调用正在执行,可能已产生副作用。
   不要假设它成功了,必要时先验证。"
```

**为什么从这里开始**：
- 改动只在 `session_store.py` 的占位文本生成处
- 不影响任何协议结构（`strip_meta` 不用改）
- 直接解决 Codex issue #13976 描述的同类问题
- **可以立刻用真实对话验证效果**

**第二步（如果第一步有效）**：把批次上下文加进 `turn_context`（复用现有的 `RunRecord`）。

**第三步（可选）**：评估是否需要结构化字段（`_effect` / `_batch`）——**要先确认 `strip_meta` 能正确处理新字段**。

**第四步（更远）**：评估并发能力。但**本报告不推荐现在做**——因为：
- 模型现在被要求"一次只调一个"（`prompt.py` 第 4 条）
- **并发的前提是有冲突判定规则，而 dummy 的工具集（5 个）里只有 1 个写工具**，冲突场景很少
- 真正的收益（省几秒）对单用户本地 Agent 价值有限

### 12.4 一个额外的发现（与并发/打断无关，但风险实在）

**dummy 完全没有检查 `finish_reason`**（10.3 节）。这意味着：

```
max_tokens 用尽 → 消息被截断 → dummy 不检查 → 抢救式解析可能拿到不完整参数 → 静默执行
```

**OpenCode 专门为此设计了"截断中毒防护"**（检测到截断则整批不执行）。

**建议**：把"检查 `finish_reason == 'length'` 时怎么处理"作为一个独立的评估项——**它与并发和打断都无关，但风险性质更严重**（会产生静默的错误结果，而不是可见的中断）。

---

## 附录：材料来源

| 系统 | 材料 | 性质 |
|---|---|---|
| Claude Code | claude-code-from-source.com 第 7 章（并发）；arXiv 2609.00006 第 6.2 节；kir shatrov 的逆向分析；trilogyai 的源码分析 | 第三方源码分析 + 学术论文 |
| Codex | arXiv 2609.00006（Table + 6.2 节）；GitHub openai/codex issue #13976；2026 版本更新说明 | 学术论文 + 官方 issue + 发布说明 |
| Hermes | **本机源码** `agent/tool_dispatch_helpers.py`、`agent/tool_executor.py`；arXiv 2609.00006 | **一手源码** + 学术论文 |
| DeepSeek Harness | Russell《万字长文：Deepseek Harness 一文全看懂》（第三方技术解析） | **第三方分析，非官方文档** |
| OpenHands / OpenCode / OpenClaw / Mistral Vibe | arXiv 2609.00006 第 6.2 节及相关章节 | 学术论文（源码级） |
| WorkBuddy | 媒体报道（新浪财经、腾讯云开发者社区、搜狐） | **产品层面，无架构细节** |
| Trae | 媒体报道、HN 讨论、安全分析报告 | **产品层面，无架构细节** |

**关于材料可靠性的说明**：
- **一手源码**（Hermes）：最高可信度
- **学术论文的源码研究**（arXiv 2609.00006）：高——它是对 pinned 到具体版本的源码做的分析，且论文自己标注了不确定项
- **第三方技术解析**（DSH）：中——分析深入，但未经官方核对
- **产品报道**（WorkBuddy / Trae）：低——只有定位与功能，无机制
