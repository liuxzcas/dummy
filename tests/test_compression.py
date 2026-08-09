"""Phase 2.2 压缩模块正式测试集(pytest)。

分层(对应 docs/phase2.2_plan.md Step 7):
- 单元:配置/结果默认值、触发判断边界、断路器、L1 折叠、L2 摘要、
  strip_meta、_validate_history
- 事实召回 8 题:忠实 mock 摘要器跑压缩全流程,断言压缩后历史保留事实。
  注意:这测的是"流程正确性"(块切分/替换/保留对不对),不是模型摘要质量;
  模型质量由 Step 8 真实 DeepSeek 冒烟负责。
- 容错 5 用例:降级(超时/空摘要)、断路器、非法结构回滚、事件契约
- 格式合法性:压缩后 strip_meta 的历史过 _validate_history

全部 mock 化,零网络请求。
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor import (  # noqa: E402
    CompressionConfig,
    CompressionResult,
    ContextCompressor,
    SummaryError,
    _validate_history,
    strip_meta,
)
from core import DummyAgent  # noqa: E402
from tools import ToolRegistry  # noqa: E402
from session_store import SessionStore  # noqa: E402

SUMMARY_MARK = "你负责压缩对话历史"


# ============================================================
# mocks
# ============================================================
class FaithfulSummarizer:
    """理想摘要器:把 [新增对话] 块原样抄进摘要(mock,零网络)。

    忠实保留所有事实——用于测流程正确性(事实是否被正确送进摘要块、
    摘要是否正确替换进历史)。
    """

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=8192):
        self.calls += 1
        prompt = messages[0]["content"]
        block = prompt.split("[新增对话]\n", 1)[1].split("\n\n输出更新后的摘要", 1)[0]
        return SimpleNamespace(content=block, tool_calls=[])


class ErrorSummarizer:
    """摘要调用必抛异常(模拟网络/API 故障)。"""

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=8192):
        raise RuntimeError("summary API down")


class EmptySummarizer:
    """摘要返回空内容(模型抽风)。"""

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=8192):
        return SimpleNamespace(content=None, tool_calls=[])


def turns(n: int) -> list[dict]:
    """构造 n 轮标准交换:user → assistant(tool_calls) → tool → assistant。"""
    h = [{"role": "system", "content": "sys prompt"}]
    for i in range(n):
        h.append({"role": "user", "content": f"u{i}"})
        h.append({"role": "assistant", "content": None,
                  "tool_calls": [{"id": f"c{i}", "type": "function",
                                  "function": {"name": "f", "arguments": "{}"}}]})
        h.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"r{i}"})
        h.append({"role": "assistant", "content": f"a{i}"})
    return h


def compress_history(history, summarizer=None, **cfg_kwargs):
    """跑一次全流程压缩。"""
    comp = ContextCompressor(
        llm=summarizer or FaithfulSummarizer(),
        config=CompressionConfig(**cfg_kwargs),
    )
    return comp, comp.compress(history)


def history_text(history) -> str:
    return json.dumps(history, ensure_ascii=False)


# ============================================================
# 单元:配置 / 结果 / 触发 / 断路器
# ============================================================
def test_config_defaults():
    c = CompressionConfig()
    assert c.window_tokens == 64000
    assert c.threshold_ratio == 0.7
    assert c.recent_turns_keep == 6
    assert c.enable_l1 is True and c.enable_l2 is True
    assert c.tool_result_max_chars == 600
    assert c.tool_result_keep_head == 200 and c.tool_result_keep_tail == 100
    assert c.summary_max_tokens == 1024 and c.summary_retry == 1
    assert c.max_consecutive_failures == 3
    assert "{old_summary}" in c.summary_prompt and "{new_block}" in c.summary_prompt


def test_result_defaults():
    r = CompressionResult(success=True, history=[], strategy_used="none")
    assert r.folded_count == 0 and r.summary_covers == 0
    assert r.chars_before == 0 and r.chars_after == 0
    assert r.error_type is None and r.error_msg is None
    assert r.duration_ms == 0 and r.folded_originals is None


def test_should_compress_boundary():
    comp = ContextCompressor(llm=None, config=CompressionConfig(window_tokens=1000))
    # 阈值 = 700;严格大于才触发
    assert comp.should_compress(699) is False
    assert comp.should_compress(700) is False   # 恰好等于 → 不触发
    assert comp.should_compress(701) is True
    assert comp.should_compress(None) is False  # 未调用过 API


def test_circuit_breaker():
    comp = ContextCompressor(llm=None, config=CompressionConfig(max_consecutive_failures=3))
    comp.register_failure()
    comp.register_failure()
    assert comp.paused is False  # 2 次未达阈值
    comp.register_failure()
    assert comp.paused is True
    assert comp.should_compress(99999) is False  # 暂停时零开销短路
    comp.register_success()
    assert comp.paused is False and comp._consecutive_failures == 0


# ============================================================
# 单元:L1 折叠 / L2 摘要 / strip_meta / 校验
# ============================================================
def test_l1_fold_and_originals():
    comp = ContextCompressor(llm=None, config=CompressionConfig(enable_l2=False))
    long_msg = {"role": "tool", "tool_call_id": "c1", "content": "A" * 1000}
    short_msg = {"role": "tool", "tool_call_id": "c2", "content": "B" * 100}
    history = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"},
               long_msg, short_msg]
    out, folded, originals = comp._fold_tool_results(history)
    assert folded == 1
    assert originals == [{"tool_call_id": "c1", "content": "A" * 1000}]
    assert out[2]["content"].startswith("A" * 200) and out[2]["content"].endswith("A" * 100)
    assert "tool_result_archive" in out[2]["content"]
    assert out[3]["content"] == "B" * 100          # 短消息不动
    assert history[2]["content"] == "A" * 1000     # 原子性:输入不动


def test_l2_summarize_structure():
    comp = ContextCompressor(llm=FaithfulSummarizer())
    new_h, covers = comp._summarize_prefix(turns(10))
    assert covers == 16
    assert new_h[0]["role"] == "system"
    assert new_h[1]["role"] == "system"           # 摘要消息
    assert new_h[1]["_meta"]["compressed"] is True
    assert new_h[1]["_meta"]["covers"] == 16
    assert new_h[2]["role"] == "user"             # 交替安全
    assert len(new_h) == 26                        # 1 sys + 1 摘要 + 6 轮原文


def test_l2_covers_accumulate_and_unique():
    comp = ContextCompressor(llm=FaithfulSummarizer())
    new_h, c1 = comp._summarize_prefix(turns(10))
    grown = new_h + turns(5)[1:]
    new_h2, c2 = comp._summarize_prefix(grown)
    assert c2 == 36                                 # 16 + 20(5 轮)
    n_sum = sum(1 for m in new_h2 if m.get("_meta", {}).get("compressed"))
    assert n_sum == 1                               # 新旧摘要不并存(回归 A9)


def test_strip_meta():
    orig = [{"role": "system", "content": "[早期对话摘要]x", "_meta": {"compressed": True}},
            {"role": "user", "content": "hi"}]
    cleaned = strip_meta(orig)
    assert "_meta" not in cleaned[0]
    assert cleaned[0]["role"] == "system"
    assert "_meta" in orig[0]                       # 浅拷贝,输入不动


@pytest.mark.parametrize("history,ok", [
    (turns(3), True),                                                    # 合法
    ([{"role": "user", "content": "x"}], False),                         # 首条非 system
    ([{"role": "system", "content": "s"}, {"role": "admin", "content": "x"}], False),  # 非法 role
    ([{"role": "system", "content": "s"},
      {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}], False),  # 缺 tool 响应
    ([{"role": "system", "content": "s"},
      {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
      {"role": "tool", "tool_call_id": "c1", "content": "r"}], True),    # 配对完整
    ([{"role": "system", "content": "s"},
      {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
      {"role": "tool", "tool_call_id": "c2", "content": "r"}], False),    # id 不匹配
    ([{"role": "system", "content": "s"}, {"role": "tool", "tool_call_id": "c9", "content": "r"}], False),  # 无主 tool
    ([{"role": "system", "content": "s"}, {"role": "tool", "content": "r"}], False),  # tool 缺 id
])
def test_validate_history(history, ok):
    assert _validate_history(history) is ok


# ============================================================
# 事实召回 8 题(忠实 mock 摘要器,全流程)
# ============================================================
def build_recall_history(fact_user_msg: str, extra_turns_before: int = 4,
                         tool_fact: str | None = None) -> list[dict]:
    """10 轮对话;事实埋在早期轮(会被摘要),第 10 轮是"提问"。"""
    h = [{"role": "system", "content": "sys prompt"}]
    # 早期轮(将被摘要)
    for i in range(extra_turns_before):
        h += [{"role": "user", "content": f"第{i}轮"},
              {"role": "assistant", "content": f"回答{i}"}]
    h.append({"role": "user", "content": fact_user_msg})
    h.append({"role": "assistant", "content": "知道了"})
    if tool_fact:
        h.append({"role": "assistant", "tool_calls": [{"id": "cf", "type": "function",
                                                       "function": {"name": "f", "arguments": "{}"}}]})
        h.append({"role": "tool", "tool_call_id": "cf", "content": tool_fact})
        h.append({"role": "assistant", "content": "看过了"})
    # 中间填充轮(将被摘要)
    for i in range(3):
        h += [{"role": "user", "content": f"填充{i}"},
              {"role": "assistant", "content": f"答{i}"}]
    # 最近轮:提问(保留原文)
    h += [{"role": "user", "content": "问题:"}, {"role": "assistant", "content": "?"}]
    return h


RECALL_CASES = [
    ("文件路径", "把结果写到 D:/Engineering/dummy/output.txt", "D:/Engineering/dummy/output.txt"),
    ("代码细节", "用 requests 库,超时 30 秒", "requests"),
    ("用户偏好", "以后回复都用中文", "中文"),
    ("决定", "决定先做 ToolResult 折叠", "ToolResult 折叠"),
    ("数字", "预算 500 元", "500"),
    ("未完成事项", "下一步是写测试", "写测试"),
    ("跨轮次", "项目名是 dummy-agent", "dummy-agent"),
]


@pytest.mark.parametrize("name,fact,expect", RECALL_CASES)
def test_recall_basic(name, fact, expect):
    history = build_recall_history(fact)
    comp, result = compress_history(history)
    assert result.success, f"{name}: {result.error_type}"
    assert result.strategy_used in ("L1+L2", "L2")
    assert expect in history_text(result.history), f"{name}: 事实丢失"


def test_recall_tool_result_in_tail():
    """第 7 题:L1 折叠(尾部保留)+ L2 摘要的组合路径。

    tool 结果 2000 字符,关键信息在尾部——L1 折叠保留尾 100 字符,
    摘要器忠实保留,压缩后仍可检索到 "port 8080"。
    """
    log = "L" * 1900 + "\nERROR: port 8080"   # 关键信息在尾部
    history = build_recall_history("看一下日志", extra_turns_before=2, tool_fact=log)
    comp, result = compress_history(history)
    assert result.success and result.folded_count == 1
    assert "port 8080" in history_text(result.history)


# ============================================================
# 容错 5 用例
# ============================================================
def test_fault_timeout_degradation():
    """摘要超时 → 降级 L1-only(success=True + error_type)。

    注意:长 tool 消息必须放在配对完整的轮次内(不能无主追加,
    否则输入历史本身非法,校验器会按设计回滚)。
    """
    history = turns(8)
    for m in history:
        if m.get("role") == "tool" and m.get("tool_call_id") == "c4":
            m["content"] = "D" * 2000   # 第 4 轮的 tool 结果变长(保留区,触发 L1)
            break
    comp, result = compress_history(history, summarizer=ErrorSummarizer())
    assert result.success is True                       # 降级不是失败
    assert result.strategy_used == "L1"                # L1 生效
    assert result.folded_count == 1
    assert result.error_type == "summary_timeout"


def test_fault_empty_summary():
    """摘要空 → 丢弃,降级(不重试)。"""
    comp = ContextCompressor(llm=EmptySummarizer())
    try:
        comp._call_summary_llm("", [{"role": "user", "content": "x"}])
        pytest.fail("应抛 SummaryError")
    except SummaryError as e:
        assert e.error_type == "empty_summary"


def test_fault_circuit_breaker_pause():
    """连续 3 次失败 → 断路器暂停,should_compress 短路。"""
    comp = ContextCompressor(llm=None, config=CompressionConfig(window_tokens=1000))
    for _ in range(3):
        comp.register_failure()
    assert comp.paused is True
    assert comp.should_compress(99999) is False


def test_fault_invalid_history_rollback():
    """压缩结果结构非法 → 回滚:success=False,不产出可归档的折叠。"""
    # 构造会在保留区留下无主 tool 的坏历史
    bad = turns(8)
    bad.append({"role": "tool", "tool_call_id": "ghost", "content": "无主结果"})
    comp, result = compress_history(bad)
    assert result.success is False
    assert result.error_type == "invalid_history"
    assert result.history is None
    assert result.folded_originals is None            # 回滚的折叠绝不归档
    assert result.folded_count == 0


def test_fault_event_contract(monkeypatch, tmp_path):
    """压缩事件契约:12 字段 + 降级时 error_type 进入事件。"""
    captured = {}

    def fake_log(self, result, trigger_tokens, error_type=None, error_msg=None):
        captured["result"] = result
        captured["tokens"] = trigger_tokens
        captured["error_type"] = error_type
        captured["error_msg"] = error_msg

    monkeypatch.setattr(DummyAgent, "_log_compression_event", fake_log)
    llm = ErrorSummarizer()
    agent = DummyAgent(llm, ToolRegistry())
    agent.session_store = SessionStore(os.path.join(tmp_path, "t.db"))
    agent.compressor = ContextCompressor(
        llm, CompressionConfig(window_tokens=1000, threshold_ratio=0.7)
    )
    agent._ensure_session()
    agent.history = turns(10)
    llm.last_usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=0, total_tokens=1000)
    agent._maybe_compress()
    r = captured["result"]
    assert r.success is True and r.error_type == "summary_timeout"   # 降级进事件
    assert captured["tokens"] == 1000
    assert captured["error_type"] is None                            # 无 persist 错误


# ============================================================
# 格式合法性
# ============================================================
def test_compressed_history_passes_schema():
    """压缩后 strip_meta 的历史必须通过 _validate_history(可安全发 API)。"""
    history = build_recall_history("把结果写到 D:/x.txt")
    comp, result = compress_history(history)
    assert result.success
    cleaned = strip_meta(result.history)
    assert _validate_history(cleaned) is True
    assert all("_meta" not in m for m in cleaned)
    assert all(m.get("role") in ("system", "user", "assistant", "tool") for m in cleaned)
