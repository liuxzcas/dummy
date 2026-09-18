"""llm.message_content —— 从 llm.chat() 返回值取文本的唯一入口。

============ 被这个 bug 坑过(2026-09-17) ============
`/improve terminal` 报"无法生成修复提案"。根因是三处调用方用错模式取值:

    if isinstance(resp, dict):
        content = resp.get("content") or ""
    elif hasattr(resp, "get"):
        content = resp.get("content") or ""
    else:
        content = ""          # ← 永远走这里

llm.chat() 返回的是 OpenAI SDK 的 **ChatCompletionMessage**(有 .content,
**没有 .get()**),所以 content 恒为空串;外层 `except Exception: return []`
又把一切异常吞掉 —— **不报错,只是永远不工作**。

影响:lessons 的教训生成从 Phase 3 Step 3 起就没生效过;
self_improve 的 /improve 报错。

为什么测试没抓到:**测试的假对象形态与真实不一致**
  · 真实: ChatCompletionMessage → 有 .content
  · 假对象: type("R", (), {"get": ...}) → 有 .get(),没有 .content
于是"用错方式取值"在测试里恰好能工作。本文件的断言锁住这个契约。
"""
import pytest

from llm import message_content


# ============================================================
# 三种形态都要支持(生产是 SDK 对象,测试是 dict/字符串)
# ============================================================
def test_sdk_object_style():
    """**生产形态**:对象带 .content(SDK 的 ChatCompletionMessage)。"""
    class Msg:
        content = "你好"
    assert message_content(Msg()) == "你好"


def test_dict_style():
    assert message_content({"content": "来自 dict"}) == "来自 dict"


def test_str_style():
    assert message_content("直接就是字符串") == "直接就是字符串"


# ============================================================
# 边界:不能崩
# ============================================================
def test_none_returns_empty():
    assert message_content(None) == ""


def test_missing_content_attr_returns_empty():
    """没有 content 属性时返回空串(不抛异常)。"""
    assert message_content(object()) == ""


def test_none_content_returns_empty():
    class Msg:
        content = None
    assert message_content(Msg()) == ""


def test_empty_string_stays_empty():
    assert message_content("") == ""


# ============================================================
# 关键契约:必须能处理 SDK 对象的形态
# ============================================================
def test_rejects_get_only_fake():
    """**这条是本次 bug 的回归测试。**

    旧测试的假对象只有 .get() 没有 .content —— 那种形态**必须拿不到内容**,
    否则说明 message_content 又退回到"靠 .get() 取值"的老路了。

    真实返回对象没有 .get(),所以用 .get() 取值必然失败。
    """
    fake = type("R", (), {"get": lambda self, k, d=None: "内容"})()
    assert message_content(fake) == "", \
        "只带 .get() 的假对象不该被支持 —— 真实返回对象没有 .get()"


def test_real_openai_message_shape(monkeypatch):
    """模拟 OpenAI SDK 的真实形态:属性访问,且没有 .get()。

    这是 message_content 存在的理由 —— 调用方不该猜类型。
    """
    class FakeChatCompletionMessage:
        __slots__ = ("content", "tool_calls", "role")
        def __init__(self):
            self.content = '{"root_cause": "x"}'
            self.tool_calls = None
            self.role = "assistant"
    m = FakeChatCompletionMessage()
    assert not hasattr(m, "get"), "真实形态没有 .get() —— 这正是旧代码失效的原因"
    assert message_content(m) == '{"root_cause": "x"}'
