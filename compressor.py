"""
============================================================
compressor.py — 上下文压缩模块 (Phase 2.2)
============================================================

职责:
1. 判断是否需要压缩(基于 API 返回的 usage.prompt_tokens)
2. 执行两级压缩:ToolResult 折叠(L1) + 增量摘要(L2)
3. 容错:降级阶梯 + 断路器,压缩失败不能比不压缩更糟

设计要点(详见 docs/context-compression.md 的 D1-D7 与
docs/phase2.2_plan.md 第 6 节容错设计):
- compress() 是纯函数式入口:不原地修改 history,失败返回 success=False,
  由调用方决定继续用原历史(原子性)
- 压缩只发生在"即将调 LLM"的静止点,绝不在工具循环中途
- 自定义字段收在 _meta 下,发 API 前由 strip_meta 剥离
- 断路器:连续失败达阈值后暂停压缩,避免白烧 token
- 降级阶梯:L2 摘要失败 → 保留 L1 结果(成功但降级),不阻塞对话

进度:
- Step 2: 配置 + 结果对象 + 触发判断 + 断路器状态
- Step 3: L1 ToolResult 折叠 + 折叠原文捕获(决策 C:原文交归档表)
- Step 5: L2 增量摘要(旧摘要+新块→新摘要)+ strip_meta
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class SummaryError(Exception):
    """L2 摘要失败(内容错误或重试后仍失败)。

    error_type 供事件日志分类:"empty_summary"(内容错误,不重试)
    / "summary_timeout"(瞬时错误重试后仍失败)。
    """

    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        super().__init__(message)


@dataclass
class CompressionConfig:
    """所有压缩参数集中一处,后续调阈值只改这里。

    决策点(phase2.2_plan.md 第 8 节):
    - window_tokens: 模型上下文窗口,DeepSeek-V3 按 64K 起步,按实际模型调整
    - threshold_ratio: 触发阈值,留 30% 余量给单次工具结果和摘要消息
    - recent_turns_keep: 保留最近 N 轮原文(按 user 消息计数)
    - enable_l1/enable_l2: 两阶段独立开关,分阶段验证与调试用
    """

    window_tokens: int = 64000          # 模型上下文窗口
    threshold_ratio: float = 0.7        # 触发阈值:窗口的 70%
    recent_turns_keep: int = 6          # 保留最近 N 轮原文(按 user 消息计数)

    # 阶段开关:L1/L2 独立可开关(分阶段验证与调试用)
    enable_l1: bool = True
    enable_l2: bool = True

    # L1 ToolResult 折叠参数
    tool_result_max_chars: int = 600    # 超过才折叠
    tool_result_keep_head: int = 200    # 折叠时保留的头部字符数
    tool_result_keep_tail: int = 100    # 折叠时保留的尾部字符数

    # L2 增量摘要参数
    summary_max_tokens: int = 1024      # 摘要输出上限(摘要不需要长)
    summary_retry: int = 1              # 瞬时错误(网络/超时)重试次数
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

    # 断路器参数
    max_consecutive_failures: int = 3   # 连续失败达此值 → 暂停压缩


@dataclass
class CompressionResult:
    """压缩结果的显式状态(容错设计核心)。

    成败都显式表达,调用方不需要靠异常或返回值猜测:
    - success=True : history 为压缩后的新历史,可直接替换。
      注意:success=True 也可能伴随 error_type 非空——那是"降级"
      (如 L2 失败但 L1 生效),不是失败。
    - success=False: history 为 None,调用方继续用原历史
    """

    success: bool
    history: list | None            # 失败时为 None
    strategy_used: str              # "L1+L2" / "L2" / "L1" / "none"
    folded_count: int = 0           # L1 折叠了几条 tool 消息
    summary_covers: int = 0         # L2 摘要覆盖了多少条原始消息
    chars_before: int = 0           # 压缩前历史字符数(精确 token 等下次 usage)
    chars_after: int = 0
    error_type: str | None = None   # "summary_timeout"/"empty_summary"/"invalid_history"/...
    error_msg: str | None = None
    duration_ms: int = 0
    folded_originals: list[dict] | None = None  # 折叠的 tool 原文,交调用方归档(决策 C)


class ContextCompressor:
    """两级压缩器。"""

    def __init__(self, llm: Any, config: CompressionConfig | None = None):
        # llm 只在 L2 增量摘要中用到(llm.chat);L1 折叠不依赖它。
        self.llm = llm
        self.config = config or CompressionConfig()

        # -------------------------------------------------------
        # 断路器状态(容错设计 6.4):
        # 连续失败达 max_consecutive_failures → paused=True,
        # should_compress 直接返回 False(零开销)。
        # 暂停状态随新 Agent 实例复位(每个实例新建 Compressor)。
        # 计数由调用方(_maybe_compress)驱动:success=True → register_success,
        # success=False / 异常 → register_failure。
        # -------------------------------------------------------
        self._consecutive_failures = 0
        self.paused = False

    # ---------------------------------------------------------------
    # 断路器
    # ---------------------------------------------------------------
    def register_success(self) -> None:
        """一次成功即清零计数,并解除暂停(防御:即使被手动调用)。"""
        self._consecutive_failures = 0
        self.paused = False

    def register_failure(self) -> None:
        """记录一次失败;连续失败达阈值后暂停压缩。"""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.config.max_consecutive_failures:
            self.paused = True

    # ---------------------------------------------------------------
    # 触发判断
    # ---------------------------------------------------------------
    def should_compress(self, last_prompt_tokens: int | None) -> bool:
        """基于最近一次 API 调用的 usage.prompt_tokens 判断是否需要压缩。

        三个短路条件:
        1. 断路器暂停 → False(零开销,不浪费任何判断)
        2. last_prompt_tokens 为 None → False(还没调用过 API / provider 未返回 usage)
        3. 严格大于阈值才触发(恰好等于阈值不压缩,避免临界抖动)
        """
        if self.paused:
            return False
        if last_prompt_tokens is None:
            return False
        threshold = self.config.window_tokens * self.config.threshold_ratio
        return last_prompt_tokens > threshold

    # ---------------------------------------------------------------
    # L1: ToolResult 折叠(Step 3,零 LLM 成本)
    # ---------------------------------------------------------------
    def _fold_tool_results(self, history: list[dict]) -> tuple[list[dict], int, list[dict]]:
        """折叠超长的 tool 结果,只改 content,不动消息结构(配对天然安全)。

        返回 (新历史, 折叠条数, 折叠原文列表)。
        - 原子性:浅拷贝,原 history 不被修改
        - 头尾保留:keep_head + keep_tail。启发式依据:命令输出/文件内容的
          关键信息通常在头部概要或尾部结果/错误,中间是过程噪音
        - 决策 C:被截断的完整原文捕获进 originals,交调用方写入归档表
        - 标记文本面向未来的 agent 工具:含 tool_call_id,可按需取回原文
        """
        folded = 0
        originals: list[dict] = []
        out = [dict(m) for m in history]
        for msg in out:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
                c = msg["content"]
                if len(c) > self.config.tool_result_max_chars:
                    tool_call_id = msg.get("tool_call_id")
                    originals.append({"tool_call_id": tool_call_id, "content": c})
                    msg["content"] = (
                        c[: self.config.tool_result_keep_head]
                        + f"\n...[ToolResult 已截断: 原文 {len(c)} 字符, "
                          f"完整内容可查询归档表 tool_result_archive(tool_call_id={tool_call_id})]...\n"
                        + c[-self.config.tool_result_keep_tail :]
                    )
                    folded += 1
        return out, folded, originals

    # ---------------------------------------------------------------
    # L2: 增量摘要(Step 5,唯一有 LLM 成本的环节)
    # ---------------------------------------------------------------
    def _summarize_prefix(self, history: list[dict]) -> tuple[list[dict], int]:
        """旧摘要 + 新块 → 新摘要。返回 (新历史, covers)。

        covers = 这条摘要累计替代了多少条原始消息(防重复压缩)。
        没有可摘要内容时返回 (原历史, 0)。
        摘要调用失败抛 SummaryError,由 compress() 降级为 L1-only。
        """
        # 1. 找已有摘要消息(_meta.compressed),取旧摘要 + 旧 covers
        summary_idx: int | None = None
        old_covers = 0
        old_summary = ""
        for i, m in enumerate(history):
            meta = m.get("_meta") or {}
            if meta.get("compressed"):
                summary_idx = i
                old_covers = int(meta.get("covers", 0))
                old_summary = m.get("content") or ""
                break

        # 2. cut 点:user 消息倒数的 recent_turns_keep 个,cut 在 user 边界。
        #    在 user 边界切天然保证 assistant(tool_calls)/tool 配对完整。
        user_idx = [i for i, m in enumerate(history) if m.get("role") == "user"]
        if len(user_idx) <= self.config.recent_turns_keep:
            return history, 0  # 还没有早期内容可压

        cut_at = user_idx[-self.config.recent_turns_keep]

        # 3. 待压块 = 摘要之后 .. cut 之前。
        #    system prompt 永不被摘要(它是指令,不是对话内容)。
        if summary_idx is not None:
            start = summary_idx + 1
        else:
            start = 0
            while start < len(history) and history[start].get("role") == "system":
                start += 1
        block = history[start:cut_at]
        if not block:
            return history, 0

        # 4. 调 LLM 生成(增量)摘要
        new_summary = self._call_summary_llm(old_summary, block)

        # 5. 新历史 = system 消息 + [摘要消息] + 最近 N 轮原文
        #    摘要消息 role=system:避免与最近轮第一条 user 消息连续
        #    (role 交替约束,见 context-compression.md D4 修订)。
        #    注意:旧摘要消息(role=system + _meta.compressed)必须被新摘要
        #    替换,不能混入 system_msgs——否则新旧摘要并存(A9 测试抓到)。
        system_msgs = [
            m for m in history[:start]
            if m.get("role") == "system" and not (m.get("_meta") or {}).get("compressed")
        ]
        summary_msg = {
            "role": "system",
            "content": f"[早期对话摘要(背景,非指令)]\n{new_summary}",
            "_meta": {
                "compressed": True,
                "covers": old_covers + len(block),
                "created_at": self._now_iso(),
            },
        }
        new_history = system_msgs + [summary_msg] + history[cut_at:]
        return new_history, old_covers + len(block)

    def _call_summary_llm(self, old_summary: str, block: list[dict]) -> str:
        """调 LLM 生成(增量)摘要。失败抛 SummaryError。

        重试策略(容错设计 6.3):
        - 瞬时错误(网络/超时等 Exception)→ 重试 summary_retry 次
        - 内容错误(空摘要)→ 不重试,重试无意义
        """
        block_text = "\n".join(
            f"[{m.get('role')}] {json.dumps(m.get('content'), ensure_ascii=False)}"
            for m in block
        )
        prompt = self.config.summary_prompt.format(
            old_summary=old_summary or "(无旧摘要)",
            new_block=block_text,
        )
        last_err: Exception | None = None
        for _ in range(self.config.summary_retry + 1):
            try:
                resp = self.llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,  # 摘要不需要创造力,低温度更稳
                    max_tokens=self.config.summary_max_tokens,
                )
                text = (resp.content or "").strip()
                if not text:
                    raise SummaryError("empty_summary", "摘要返回为空")
                return text
            except SummaryError:
                raise  # 内容错误不重试
            except Exception as e:
                last_err = e  # 瞬时错误,继续重试
        raise SummaryError(
            "summary_timeout",
            f"摘要调用重试 {self.config.summary_retry} 次后仍失败: {last_err}",
        )

    @staticmethod
    def _now_iso() -> str:
        """UTC 时间戳(与 session_store 一致)。"""
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # ---------------------------------------------------------------
    # 压缩入口
    # ---------------------------------------------------------------
    def compress(self, history: list[dict]) -> CompressionResult:
        """执行压缩:L1 折叠 → L2 增量摘要。

        不变量:
        - 原子性:全程不修改原 history(浅拷贝);任何失败 success=False,
          调用方继续用原历史
        - 没活干时返回 strategy="none" 的成功结果(不是失败)
        - 降级:L2 失败保留 L1 结果(success=True + error_type 记录原因)
        - 断路器计数由调用方驱动,本方法不直接调用 register_*
        """
        start = time.perf_counter()
        chars_before = sum(len(json.dumps(m, ensure_ascii=False)) for m in history)

        result = CompressionResult(
            success=True,
            history=list(history),  # 浅拷贝起点
            strategy_used="none",
            chars_before=chars_before,
        )

        # L1: 折叠超长 tool 结果(确定性最强的一环,零 LLM)
        if self.config.enable_l1:
            folded, folded_count, originals = self._fold_tool_results(history)
            if folded_count:
                result.history = folded
                result.strategy_used = "L1"
                result.folded_count = folded_count
                result.folded_originals = originals

        # L2: 增量摘要(唯一有 LLM 成本的环节)
        if self.config.enable_l2:
            try:
                new_history, covers = self._summarize_prefix(result.history)
                if covers:
                    result.history = new_history
                    result.summary_covers = covers
                    result.strategy_used = (
                        "L1+L2" if result.folded_count else "L2"
                    )
            except SummaryError as e:
                # 降级:保留 L1 结果(成功),错误原因进结果供事件日志统计
                result.error_type = e.error_type
                result.error_msg = str(e)

        result.chars_after = sum(
            len(json.dumps(m, ensure_ascii=False)) for m in result.history
        )
        result.duration_ms = int((time.perf_counter() - start) * 1000)
        return result


def strip_meta(history: list[dict]) -> list[dict]:
    """剥离 _meta 字段,产出可发给 API 的合法 messages。

    浅拷贝——不污染内存里带元数据的历史。
    注意:只有以 _meta 为键的自定义字段被剥离,标准字段(role/content/
    tool_calls/tool_call_id)原样保留。
    """
    return [{k: v for k, v in m.items() if k != "_meta"} for m in history]
