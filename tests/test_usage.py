"""用量统计测试 —— 成本计算 / 缓存命中率 / 累计。

============ 为什么要专门测成本 ============
2026-09-16 实测发现旧算法**把缓存部分算了两遍**:
  DeepSeek 的 prompt_tokens **已包含** cached_tokens
  (实测: prompt=1620, cached=1408, miss=212 → 1408+212=1620)
  旧算法 cost = prompt*¥3.0 + cached*¥0.1  ← cached 被算两次
  实测高估 5.59 倍(命中越多越狠)。

而在此之前,**整个用量/成本模块没有任何测试**。
"""
import os

from core import DummyAgent
from tools import create_default_registry


class _Usage:
    """最小 usage 桩(模拟 OpenAI/DeepSeek 的返回对象)。"""

    def __init__(self, prompt, completion, cached):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_cache_hit_tokens = cached
        self.prompt_tokens_details = None


def _agent():
    a = DummyAgent(None, create_default_registry(), system_prompt="P")
    a.llm = type("L", (), {"last_usage": None, "base_url": "https://api.deepseek.com"})()
    return a


def test_cost_excludes_cached_from_full_price():
    """缓存部分只按缓存价计,不再按输入价重复计一遍。

    数字取自 2026-09-16 的实测:prompt=1620, completion=16, cached=1408。
    正确值 = (1620-1408)*3.0 + 1408*0.1 + 16*9.0 = 636 + 140.8 + 144 = ¥0.0009208
    """
    a = _agent()
    a.session_usage = {"prompt": 1620, "completion": 16, "cached": 1408}
    line = a.format_usage_line()
    assert "¥0.0009" in line, f"成本应约 ¥0.0009,实际: {line}"
    # 旧算法会给 ¥0.0051(高估 5.59 倍)——确保没退回去
    assert "¥0.0051" not in line


def test_cost_without_cache_uses_full_price():
    """无缓存命中时,全部输入按输入价。"""
    a = _agent()
    a.session_usage = {"prompt": 1000, "completion": 100, "cached": 0}
    # (1000-0)*3.0 + 0 + 100*9.0 = 3000 + 900 = 3900 / 1e6 = ¥0.0039
    assert "¥0.0039" in a.format_usage_line()


def test_cost_all_cached():
    """全部命中缓存 → 输入部分按缓存价(¥0.1),不是输入价。"""
    a = _agent()
    a.session_usage = {"prompt": 1000, "completion": 0, "cached": 1000}
    # (1000-1000)*3.0 + 1000*0.1 = 100 / 1e6 = ¥0.0001
    assert "¥0.0001" in a.format_usage_line()


def test_accumulate_usage_sums_correctly():
    """多次调用的用量正确累加(内存层)。"""
    a = _agent()
    a.session_usage = {"prompt": 0, "completion": 0, "cached": 0}
    a.llm.last_usage = _Usage(100, 10, 60)
    a._accumulate_usage()
    a.llm.last_usage = _Usage(200, 20, 150)
    a._accumulate_usage()
    assert a.session_usage == {"prompt": 300, "completion": 30, "cached": 210}


def test_hit_rate_uses_last_call():
    """显示行的"命中率"取自本次调用(不是累计)。"""
    a = _agent()
    a.llm.last_usage = _Usage(1000, 10, 800)     # 本次 80%
    a.session_usage = {"prompt": 50000, "completion": 500, "cached": 10000}
    line = a.format_usage_line()
    assert "命中 80%" in line, f"应显示本次的 80%,实际: {line}"


def test_local_endpoint_shows_no_cost():
    """本地部署(ollama/vllm)不按云端单价估算。"""
    a = _agent()
    a.llm.base_url = "http://127.0.0.1:11434/v1"
    a.session_usage = {"prompt": 10000, "completion": 100, "cached": 5000}
    assert "本地" in a.format_usage_line()
