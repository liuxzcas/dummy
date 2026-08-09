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

本文件当前为 Step 2 骨架:配置 + 结果对象 + 触发判断 + 断路器状态。
L1 折叠(L1)与增量摘要(L2)分别在后续 Step 3 / Step 5 实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CompressionConfig:
    """所有压缩参数集中一处,后续调阈值只改这里。

    决策点(phase2.2_plan.md 第 8 节):
    - window_tokens: 模型上下文窗口,DeepSeek-V3 按 64K 起步,按实际模型调整
    - threshold_ratio: 触发阈值,留 30% 余量给单次工具结果和摘要消息
    - recent_turns_keep: 保留最近 N 轮原文(按 user 消息计数)
    """

    window_tokens: int = 64000          # 模型上下文窗口
    threshold_ratio: float = 0.7        # 触发阈值:窗口的 70%
    recent_turns_keep: int = 6          # 保留最近 N 轮原文(按 user 消息计数)

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
    - success=True : history 为压缩后的新历史,可直接替换
    - success=False: history 为 None,调用方继续用原历史
    """

    success: bool
    history: list | None            # 失败时为 None
    strategy_used: str              # "L1+L2" / "L1" / "none"
    folded_count: int = 0           # L1 折叠了几条 tool 消息
    summary_covers: int = 0         # L2 摘要覆盖了多少条原始消息
    chars_before: int = 0           # 压缩前历史字符数(精确 token 等下次 usage)
    chars_after: int = 0
    error_type: str | None = None   # "summary_timeout"/"empty_summary"/"invalid_history"/...
    error_msg: str | None = None
    duration_ms: int = 0


class ContextCompressor:
    """两级压缩器。Step 2 只提供骨架与触发判断,压缩逻辑后续实现。"""

    def __init__(self, llm: Any, config: CompressionConfig | None = None):
        # llm 目前只是占位:Step 2 的触发判断不依赖它,
        # Step 5 的增量摘要才真正调用(llm.chat)。
        self.llm = llm
        self.config = config or CompressionConfig()

        # -------------------------------------------------------
        # 断路器状态(容错设计 6.4):
        # 连续失败达 max_consecutive_failures → paused=True,
        # should_compress 直接返回 False(零开销)。
        # 暂停状态随新 Agent 实例复位(每个实例新建 Compressor)。
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
    # 压缩入口(Step 3 / Step 5 实现)
    # ---------------------------------------------------------------
    def compress(self, history: list[dict]) -> CompressionResult:
        """执行压缩。Step 2 阶段为占位,防止误用半成品。

        完成后将依次接入:
        - L1 ToolResult 折叠(Step 3,零 LLM 成本)
        - L2 增量摘要(Step 5,失败降级 L1-only)
        - 压缩后校验 _validate_history(Step 6)
        """
        raise NotImplementedError("compressor.compress(): Step 3 实现 L1 折叠")
