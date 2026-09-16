# 主流开源 AI Agent 框架如何表示"工具结果的成败"

调研日期: 2026-09-16
调研口径: 直接读 GitHub 上 raw 源码 / 官方 schema,不采信文档宣传。
网络说明: 本机 bash 直连 raw.githubusercontent.com 被墙(HTTP 000),全部源码经 web_extract 通道取得。

---

## 0. 结论先行

**生态的主流答案是"结构化字段 + 纯文本载荷"双层并存,而不是二选一。**

- 结构化字段(供**框架控制流 / 遥测 / UI** 用): `is_error` / `status` / `isError`
- 文本载荷(供 **LLM 读**用): 因为 OpenAI function calling 的 wire 格式里 tool 消息 content 只能是字符串,所以"成败"必须再以**文字**形式重复一遍给模型看。

不存在"结构化后又退回纯文本"的项目;真实的演进方向是**从纯文本 → 结构化**,而且是**保留文本、叠加结构化**(OpenHands 最典型)。

对你们的 bug(`[EXIT CODE: N]` 被误判成失败): **没有任何一个主流框架靠子串匹配工具输出判断成败**。要么让工具返回结构化字段,要么把"判定逻辑"和"给 LLM 看的文本"彻底分离。

---

## 1. OpenAI function calling 规范本身(最底层的约束)

**文件**: https://developers.openai.com/api/docs/guides/function-calling (官方 guide)

原文要点:
> "The result you pass in the `function_call_output` message should typically be a string, where the format is up to you (JSON, error codes, plain text, etc.). The model will interpret that string as needed."
> "If your function has no return value (for example, `send_email`), return a string that indicates success or failure, such as `"success"`."

**判定**: 纯文本。**OpenAI 的 wire 协议里 tool 消息没有任何 status/error 字段**,格式"由你决定"。

**这是整个生态困境的根源**: 协议层没有结构化失败位,所以上层框架要么(a)自己加结构化字段,要么(b)把错误塞进 content 文本。生态里两条路都有人走,且常常同时走。

**生态实践共识(第三方 guide,非官方源码)** — Cadence 博客:
> "The single biggest mistake in production agents is letting tool errors propagate as exceptions... wrap every tool execution in try/except and return the error as a JSON message."
```json
{"error": type(e).__name__, "message": str(e)[:500]}
```
即: **在 content 字符串里放 JSON** —— 文本容器里嵌结构化。这是 OpenAI 协议下的民间通用解法。

---

## 2. Anthropic / Claude —— 协议层唯一有 `is_error` 的

**文件**: https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls (官方)

wire 格式里 `tool_result` block **有显式的 `is_error: bool` 字段**:
```json
{"role": "user", "content": [
  {"type": "tool_result",
   "tool_use_id": "toolu_01A09q90qw90lq917835lq9",
   "content": "ConnectionError: the weather service API is not available (HTTP 500)",
   "is_error": true}
]}
```
文档原文: "If the tool itself throws an error during execution... you can return the error message in the `content` along with `\"is_error\": true`"

**关键对比**: Anthropic 在**协议层**就给了结构化成败位;OpenAI 没有。所以 Claude 生态的所有工具(含 Claude Code)天然能用结构化判断。

**Claude Code / Agent SDK 自定义工具**: https://code.claude.com/docs/en/agent-sdk/custom-tools
同一模式下沉到 MCP 工具:
```json
{"content": [{"type": "text", "text": "..."}], "is_error": true}
```
文档原文: "`isError` marks this as a failed call rather than odd-looking data."

**判定**: **结构化**(协议级 bool)。注意它的语义注释:结构化位的作用是"避免模型把失败结果当成正常数据"——这正是文本方案做不到的。

---

## 3. MCP 规范 —— `isError` 的结构化设计 + 官方给出的理由

**文件**: modelcontextprotocol/schema/2025-06-18/schema.ts,第 749-773 行(原文引用)

```ts
export interface CallToolResult extends Result {
  /** A list of content objects that represent the unstructured result of the tool call. */
  content: ContentBlock[];
  /** An optional JSON object that represents the structured result of the tool call. */
  structuredContent?: { [key: string]: unknown };
  /**
   * Whether the tool call ended in an error.
   *
   * If not set, this is assumed to be false (the call was successful).
   *
   * Any errors that originate from the tool SHOULD be reported inside the result
   * object, with `isError` set to true, _not_ as an MCP protocol-level error
   * response. Otherwise, the LLM would not be able to see that an error occurred
   * and self-correct.
   *
   * However, any errors in _finding_ the tool, an error indicating that the
   * server does not support tool calls, or any other exceptional conditions,
   * should be reported as an MCP error response.
   */
  isError?: boolean;
}
```

**这是本调研最有价值的一段设计依据**,它明确区分了三层:
1. `content`: 非结构化结果(给 LLM 看)
2. `structuredContent`: 结构化结果(可选,机器读)
3. `isError`: 结构化成败位 —— **且明确要求用结果内字段而非协议级错误**,理由是"否则 LLM 看不到发生了错误、无法自我纠正"

即:**同一份结果里同时带"人读文本"和"机读成败位"**。MCP 把"给 LLM 看"和"给程序判断"视为两个不同消费者,刻意分开表达。

---

## 4. OpenHands (software-agent-sdk, V1) —— 最完整的"三层"样本

### 4.1 基础 Observation:显式 `is_error: bool`
**文件**: openhands-sdk/openhands/sdk/tool/schema.py

```python
class Observation(Schema, ABC):
    """Base schema for output observation."""
    ERROR_MESSAGE_HEADER: ClassVar[str] = "[An error occurred during execution.]\n"
    content: list[TextContent | ImageContent] = Field(
        default_factory=list,
        description=("Content returned from the tool ... When there is an error, it should be written in this field."),
    )
    is_error: bool = Field(
        default=False, description="Whether the observation indicates an error"
    )

    @classmethod
    def from_text(cls, text: str, is_error: bool = False, **kwargs) -> "Self":
        return cls(content=[TextContent(text=text)], is_error=is_error, **kwargs)

    @property
    def to_llm_content(self):
        llm_content = []
        if self.is_error:                      # ← 结构化位 → 转成文本前缀给 LLM
            llm_content.append(TextContent(text=self.ERROR_MESSAGE_HEADER))
        llm_content.extend(self.content)
        return llm_content
```
注意: `is_error=True` 时会给 LLM 的消息前面**插一句文本** `[An error occurred during execution.]` —— 结构化位最终仍要降级为文本才能进 LLM。

### 4.2 terminal 工具:结构化 `exit_code` + 文本后缀,二者并存
**文件**: openhands-tools/openhands/tools/terminal/definition.py

```python
class TerminalObservation(Observation):
    command: str | None = Field(...)
    exit_code: int | None = Field(
        default=None,
        description="The exit code of the command. -1 indicates the process hit the soft timeout and is not yet finished.",
    )
    timeout: bool = Field(default=False, description="Whether the command execution timed out.")
    metadata: CmdOutputMetadata = Field(...)

    @property
    def to_llm_content(self):
        llm_content = []
        if self.is_error:
            llm_content.append(TextContent(text=self.ERROR_MESSAGE_HEADER))
        ret = f"{self.metadata.prefix}{self.text}{self.metadata.suffix}"
        ...
        if self.metadata.exit_code != -1:
            ret += f"\n[Command finished with exit code {self.metadata.exit_code}]"   # ← 注意!
        ...
```

**这就是你们 bug 的同款写法**: terminal 结果**既有**结构化 `exit_code` 字段,**又在文本里追加** `[Command finished with exit code N]`。

而且 UI 渲染时**不看 `is_error`,而是自己根据 `exit_code` 重新判断**:
```python
if self.metadata.exit_code == 0:
    text.append("\n✅ "); text.append(f"Exit code: {self.metadata.exit_code}")   # 绿
elif self.metadata.exit_code == -1:
    text.append("\n⏳ "); text.append("Process still running (soft timeout)")      # 黄
else:
    text.append("\n❌ "); text.append(f"Exit code: {self.metadata.exit_code}")   # 红
```
**关键点**: 成功与否由 **`exit_code == 0` 这个结构化字段**决定,跟文本内容、跟 `is_error` 都解耦。文本里的 `[Command finished with exit code 0]` 纯粹是给 LLM 读的。

### 4.3 Observation 的类层次(用**类型**表达成败,而非 bool)
**文件**: openhands-sdk/openhands/sdk/event/llm_convertible/observation.py

```python
class ObservationBaseEvent(LLMConvertibleEvent):
    """Base class for anything as a response to a tool call. Examples include tool execution, error, user reject."""
    source: SourceType = "environment"
    tool_name: str
    tool_call_id: ToolCallID

class ObservationEvent(ObservationBaseEvent):      # 成功
    observation: Observation
    action_id: EventID
    extended_content: list[TextContent]

class UserRejectObservation(ObservationBaseEvent):  # 用户/hook 拒绝
    rejection_reason: str = "User rejected the action"
    rejection_source: RejectionSource = "user"   # Literal["user", "hook"]

class AgentErrorEvent(ObservationBaseEvent):        # scaffold 错误
    source: SourceType = "agent"
    error: str
    classification: ErrorClassification | None = Field(
        default=None, description="Safe structured error semantics for API consumers."
    )
```

**这是第三条路线: 用类/事件类型区分成败**(而非一个 bool 字段)。成功是 `ObservationEvent`,失败按**来源**细分(`UserRejectObservation` / `AgentErrorEvent`)。类型信息天然结构化、不可误判。

### 4.4 失败原因也结构化: `ErrorClassification`
**文件**: openhands-sdk/openhands/sdk/event/error_classification.py

```python
class FailureKind(StrEnum):
    AUTH = "auth"; QUOTA = "quota"; RATE_LIMIT = "rate_limit"; CONFIG = "config"
    TRANSIENT = "transient"; AGENT_ACTION = "agent_action"; INTERNAL = "internal"; UNKNOWN = "unknown"

FailureAction = Literal["none", "retry", "settings"]

class ErrorClassification(BaseModel):
    """The only failure metadata that crosses the event/API boundary."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: FailureKind
    retryable: bool
    user_action: FailureAction = "none"
    error_id: str | None = None
```
注释(self-documented): "``AgentErrorEvent`` describes *where* an error was surfaced (the agent scaffold), not *why* it happened."

**方法**: 失败归属也走枚举 + `retryable` bool,而不是解析错误文本。虽然 `classify_error(code, detail)` 内部为了**映射第三方错误**还是不得不做 `detail.casefold()` 子串匹配(`"rate limit" in text`、`"error code: 429" in text` 等),但那是**边界适配层**,且输出立刻转成闭合枚举 —— 内部逻辑永远只消费 `FailureKind`,不碰原始字符串。**失败分类的文本匹配被限制在"翻译层",不外泄到判定层** —— 这条对你们有直接借鉴价值。

### 4.5 V0→V1 迁移:明确走过"结构化"这条路
**文件**: OpenHands/OpenHands issue #10577 "Proposal: Minimal Python SDK"(2025-08-22,已关闭)

原文:
> ToolResults use the MCP output format, with an additional `meta` field for extra data.
> Example:
> ```
> "content" [{"type": "text", "text": "Hello world"}]
> "meta" {"observationType": "CmdOutputObservation", "exitCode": 0, "isError": false}
> ```

并且计划里写:
> "ToolResult represents the output of a ToolCall. It corresponds most closely to the generic `Observation` class in OpenHands, or possibly `MCPObservation`"

**判定**: OpenHands 从 V0 的 `Action/Observation` 类体系,**主动迁移到 MCP 格式 + `meta` 结构化字段**。方向是"更结构化",不是退回文本。V0 已于 2026-04 标记弃用。

**tradeoff(OpenHands 自己踩的坑,源码可见)**:
- 结构化好,但 **wire 协议只吃文本** → 必须在 `to_llm_content` 里把结构化值再**拼回文本**(`[Command finished with exit code N]`)→ 文本标记与结构化字段并存,两套表示有**不一致风险**。
- PS1 marker 被 TUI/ANSI 破坏时无法取 exit_code,源码里降级为 `exit_code=-1` + 文本 `"[The command completed but the exit code could not be determined...]"`,并 `logger.warning`。**结构化字段取不到值时的兜底,仍是文本解释。**

---

## 5. Aider —— 用**返回值元组**,从不解析文本

**文件**: aider/run_cmd.py

```python
def run_cmd_subprocess(command, verbose=False, cwd=None, encoding=sys.stdout.encoding):
    ...
    process.wait()
    return process.returncode, "".join(output)     # ← (exit_code, output) 元组

def run_cmd(command, verbose=False, error_print=None, cwd=None):
    try:
        ...
    except OSError as e:
        error_message = f"Error occurred while running command '{command}': {str(e)}"
        return 1, error_message                     # ← 失败也返回 (1, msg),不抛文本
```

**判定**: **结构化,且是最"硬"的一种** —— 通过 Python 返回值元组把 exit code 和输出**物理分离**,调用方 `exit_code, output = run_cmd(...)` 拿到的 exit code 是 `int`,**不可能被子串匹配污染**。

**Aider 踩过的"退出码误判"坑(官方文档 + issue 明确记录)**:

文档 https://aider.chat/docs/usage/lint-test.html 原文:
> "Many people use code formatters as linters... These tools sometimes return non-zero exit codes if they make changes, **which will confuse aider into thinking there's an actual lint error that needs to be fixed**."

Issue #2167 "Autofix with lint tools: add --lint-twice":
> "many linters will exit with non-zero exit code when they are simply reformatting the code. If a second run exits clean then there are no actual [errors]"

**这是与你们 bug 同族问题的实证**: "非零退出码 ≠ 失败"。Aider 的处理是**加一轮二次运行来消歧**(`--lint-twice`),而不是改判定逻辑 —— 说明即便有结构化 exit code,"exit code 语义 != 成败语义"这个坑依然存在。

---

## 6. LangChain / LangGraph —— `ToolMessage.status`(结构化,但曾被路由逻辑忽略)

### 6.1 字段定义(结构化)
**文件**: libs/core/langchain_core/messages/tool.py

```python
class ToolMessage(BaseMessage, ToolOutputMixin):
    """Message for passing the result of executing a tool back to a model."""
    tool_call_id: str
    type: Literal["tool"] = "tool"
    artifact: Any = None
    """Artifact of the Tool execution which is not meant to be sent to the model."""
    status: Literal["success", "error"] = "success"
    """Status of the tool invocation."""

def _merge_status(left, right) -> Literal["success", "error"]:
    return "error" if "error" in {left, right} else "success"   # 流式合并:error 优先
```

**注意**: `status` 是**聚合语义**(error 优先)且 **不发给模型** —— 官方注释把 `artifact` 明说"not meant to be sent to the model",`status` 同理属于框架内部字段。

### 6.2 但 content 仍是文本,错误信息是模板化的纯文本
**文件**: libs/prebuilt/langgraph/prebuilt/tool_node.py

```python
TOOL_CALL_ERROR_TEMPLATE = "Error: {error}\n Please fix your mistakes."
TOOL_EXECUTION_ERROR_TEMPLATE = (
    "Error executing tool '{tool_name}' with kwargs {tool_kwargs} with error:\n"
    " {error}\n"
    " Please fix the error and try again."
)
TOOL_INVOCATION_ERROR_TEMPLATE = (
    "Error invoking tool '{tool_name}' with kwargs {tool_kwargs} with error:\n"
    " {error}\n"
    " Please fix the error and try again."
)
```
**给 LLM 的仍是英文散文文本**("Please fix your mistakes"),结构化 `status` 不进 LLM。

### 6.3 `status` 才是控制流的真契约
同文件 `ToolCallWrapper` docstring 里的**官方推荐写法**:
```python
# Conditional retry based on response:
def handler(request, execute):
    for attempt in range(3):
        result = execute(request)
        if isinstance(result, ToolMessage) and result.status != "error":   # ← 看 status,不看文本
            return result
        if attempt < 2:
            continue
        return result
```
`handle_tool_errors` 的返回值语义(官方 reference): "the content is normalized to the content of a `ToolMessage` with `status=\"error\"`"。

### 6.4 LangChain 真实踩坑:#36411 —— 结构化字段有了,但路由逻辑没看它
**Issue**: langchain-ai/langchain#36411 "bug(langchain): agent terminates on return_direct tool even when tool call fails"

> "Expected behavior: if any `ToolMessage` produced by a `return_direct` tool has `status=\"error\"`, the agent should route back to the model instead of terminating...
> `ToolMessage.status` is already defined in `langchain-core` (`Literal[\"success\", \"error\"]`) and populated by the tool base class on failure, **so no new fields are needed — only the routing condition needs updating.**"

引用的问题代码:
```python
# Current code (factory.py:1802-1805)
if client_side_tool_calls and all(tools_by_name[c["name"]].return_direct for c in client_side_tool_calls):
    return end_destination          # exits even if tool failed
```

**这是与你们 bug 结构性同源的真实案例**: 结构化成败字段**已经存在**(`status="error"`),但**决定行为的代码没读它**,于是失败被当成成功处理。教训: 有了结构化字段还不够,**所有控制流分支都必须消费该字段**;否则等于没有。

### 6.5 MCP → ToolMessage 的桥接
langchain-mcp-adapters PR #626: MCP 的 `isError` 结果被映射为 `status="error"` 的 ToolMessage,并保留 `structuredContent` 到 `artifact`。说明生态在**收敛到统一的结构化成败表示**。

---

## 7. OpenAI Codex CLI

**判定口径说明**: Codex 是 Rust 闭源二进制 + 开源 repo(openai/codex)。我未能在 openai/codex 源码中直接定位到 exit_code 字段的权威定义行(仓库结构复杂、`codex-rs/tools/src/lib.rs` 只导出 `mod tool_output; ... pub use tool_output::ToolOutput;` 而未展开该模块内容)。**以下为第三方逆向分析,标注为"非我直读源码",置信度较低:**

第三方兼容层项目 2h2d-co/pi-openai-codex-compat 的 `OFFICIAL_CODEX_CLI_TOOL_CATALOG.md`(**逆向 Codex 0.149.1**)声称:
> "Official Codex's in-memory `exec_command` and `write_stdin` specifications share a closed output schema requiring `wall_time_seconds` and `output`, with optional `chunk_id`, `exit_code`, `session_id`, and `original_token_count`."
> "**Exit semantics**: Unified nonzero exits and one-shot nonzero exits/timeouts are **successful tool results**. One-shot timeouts use exit code 124 and prepend a timeout line."
> "changing only Pi's internal `isError` classification without changing model-visible output is explicitly deferred."

**若此分析属实,Codex 的核心设计是: `exit_code` 是结构化字段且语义独立于 `isError`;非零退出码默认仍算"成功的工具结果"。** —— 直接印证"退出码 ≠ 成败"。

**我实际直读到的**: `codex-rs/tools/src/lib.rs` 确实存在 `mod tool_output;` → `pub use tool_output::ToolOutput;` 与 `pub use tool_output::JsonToolOutput;`,说明 Codex 有独立的 **`ToolOutput` 抽象**(结构化输出类型),而非纯字符串。这一条是我看源码确认的。

---

## 8. 横向对照表

| 项目 | 成败表示 | 类型 | LLM 看到的 | 控制流依据 |
|---|---|---|---|---|
| OpenAI API 规范 | 无 | **纯文本** | content 字符串 | 无(由调用方决定) |
| Anthropic API | `is_error` | **结构化** | content 字符串 + is_error 位 | `is_error` |
| MCP 规范 | `isError` | **结构化** | `content` + `structuredContent` | `isError` |
| OpenHands | `is_error` + `exit_code` + 事件类 | **结构化+类型** | 文本 + `[Command finished with exit code N]` | `is_error` / `exit_code==0` / 事件类型 |
| Aider | `(exit_code, output)` 元组 | **结构化** | 原始输出文本 | `exit_code != 0` |
| LangChain | `ToolMessage.status` | **结构化** | 模板化英文错误文本 | `status != "error"` |
| LangGraph | `status` + 错误模板 | **结构化+文本** | `"Error: ... Please fix"` | `status != "error"` |
| Codex CLI(待证实) | `exit_code` | **结构化** | 文本 + timeout 行 | 非零退出码仍算成功 |

**统计**: 明确走结构化的 6 个;协议层纯文本的 1 个(OpenAI);**没有一个是"结构化后又退回纯文本"**;全部保留"给 LLM 的文本"作为并行通道。

---

## 9. 对你们框架的直接建议(基于以上实证)

1. **绝不用子串匹配工具输出判成败**。所有被调研框架都不这样做 —— 最差的 Aider 也至少用返回值元组。

2. **采用"双层"而不是"二选一"**,这是生态唯一共识:
   - 结构化层:工具返回 `is_error: bool`(或 `status: Literal["success","error"]`)+ 可选 `exit_code: int` / `error_kind: enum`。
   - 文本层:给 LLM 的内容里可以继续写 `[EXIT CODE: N]`,但**判定逻辑永远不读它**。

3. **`exit_code` 与 `is_error` 必须语义独立**(Codex 的做法,OpenHands 也如此)。非零退出码不一定失败(`grep` 无匹配返回 1、formatter 改动返回非零 —— Aider #2167 实证)。判定应是 `exit_code == 0` 的显式规则,**且由工具自己给 `is_error`,不要让框架从文本猜**。

4. **像 MCP 那样明确注释"为什么用结果内字段而不是抛异常"**: 因为要让 LLM 看到失败并自我纠正。这会防止后来者把错误改成异常抛出。

5. **把文本匹配限制在"翻译层"**(OpenHands `classify_error` 的做法):允许在适配第三方错误时做子串匹配,但输出立刻收敛为闭合枚举,**判定层只消费枚举**。

6. **警惕 LangChain #36411 的教训**: 加了结构化字段后,**审计所有读取工具结果的控制流分支**,确认每一处都消费该字段。字段存在但分支忽略 == 没有字段。

---

## 10. 我实际看到的代码 vs 我的推测(按要求区分)

**实际直接读到的源码/规范原文**:
- OpenAI function-calling guide 原文(web_extract)
- Anthropic `is_error` wire 格式 + Claude Code MCP 工具 `is_error`(官方 docs)
- MCP `CallToolResult.isError` 定义与注释(schema.ts 749-773 行,已 read_file 逐行确认)
- OpenHands: `tool/schema.py`(Observation.is_error)、`tools/terminal/definition.py`(exit_code + 文本后缀 + visualize)、`event/llm_convertible/observation.py`(事件类层次)、`event/error_classification.py`、`terminal/terminal_session.py`(PS1 降级路径)
- OpenHands issue #10577 全文(MCP meta 格式)
- Aider: `run_cmd.py` 全文(元组返回);lint-test 文档 + issue #2167 引文
- LangChain: `messages/tool.py`(status 字段 + _merge_status)、`tools/base.py`(ToolException)
- LangGraph: `prebuilt/tool_node.py`(错误模板 + `status != "error"` 写法)
- Codex: `codex-rs/tools/src/lib.rs`(存在 `ToolOutput` 模块,已确认)

**我的推测(未直读权威源码,置信度低)**:
- Codex CLI 的 `exit_code` 语义("非零退出仍算成功")—— 来自第三方逆向项目,非 OpenAI 官方源码。
- Lance 未逐行核对 `tool_output.rs` 的字段定义(仓库路径未定位到)。

**未展开/未读**:
- Codex `codex-rs/tools/src/tool_output.rs` 全文(未定位成功)
- Claude Code 闭源主体(仅有官方 docs 的 SDK 接口)
- LangGraph `_default_handle_tool_errors` 实现体(仅读到 reference 描述)
- OpenHands V0 仓库的 `CmdOutputObservation` 历史定义(V0 已弃用,仅通过 #10577 间接了解)
