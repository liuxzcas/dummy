# Phase 2.2 实施计划 — Context Compression

> EN TL;DR: Execution plan for dummy-agent Phase 2.2 (context compression).
> Definition of Done: 8-question fact-recall mini test set ≥90%, measured
> compression ≥50%, compressed history always API-valid, /resume works after
> compression, real-world smoke test passes, and — the key addition — failure
> handling is explicit and fault-tolerant: compression never makes things worse
> than not compressing. Failures are visible (CompressionResult + event log +
> CLI messages), degrade gracefully (L1-only → skip → circuit breaker pause),
> and never corrupt in-memory history (atomicity + post-compression validation).
> Companion design doc: docs/context-compression.md (what to build);
> this file is the how-to (step order, per-step verification, failure design).

## 1. 目标与验收标准(DoD)

目标:长对话接近上下文上限时自动压缩早期内容,不报错、不丢关键事实、不污染历史。

全部满足才算完成:

- [ ] 8 题 mini 事实召回测试集通过,召回率 ≥ 90%
- [ ] 实测压缩率 ≥ 50%(压缩前后对比)
- [ ] 压缩后调用 API 100% 成功(格式合法性)
- [ ] /resume 恢复压缩后的会话一切正常
- [ ] 真实长对话冒烟通过(DeepSeek 实测)
- [ ] 压缩失败时:看得见(事件日志 + CLI)、退得稳(降级阶梯)、停得下(断路器)

## 2. 前置条件

- Session 持久化已完成(7d60e11 已合入 main)
- save_history 已支持历史缩水(逐条 upsert + 删尾部)
- 设计文档:docs/context-compression.md(决策 D1-D7)

## 3. 改动文件清单

| 文件 | 动作 | 说明 |
|------|------|------|
| `compressor.py` | 新增 | 压缩逻辑 + 容错(全部纯逻辑,可单测) |
| `tests/test_compression.py` | 新增 | 测试集 + 容错用例 |
| `llm.py` | 修改 | `chat()` 暴露 `last_usage`(1 行) |
| `core.py` | 修改 | 触发点 + strip_meta + 事件日志 + 折叠原文归档(约 40 行) |
| `session_store.py` | 修改 | 新增 `tool_result_archive` 表 + 归档写入/查询方法(决策 C) |
| `.gitignore` | 修改 | 追加 `logs/compression.jsonl`(若 logs/ 已忽略则无需) |

## 4. 架构总览

```
用户输入 → chat()
  → 工具循环顶部(每次调 LLM 前):
      should_compress(last_usage.prompt_tokens)?
        ├─ 否 → 继续
        └─ 是 → compressor.compress(history)      # 永不抛异常
                  ├─ L1 ToolResult 折叠(零 LLM)
                  ├─ L2 增量摘要(失败→重试1次→降级L1)
                  ├─ _validate_history(压缩后校验)
                  └─ 返回 CompressionResult(显式状态)
          → success? 更新 history + _persist_history()
          → 无论成败:写 logs/compression.jsonl + CLI 反馈
  → strip_meta(history) → llm.chat()
```

关键不变量:
- compress() 是纯函数:不原地修改,失败返回原历史(调用方决定)
- 压缩只发生在"即将调 LLM"的静止点,绝不在工具循环中途
- 磁盘(SQLite)永远保留全文,内存压缩可逆

## 5. 分步实施计划

### Step 1 · llm.py 暴露 token 计数(5 分钟)

```python
# llm.py 的 chat() 末尾:
self.last_usage = response.usage   # 精确 token 计数,不估算
return response.choices[0].message
```

验证:mock response,断言 `last_usage.prompt_tokens` 值正确。

### Step 2 · compressor.py 骨架 + 配置 + CompressionResult(20 分钟)

```python
@dataclass
class CompressionConfig:
    window_tokens: int = 64000        # 模型上下文窗口,DeepSeek-V3 起步值,按实际模型调整
    threshold_ratio: float = 0.7      # 触发阈值:窗口的 70%
    recent_turns_keep: int = 6        # 保留最近 N 轮原文(按 user 消息计数)

    # 阶段开关:L1/L2 独立可开关(分阶段验证与调试用)
    enable_l1: bool = True
    enable_l2: bool = True
    tool_result_max_chars: int = 600  # 超过才折叠
    tool_result_keep_head: int = 200
    tool_result_keep_tail: int = 100
    summary_max_tokens: int = 1024
    summary_retry: int = 1            # 瞬时错误重试次数
    max_consecutive_failures: int = 3 # 断路器阈值

@dataclass
class CompressionResult:
    success: bool
    history: list | None          # 失败时为 None
    strategy_used: str            # "L1+L2" / "L1" / "none"
    folded_count: int = 0
    summary_covers: int = 0
    chars_before: int = 0
    chars_after: int = 0
    error_type: str | None = None # "summary_timeout"/"empty_summary"/"invalid_history"/...
    error_msg: str | None = None
    duration_ms: int = 0

def should_compress(last_prompt_tokens) -> bool:
    # 断路器暂停时直接 False,零开销
```

验证:边界测试(临界值上下各 1 token)、断路器暂停时返回 False。

### Step 3 · L1 ToolResult 折叠 + 原文归档(1 小时)—— 核心,先做零成本的

决策 C 落地:折叠时捕获被截断的原文,交 session_store 写入归档表。

```python
# compressor.py
def _fold_tool_results(self, history) -> tuple[list, int, list[dict]]:
    """只改 role=tool 且超长的消息 content,不动结构(配对天然安全)。

    返回 (新历史, 折叠条数, 折叠原文列表)。
    折叠原文 = [{"tool_call_id":..., "content": 完整原文}],供调用方归档(决策 C)。"""
    folded = 0
    originals: list[dict] = []
    out = [dict(m) for m in history]          # 浅拷贝,保持原子性
    for msg in out:
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
            c = msg["content"]
            if len(c) > self.config.tool_result_max_chars:
                originals.append({"tool_call_id": msg.get("tool_call_id"), "content": c})
                msg["content"] = (c[:self.config.tool_result_keep_head]
                    + f"\n...[ToolResult 已截断: 原文 {len(c)} 字符, 完整内容见归档表 tool_result_archive]...\n"
                    + c[-self.config.tool_result_keep_tail:])
                folded += 1
    return out, folded, originals
```

CompressionResult 增加字段(决策 C):

```python
folded_originals: list[dict] | None = None  # 折叠的 tool 原文,交调用方归档
```

session_store.py 新增(决策 C):

```sql
CREATE TABLE IF NOT EXISTS tool_result_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tool_call_id TEXT,           -- 稳定键:压缩重写不改变 tool_call_id
    folded_at TEXT NOT NULL,     -- 折叠时间
    original_len INTEGER NOT NULL,  -- 原文长度
    content TEXT NOT NULL        -- 完整原文
)
```

```python
def archive_tool_results(self, session_id, items) -> None:
    """写入归档表;同 (session_id, tool_call_id) 覆盖旧值(幂等)。"""

def get_archived_tool_result(self, session_id, tool_call_id) -> str | None:
    """按需取回折叠前的完整原文。"""
```

验证:1000 字 tool 消息 → 断言截断格式 + originals 捕获正确;短消息不折叠不捕获;
其他 role 不碰;归档表写入后按 tool_call_id 取回 == 原文。

### Step 4 · core.py 触发点接入(20 分钟)

```python
# 工具循环顶部,每次调 LLM 之前:
if self.compressor.should_compress(
        self.llm.last_usage.prompt_tokens if self.llm.last_usage else None):
    result = self.compressor.compress(self.history)
    if result.success:
        self.history = result.history
        persist_error = None
        try:
            self._persist_history()
            # 决策 C:折叠原文归档(重写已删掉折叠前的 tool 原文,这里补回)
            if result.folded_originals:
                self.session_store.archive_tool_results(
                    self.current_session_id, result.folded_originals)
        except Exception as e:
            persist_error = str(e)
            print(f"⚠️ 压缩后落库失败: {e}(内存已更新,下次 persist 重写)")
    else:
        print(f"⚠️ 压缩失败({result.error_type}): 已跳过,对话继续")
    # 事件日志:成败都记;落库/归档失败也写入 error_type=persist_failed,
    # 使"落库失败率"与"压缩降级率"一样可统计(可观测性,见 6.5)
    self._log_compression_event(
        result,
        error_type="persist_failed" if persist_error else None,
        error_msg=persist_error,
    )
```

验证:e2e mock——触发 → 压缩 → 对话继续 → 落库消息数正确。

### Step 5 · L2 增量摘要 + 容错(2 小时)—— 核心难点

```python
def _summarize_prefix(self, history) -> tuple[list, int]:
    """旧摘要 + 新块 → 新摘要。返回 (新历史, covers)。
    失败返回 (原 history, 0),由调用方决定降级。"""
    # 1. 找已有摘要(_meta.compressed),取旧摘要 + 旧 covers
    # 2. 找 cut 点:user 消息倒数的 recent_turns_keep 个,cut 在 user 边界
    #    (保证 tool_calls/tool 配对完整)
    # 3. 待压块 = 摘要之后到 cut 之前;空则跳过
    # 4. 调 LLM(见 _call_summary_llm,含重试与内容校验)
    # 5. 新历史 = system 消息 + [摘要消息(role=user, _meta.covers)] + 最近 N 轮
```

```python
def _call_summary_llm(self, old_summary, block) -> str:
    """摘要调用:重试 1 次 + 空内容校验。失败抛 SummaryError。"""
    prompt = self.config.summary_prompt.format(...)
    last_err = None
    for attempt in range(self.config.summary_retry + 1):
        try:
            resp = self.llm.chat(messages=[{"role": "user", "content": prompt}],
                                 temperature=0.2,
                                 max_tokens=self.config.summary_max_tokens)
            text = (resp.content or "").strip()
            if not text:
                raise SummaryError("empty summary")     # 内容错误不重试
            return text
        except SummaryError:
            raise
        except Exception as e:                           # 瞬时错误才重试
            last_err = e
    raise SummaryError(f"summary failed after retry: {last_err}")
```

验证(mock LLM):
- 结构正确(摘要 + 最近 N 轮)、covers 累加、二次压缩不重复压
- cut 从未切断 tool_calls 配对
- 摘要抛 timeout → 重试 1 次 → 仍失败 → 降级 L1-only,原历史保留
- 摘要返回 None → 丢弃,降级 L1-only

### Step 6 · 压缩后校验 + strip_meta(20 分钟)

```python
def _validate_history(history) -> bool:
    """配对校验:assistant 消息里的每个 tool_calls id,
    必须存在同 session 的 tool 消息引用它。压缩结果不过则回滚。"""
    open_calls = set()
    for m in history:
        for tc in (m.get("tool_calls") or []):
            open_calls.add(tc.get("id"))
        if m.get("role") == "tool":
            open_calls.discard(m.get("tool_call_id"))
    return not open_calls  # 有未配对 → 非法

def strip_meta(history) -> list:
    """发 API 前剥离 _meta(浅拷贝),不污染内存里的元数据历史。"""
    return [{k: v for k, v in m.items() if k != "_meta"} for m in history]
```

验证:构造未配对历史 → 校验拦截;strip 后无 _meta 键。

### Step 7 · 测试集 + 质量门(1.5 小时)—— 最重要

`tests/test_compression.py`:

A. 事实召回 8 题(压缩后问问题,断言包含匹配):
文件路径 / 代码细节 / 用户偏好 / 决定 / 数字 / 未完成事项 / 工具结果 / 跨轮次

B. 容错 5 用例(见第 7 节)

C. 格式合法性:压缩后 history 过 API schema 校验(严格 mock 或真实 DeepSeek)

验证:全部通过才算 Step 7 完成。

### Step 8 · 真实冒烟(30 分钟)

- DeepSeek 跑长对话(30+ 轮逼出触发),人工检查早轮事实召回、回答质量
- 记录实测:触发时 prompt_tokens、压缩率、摘要延迟、事件日志内容

### Step 9 · 收尾(15 分钟)

- roadmap.md 更新 Phase 2.2 状态
- 提交 → 合并 main → 推送(网络恢复后)

## 6. 容错设计(显式 + 降级阶梯 + 断路器)

### 6.1 失败场景清单

| # | 失败点 | 典型原因 | 处理 |
|---|--------|---------|------|
| 1 | L2 摘要调用失败 | 网络/超时/API 错误 | 重试 1 次 → 降级 L1-only |
| 2 | 摘要空/None/超长 | 模型抽风 | 丢弃,降级 L1-only(不重试) |
| 3 | L1 折叠异常 | 理论上不会(纯字符串) | try 兜底,记录 |
| 4 | 压缩结果结构非法 | 意外切断配对 | 压缩后校验,回滚用原历史 |
| 5 | 压缩后落库失败 | SQLite 锁/磁盘 | 记录错误(`error_type=persist_failed` 写入事件日志,可统计),内存继续,下次重写 |
| 6 | 连续失败死循环 | 每次触发每次都失败 | 断路器暂停压缩 |

### 6.2 设计原则

> **压缩失败不能比不压缩更糟——失败要看得见、退得稳、停得下。**

1. 原子性:compress() 绝不原地修改 history;失败返回 success=False,调用方继续用原历史,内存状态永不被半成品污染
2. 内容校验:空/None 摘要一律视为失败,宁可降级也不塞垃圾进历史
3. 压缩后校验:_validate_history 是"压缩比不压缩更糟"的最后防线
4. 重试只对瞬时错误(网络/超时);内容错误重试无意义,直接降级

### 6.3 降级阶梯

```
正常:  L1 折叠 + L2 摘要        → 成功,更新 history
  ▼ L2 失败(重试后)
降 1:  只做 L1 折叠             → 仍有效(tool 结果通常占 token 大头)
  ▼ L1 也异常(几乎不可能)
降 2:  本轮跳过压缩             → 原历史继续对话
  ▼ 连续失败 ≥ 3 次
降 3:  断路器暂停               → should_compress 直接 False,零开销;
                                  CLI 明确告知;新会话自动复位
```

### 6.4 断路器

- 计数器挂在 Compressor 实例:`self._consecutive_failures`
- 成功一次即清零;达 `max_consecutive_failures`(3)→ `self.paused = True`
- 暂停期间 should_compress 返回 False;暂停状态随新 Agent 实例复位
- CLI 消息:`🛑 压缩已临时暂停(连续 3 次失败),本会话不再自动压缩,新会话恢复`

### 6.5 事件日志(logs/compression.jsonl)

每次压缩(无论成败)追加一行,JSONL 格式:

```json
{"ts": "2026-08-07T01:20:00+08:00", "session": "ab12...", "trigger_tokens": 45210,
 "strategy": "L1", "folded": 3, "covers": 0, "chars_before": 88421, "chars_after": 29310,
 "success": false, "error_type": "summary_timeout", "error_msg": "connect timed out",
 "duration_ms": 61000}
```

用途:复盘"压缩到底发生了什么"、统计失败率、调阈值。

落库/归档失败同样写进事件(不吞掉):`error_type="persist_failed"`。
原则:**一行 print 警告不可追溯,事件日志里的 error_type 才可统计**——
这样"落库失败率"和"压缩降级率(summary_timeout)"都能从日志直接数出来,
而不是靠"感觉是否常见"。

### 6.6 CLI 可见性

```
成功: 🗜️ 压缩完成: 88.4K chars → 29.3K (L1 折叠 3 条 + 摘要 covers 42),耗时 1.2s
降级: ⚠️ 摘要失败(timeout,已重试 1 次)→ 回退 ToolResult 折叠,本轮不摘要
跳过: ⚠️ 压缩失败(invalid_history),已回滚,对话继续(详情见 logs/compression.jsonl)
暂停: 🛑 压缩已临时暂停(连续 3 次失败),本会话不再自动压缩,新会话恢复
```

### 6.7 core.py 集成(见 Step 4)

## 7. 测试策略

### 7.1 mini 事实召回集(8 题)

| # | 场景 | 对话里埋的事实 | 压缩后提问 | 期望答案 |
|---|------|--------------|-----------|---------|
| 1 | 文件路径 | "写到 D:/Engineering/dummy/output.txt" | 输出路径? | D:/Engineering/dummy/output.txt |
| 2 | 代码细节 | "用 requests,超时 30s" | 库?超时? | requests, 30 |
| 3 | 用户偏好 | "以后回复都用中文" | 语言偏好? | 中文 |
| 4 | 决定 | "决定先做 ToolResult 折叠" | 先做什么? | ToolResult 折叠 |
| 5 | 数字 | "预算 500 元" | 预算? | 500 |
| 6 | 未完成事项 | "待办:还差 FTS" | 未完成事项? | FTS |
| 7 | 工具结果 | 2000 字日志含 "ERROR: port 8080" | 错误信息? | port 8080 |
| 8 | 跨轮次 | 第 3 轮用户名,第 40 轮提问 | 用户名? | 记忆中的值 |

断言用包含匹配,不用精确匹配。

### 7.2 容错用例(5 个)

1. 摘要抛 timeout → 断言降级 L1-only、history 未被污染、事件日志 success=false
2. 摘要返回 None → 断言丢弃、不重试、降级 L1-only
3. 连续失败 3 次 → 断言断路器暂停、should_compress 返回 False
4. 构造非法压缩结果(未配对 tool_calls)→ 断言校验拦截、回滚原历史
5. 事件日志:文件存在、JSON 可解析、字段齐全

## 8. 实现时待定决策点

| # | 决策点 | 默认 | 何时调整 |
|---|--------|------|---------|
| 1 | window_tokens | 64000 | 按 DeepSeek 实际模型上下文调整,做成配置 |
| 2 | threshold_ratio | 0.7 | 冒烟后按实测触发频率调 |
| 3 | 摘要 prompt 措辞 | 强制"路径/数字/名称原样保留" | 召回测试不过时优先改这里 |
| 4 | 每 5 次基于磁盘原文重建摘要 | 防误差累积 | 摘要漂移出现时提前 |
| 5 | 断路器阈值 | 3 次 | 频繁瞬断网络环境可调大 |

## 9. 风险与对策

| 风险 | 对策 |
|------|------|
| API 拒收多余字段 | strip_meta(Step 6) |
| 摘要丢精确值 | 测试集拦截(Step 7)+ prompt 强制保留 |
| 压缩时机不对 | 只在调 LLM 前、绝不在工具循环中途 |
| 摘要调用失败挂掉主流程 | 降级阶梯 + 断路器(第 6 节) |
| 压缩后配对被切断 | _validate_history 回滚(Step 6) |
| 落库失败丢状态 | 单独 try,内存为准,下次重写 |

## 10. 规模估计

- 总工作量:约 5-7 小时(含测试与冒烟)
- Step 1-4(骨架 + L1 + 触发):一次 session 完成
- Step 5(L2 + 容错):核心,单独一次 session
- Step 7(测试集):质量门,最花时间但最值得

> 配套文档:docs/context-compression.md(设计决策 D1-D7)
> 调研背景:docs/memory-research.md、docs/agent-memory-report.html(08 节)
