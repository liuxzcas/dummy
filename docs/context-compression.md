# Context Compression — 设计文档 (Phase 2.2)

> EN TL;DR: This doc is the blueprint for Phase 2.2 (context compression) of
> dummy-agent. It covers: why we only do application-layer compression (we call
> the DeepSeek API, we cannot touch the KV cache), a two-stage strategy
> (tool-result folding first — zero LLM cost, then incremental LLM summarization),
> exact message formats (with `_meta` tags stripped before hitting the API),
> token accounting via `usage.prompt_tokens` instead of heuristics, a lossless
> design (full history stays on disk in SQLite, only the in-memory history is
> compressed), metrics, a mini fact-recall test set, and an ordered
> implementation plan.

## 1. 目标与范围

目标:让 Agent 在长对话中不因上下文超限而失败——当历史接近模型窗口上限时,
自动压缩早期内容,保留关键信息,同时保证:

1. 压缩后 history 仍是合法的 OpenAI messages 格式(DeepSeek API 严格)
2. 压缩可逆:磁盘(SQLite)永远保留全文,内存压缩不丢数据
3. 压缩有衡量:事实召回率、压缩率、成本、延迟都可量化

不在范围内(明确不做):

- KV cache 压缩(StreamingLLM / H2O / SnapKV / PyramidKV 等)—— 那是 serving 层技术,
  调用 DeepSeek API 无法控制,只作背景了解,见 §11
- 跨 Session 记忆(Phase 2.4)—— 依赖本阶段先落地
- 语义搜索(FTS5,Phase 2.3b)

## 2. 背景:为什么只做应用层压缩

Context compression 分两条技术线:

| 层 | 技术 | 控制方 | 对 dummy-agent |
|----|------|--------|----------------|
| 推理层 | KV cache 压缩(attention 缓存裁剪) | 模型 serving 引擎 | ❌ 调 API,不可控 |
| 应用层 | prompt/history 压缩(发给 LLM 的文本) | 我们自己 | ✅ 本阶段目标 |

应用层压缩的本质:把"发给 LLM 的历史消息"从全量换成"摘要 + 最近原文"。
这是在消息层面做的,不碰模型内部。

## 3. 总体架构与数据流

```
                     ┌─────────────────────────────────────────┐
                     │              DummyAgent (core.py)        │
  用户输入 ──► chat() │                                         │
                     │  history(内存, 可能含摘要消息+_meta)       │
                     │       │                                 │
                     │       ▼ 每次循环迭代前检查                │
                     │  maybe_compress(history) ──► 超阈值?     │
                     │       │ 是                              │
                     │       ▼                                 │
                     │  ContextCompressor                      │
                     │   1. ToolResult 折叠(无 LLM,零成本)      │
                     │   2. 增量摘要(旧摘要+新块 → 新摘要)       │
                     │       │                                 │
                     │       ▼ _strip_meta(剥离 _meta 字段)     │
                     │  llm.chat(合法 messages) ──► usage       │
                     │       │                                 │
                     │       ▼ _persist_history()              │
                     │  SessionStore(SQLite 全文,永不删除)      │
                     └─────────────────────────────────────────┘
```

关键点:压缩只发生在"内存 history"这一层;SQLite 里的全文是 ground truth,
永不因压缩而丢失。`/resume` 恢复的也是全文。

## 4. 设计决策

### D1: 触发时机 = chat() 工具循环的每次迭代入口

- 位置:工具循环 `for turn in range(MAX_TOOL_TURNS)` 顶部、每次调 LLM 之前
- 理由:历史在工具调用过程中也会增长(每条 tool 消息都可能很大),
  只在 chat() 入口检查挡不住循环内的增长
- 不要在工具循环中间"边执行边压缩"——压缩只发生在"即将调 LLM"这个静止点

### D2: Token 计数用 API 返回的 usage,不用估算

- `llm.chat()` 返回后,把 `response.usage` 存到 `self.llm.last_usage`
- `usage.prompt_tokens` 就是"模型这次实际看到的上下文大小"(含 system + 工具定义),
  这是最精确的计数,比任何字符/词估算都可靠
- 触发条件:`last_usage.prompt_tokens > WINDOW_TOKENS * THRESHOLD_RATIO`
- WINDOW_TOKENS 做成配置常量(DeepSeek-V3 按 64K 起步,以模型实际为准);
  THRESHOLD_RATIO 默认 0.7,留 30% 余量给单次工具结果和摘要消息本身

### D3: 两级压缩,先做零成本的,再做贵的

| 级别 | 做什么 | LLM 开销 | 风险 |
|------|--------|---------|------|
| L1 ToolResult 折叠 | 超长 tool 结果截断(保留头尾+标记) | 无 | 极低(只改 content,不动结构) |
| L2 增量摘要 | 早期对话压成一条摘要消息 | 每次压缩 1 次调用 | 中(信息丢失/幻觉) |

L1 先实现、先验证;L2 在 L1 之上叠加。两者独立可开关。

### D4: 摘要消息格式与 _meta 约定

```json
{
  "role": "user",
  "content": "[早期对话摘要]\n<摘要正文>",
  "_meta": {
    "compressed": true,
    "covers": 42,
    "created_at": "2026-08-05T16:00:00+08:00"
  }
}
```

- role 用 `user` + 前缀标记,不用 `system`:避免和真正的 system prompt
  (Agent 身份指令)混淆,也避免模型把摘要当指令执行
- `_meta.covers` = 这条摘要替代了多少条原始消息(不是轮数)。下次增量摘要时,
  只摘要"摘要之后到 cut-off 之间的消息",新 covers = 旧 covers + 新块消息数
- 所有自定义字段收在 `_meta` 一个键下;**发 API 前必须剥离**(见 D6)

### D5: 压缩边界规则——只在 user 消息前切

- 摘要 cut-off 点只能选在"某条 user 消息之前"
- 理由:每条 user 消息开启一轮完整交换(user→assistant→tool…→assistant),
  在 user 边界切,天然保证 tool_calls 和 tool 消息成对完整,
  不会出现"assistant 有 tool_calls 但结果丢了"这种非法序列
- 保留最近 N 轮原文(N 默认 6,按 user 消息计数)

### D6: _meta 与 API 的隔离

- 内存 history 和 SQLite payload 允许 `_meta` 字段
- 发给 LLM 前调用 `_strip_meta(history)`:过滤掉所有 `_meta` 键
- 理由:DeepSeek API 对消息 schema 严格,未知顶层字段可能被拒;
  本项目已经吃过 JSON 格式的亏(d6e1fb3 的 JSON 解析加固),不要在
  请求体里赌兼容性

### D7: 压缩与持久化联动

- 压缩后内存 history 变短 → 照常调 `_persist_history()` 全量重写
- save_history 的"逐条 upsert + 删除超出新长度的尾部"逻辑正好承接
  历史缩水场景(7d60e11 已就绪)
- 摘要消息本身也入库(带 _meta),`/resume` 恢复后格式依然合法

## 5. 模块设计:compressor.py(代码骨架)

```python
"""
compressor.py — 上下文压缩模块 (Phase 2.2)

职责:
1. 判断是否需要压缩(基于 API 返回的 usage.prompt_tokens)
2. 执行两级压缩:ToolResult 折叠 + 增量摘要
3. 保证输出始终是合法 OpenAI messages 格式

设计要点:
- 压缩只作用于"内存 history";SQLite 全文保留,压缩可逆
- 只在 user 消息边界切割,保证 tool_calls/tool 配对完整
- 自定义字段收在 _meta 下,发 API 前由 _strip_meta 剥离
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CompressionConfig:
    window_tokens: int = 64000        # 模型上下文窗口(DeepSeek-V3 起步值,按实际模型调整)
    threshold_ratio: float = 0.7      # 触发阈值:窗口的 70%
    recent_turns_keep: int = 6        # 保留最近 N 轮原文(按 user 消息计数)
    tool_result_max_chars: int = 600  # tool 结果折叠阈值(超过才截断)
    tool_result_keep_head: int = 200  # 折叠时保留的头部字符数
    tool_result_keep_tail: int = 100  # 折叠时保留的尾部字符数
    summary_prompt: str = field(
        default=(
            "你负责压缩对话历史。以下是旧摘要和新增对话:\n\n"
            "[旧摘要]\n{old_summary}\n\n[新增对话]\n{new_block}\n\n"
            "输出更新后的摘要,要求:\n"
            "1. 保留:用户目标、已做的决定、文件路径、代码细节、用户偏好、未完成事项\n"
            "2. 精确信息(数字/路径/名称)必须原样保留,不要概括\n"
            "3. 丢弃寒暄和过程性内容\n"
            "4. 直接输出摘要正文,不要解释\n"
        )
    )


class ContextCompressor:
    """两级压缩器:ToolResult 折叠(零成本) + 增量摘要(LLM)。"""

    def __init__(self, llm: Any, config: Optional[CompressionConfig] = None):
        self.llm = llm
        self.config = config or CompressionConfig()

    # ---------------------------------------------------------------
    # 触发判断
    # ---------------------------------------------------------------
    def should_compress(self, last_prompt_tokens: Optional[int]) -> bool:
        """基于最近一次 API 调用的 usage.prompt_tokens 判断。"""
        if last_prompt_tokens is None:
            return False
        return last_prompt_tokens > self.config.window_tokens * self.config.threshold_ratio

    # ---------------------------------------------------------------
    # 主入口:压缩 history,返回新的合法 messages
    # ---------------------------------------------------------------
    def compress(self, history: list[dict]) -> list[dict]:
        history = self._fold_tool_results(history)   # L1: 零成本,先做
        history = self._summarize_prefix(history)    # L2: LLM 增量摘要
        return history

    # ---------------------------------------------------------------
    # L1: ToolResult 折叠 —— 只改 tool 消息的 content,不动结构
    # ---------------------------------------------------------------
    def _fold_tool_results(self, history: list[dict]) -> list[dict]:
        folded = 0
        for msg in history:
            if msg.get("role") == "tool":
                content = msg.get("content")
                if isinstance(content, str) and len(content) > self.config.tool_result_max_chars:
                    cfg = self.config
                    msg["content"] = (
                        content[: cfg.tool_result_keep_head]
                        + f"\n...[ToolResult 已截断: 原文 {len(content)} 字符, "
                          f"完整内容见 SQLite session.db]...\n"
                        + content[-cfg.tool_result_keep_tail :]
                    )
                    folded += 1
        return history

    # ---------------------------------------------------------------
    # L2: 增量摘要 —— 旧摘要 + 新块 → 新摘要
    # ---------------------------------------------------------------
    def _summarize_prefix(self, history: list[dict]) -> list[dict]:
        # 找到已存在的摘要消息(如果有),以及它 covers 的消息数
        summary_idx = next(
            (i for i, m in enumerate(history) if m.get("_meta", {}).get("compressed")),
            None,
        )
        old_covers = history[summary_idx]["_meta"]["covers"] if summary_idx is not None else 0
        old_summary = history[summary_idx]["content"] if summary_idx is not None else ""

        # 找 cut-off 点:保留最近 N 轮原文,cut 在 user 消息边界
        user_idx = [i for i, m in enumerate(history) if m.get("role") == "user"]
        if len(user_idx) <= self.config.recent_turns_keep:
            return history  # 还没到需要摘要的长度

        cut_at = user_idx[-self.config.recent_turns_keep]

        # 待摘要块 = 摘要消息之后 .. cut_at 之前(全是旧内容)
        start = (summary_idx + 1) if summary_idx is not None else 0
        block = history[start:cut_at]
        if not block:
            return history

        new_summary = self._call_summary_llm(old_summary, block)
        covers = old_covers + len(block)

        # 新 history = [system...] + [摘要消息] + 最近 N 轮原文
        # 注意:摘要消息插在原 system prompt 之后、最近原文之前
        system_msgs = [m for m in history[:start] if m.get("role") == "system"]
        summary_msg = {
            "role": "user",
            "content": f"[早期对话摘要]\n{new_summary}",
            "_meta": {"compressed": True, "covers": covers,
                      "created_at": self._now_iso()},
        }
        return system_msgs + [summary_msg] + history[cut_at:]

    def _call_summary_llm(self, old_summary: str, block: list[dict]) -> str:
        """调 LLM 生成(增量)摘要。块消息转成紧凑文本再喂。"""
        block_text = "\n".join(
            f"[{m.get('role')}] {json.dumps(m.get('content'), ensure_ascii=False)}"
            for m in block
        )
        prompt = self.config.summary_prompt.format(
            old_summary=old_summary or "(无旧摘要)",
            new_block=block_text,
        )
        # 用低 temperature + 较短 max_tokens,摘要不需要创造力
        resp = self.llm.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1024,
        )
        return resp.content or ""

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _strip_meta(history: list[dict]) -> list[dict]:
    """剥离 _meta 字段,产出可发给 API 的合法 messages。

    注意:这是浅拷贝;不要原地删,避免污染内存里带元数据的 history。
    """
    cleaned = []
    for msg in history:
        if "_meta" in msg:
            msg = {k: v for k, v in msg.items() if k != "_meta"}
        cleaned.append(msg)
    return cleaned
```

## 6. core.py 集成点(改动清单)

| 文件 | 改动 | 说明 |
|------|------|------|
| llm.py | `chat()` 末尾加 `self.last_usage = response.usage` | 暴露精确 token 计数 |
| core.py | `__init__` 里创建 `self.compressor = ContextCompressor(self.llm)` | 依赖注入 |
| core.py | 工具循环顶部:`if self.compressor.should_compress(self.llm.last_usage.prompt_tokens if self.llm.last_usage else None): self.history = self.compressor.compress(self.history)` | 触发点 |
| core.py | 调 `self.llm.chat(...)` 前:`messages = _strip_meta(self.history)` | API 隔离 |
| core.py | 压缩后照常 `_persist_history()` | 缩水场景已支持 |

注意:压缩只发生在"即将调 LLM"的静止点;`_persist_history()` 在压缩后、
调用前调用一次即可,避免中间状态落库。

## 7. 衡量指标

效率指标(每次压缩可测,便宜):

| 指标 | 公式 | 目标参考 |
|------|------|---------|
| 压缩率 | 1 - compressed_tokens / original_tokens | ≥ 50%(L1+L2 通常 60-80%) |
| 每次会话 token 节省 | 压缩前累计 prompt_tokens - 压缩后 | 记录即可 |
| 压缩延迟 | 摘要调用耗时 | < 3s(DeepSeek) |
| 触发频率 | 多少次 chat 触发一次压缩 | 长对话 1-2 次/会话 |

质量指标(贵,必须做):

| 指标 | 怎么测 | 目标 |
|------|--------|------|
| 事实召回率 | mini 测试集(§8) | ≥ 90% |
| 摘要幻觉率 | LLM-as-judge 或人工:摘要含原文没有的信息 | < 5% |
| 任务完成率 | 同一批工具任务,压缩前 vs 压缩后 | 不掉点 |
| 格式合法性 | 压缩后 history 直接调 API 不报错 | 100% |

## 8. mini 事实召回测试集(模板)

建 `tests/test_compression.py`,每次改压缩策略必跑。题目覆盖真实场景:

| # | 场景 | 对话里埋的事实 | 压缩后提问 | 期望答案 |
|---|------|---------------|-----------|---------|
| 1 | 文件路径 | "把结果写到 D:/Engineering/dummy/output.txt" | 输出文件路径? | D:/Engineering/dummy/output.txt |
| 2 | 代码细节 | "用 requests 库,超时设 30s" | 用什么库?超时多少? | requests, 30 |
| 3 | 用户偏好 | "以后回复都用中文,代码注释也要中文" | 语言偏好? | 中文 |
| 4 | 决定 | "我们决定 Phase 2.2 先做 ToolResult 折叠" | 先做什么? | ToolResult 折叠 |
| 5 | 数字 | "预算 500 元,token 单价 2 元/M" | 预算多少? | 500 |
| 6 | 未完成事项 | "待办:还差 FTS 没做" | 未完成事项? | FTS |
| 7 | 工具结果 | terminal 返回了 2000 字符的日志,内含 "ERROR: port 8080" | 错误信息? | port 8080 |
| 8 | 跨轮次 | 第 3 轮提到用户名,第 40 轮问用户名 | 用户名? | 记忆中的值 |

测试方法:构造 N 轮对话 → 强制压缩 → 用压缩后的 history 问问题(LLM 或直接查
摘要文本)→ 比对答案。断言用包含匹配,不用精确匹配。

## 9. 注意事项清单(坑)

1. 摘要丢精确值(路径/数字/名称)→ 摘要 prompt 明确要求原样保留(§5 已内置)
2. tool_calls/tool 配对被切断 → 只在 user 边界切(§4 D5)
3. 摘要消息被模型当指令 → role 用 user + 前缀标记(§4 D4)
4. 自定义字段被 API 拒 → 发请求前 _strip_meta(§4 D6)
5. 反复摘要的误差累积 → 增量摘要,但每 5 次基于磁盘原文重建一次
6. 在工具循环中途压缩 → 只在调 LLM 前压缩(§4 D1)
7. 摘要调用失败导致主流程挂掉 → 压缩失败必须降级(跳过压缩,继续对话)
8. 触发阈值太高(99%)→ 单次工具结果就可能撑爆窗口,留 30% 余量(§4 D2)
9. 压缩后忘了持久化 → 压缩是状态变更,必须落库
10. 恢复的 history 带 _meta → /resume 后同样要 _strip_meta 再发 API

## 10. 实施路线(子任务顺序)

1. [ ] llm.py:暴露 last_usage
2. [ ] compressor.py:config + should_compress + ToolResult 折叠(L1)
3. [ ] core.py:触发点接入,压缩后 _persist_history
4. [ ] tests/test_compression.py:写 L1 的测试(#7 场景)
5. [ ] compressor.py:L2 增量摘要(_summarize_prefix + _call_summary_llm)
6. [ ] _strip_meta 接入 core.py 的 API 调用路径
7. [ ] 测试:补全 §8 全部场景 + 压缩失败降级路径
8. [ ] 真实对话冒烟:长对话跑到触发,人工检查召回
9. [ ] roadmap.md 更新状态,记录实测压缩率/延迟

## 11. 参考

论文/项目:

- LLMLingua(EMNLP 2023)/ LongLLMLingua / LLMLingua-2(ACL 2024)— 小模型 prompt 压缩
- Recursively Summarizing Enables Long-Term Dialogue Memory(EMNLP 2023 Findings)— 滚动摘要
- MemGPT / Letta(arXiv:2310.08560)— OS 式分层记忆(Phase 2.4 参考)
- Mem0(arXiv:2504.19413)、Zep / Graphiti(arXiv:2501.13956)— 记忆层实现
- LongMemEval(ICLR 2025,arXiv:2410.10813)— 长程记忆评测基准
- "Hold Onto That Thought"(arXiv:2512.12008)— KV cache 压缩在推理任务上的掉点评测
- Microsoft Agent Framework 文档:Compaction(三种策略:ToolResult / Summarization / SlidingWindow)

基准:LongBench / LongBench-v2(ZhipuAI,中英双语,21 任务)

> 注:本阶段只做应用层压缩;KV cache 类方法(serving 层)仅作背景,见 §2。
