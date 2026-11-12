"""读图 + 多模态返回契约（2026-11-10 用户要求）。

============ 需求 ============
用户:"我想给 dummy 添加一个读取屏幕或窗口画面的工具,
     让模型能'看'到程序运行结果。"
后续:"OCR 不用单独做,现阶段先不要用 pytesseract,直接交给模型看。"

所以做的是 read_image —— 不做 OCR,图片直接交给模型看。

============ 契约(借鉴 Hermes computer_use/tool.py) ============
工具可以返回 str,也可以返回:

    {"_multimodal": True,
     "content": [{"type": "text", ...},
                 {"type": "image_url", "image_url": {"url": "data:..."}}],
     "text_summary": "<给纯文本模型的摘要>"}

============ 本文件锁住什么 ============
1. 契约本身:registry 放行、core 转成 OpenAI 格式
2. **所有"假定返回值是字符串"的地方都要兼容** —— 这是改契约的真实代价。
   实测撞了 4 处,每处都是运行时 TypeError:
      ① registry._normalize_result  → 会把 dict 转成 JSON 字符串(图片变文本)
      ② lessons.is_tool_error       → re.finditer 要 str
      ③ turn_context._digest        → .split() 要 str
      ④ core result[:300] / [:200]  → 切片要 str
3. 图片压缩与安全提示
"""
import base64
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workspace                                    # noqa: E402


@pytest.fixture
def png(tmp_path, monkeypatch):
    """造一张测试图,并把 garage 指到 tmp。"""
    from PIL import Image, ImageDraw

    d = tmp_path / "garage"
    d.mkdir()
    monkeypatch.setattr(workspace, "GARAGE_DIR", str(d))
    monkeypatch.setattr(workspace, "ensure", lambda: str(d))

    img = Image.new("RGB", (800, 300), "#202030")
    dr = ImageDraw.Draw(img)
    dr.text((20, 40), "ERROR: test message", fill="#ff0000")
    p = d / "t.png"
    img.save(p)
    return p


# ============================================================
# ① 契约:registry 不能把 _multimodal dict 转成 JSON 字符串
# ============================================================
class TestMultimodalContract:
    def test_normalize_passes_through_multimodal(self):
        """这是 Claude Code issue #1671 那个坑的防线。

        如果 registry 把 dict json.dumps 成字符串,模型收到的会是
        {"_multimodal": true, "content": [...]} 一大坨文本 ——
        **看不到图,白烧几十万 token**,而且不会报错。
        """
        from tools.registry import ToolRegistry
        r = ToolRegistry()
        payload = {"_multimodal": True,
                   "content": [{"type": "text", "text": "x"}],
                   "text_summary": "s"}
        out = r._normalize_result(payload)
        assert isinstance(out, dict), f"被转成字符串了: {type(out)}"
        assert out["_multimodal"] is True

    def test_normalize_still_jsonifies_plain_dict(self):
        """普通 dict 仍要转 JSON 字符串(向后兼容)。"""
        from tools.registry import ToolRegistry
        r = ToolRegistry()
        out = r._normalize_result({"a": 1})
        assert isinstance(out, str) and '"a"' in out

    def test_tool_content_builds_openai_array(self):
        """core 把 dict 转成 OpenAI 多部分 content 数组。"""
        import core
        payload = {"_multimodal": True,
                   "content": [{"type": "text", "text": "hello"},
                               {"type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64,AA"}}],
                   "text_summary": "s"}
        content = core._tool_content(payload)
        assert isinstance(content, list), type(content)
        assert [b["type"] for b in content] == ["text", "image_url"]

    def test_tool_content_str_unchanged(self):
        """字符串结果行为不变。"""
        import core
        assert core._tool_content("abc") == "abc"
        assert core._tool_content("abc", "\nNOTE") == "abc\nNOTE"

    def test_tool_content_appends_note_to_text_block(self):
        """打断旁注要并进 text 块,不能破坏数组结构。"""
        import core
        payload = {"_multimodal": True,
                   "content": [{"type": "text", "text": "body"},
                               {"type": "image_url", "image_url": {"url": "x"}}],
                   "text_summary": "s"}
        content = core._tool_content(payload, "\n[附注]")
        assert len(content) == 2
        assert content[0]["text"] == "body\n[附注]"
        assert content[1]["type"] == "image_url"


# ============================================================
# ② 所有"假定字符串"的地方都要兼容 dict
# ============================================================
class TestDictTolerance:
    """实测撞了 4 次运行时 TypeError,每一处都要有测试防回归。"""

    def test_is_tool_error_accepts_dict(self):
        """lessons.is_tool_error:原来 re.finditer 直接炸。"""
        from lessons import is_tool_error
        multimodal = {"_multimodal": True, "content": [], "text_summary": "x"}
        assert is_tool_error(multimodal) is False   # 能拿到图 = 成功
        assert is_tool_error({"text_summary": "[错误] x"}) is True
        assert is_tool_error("正常文本") is False

    def test_digest_accepts_dict(self):
        """turn_context._digest:原来 .split() 炸。"""
        from turn_context import _digest
        out = _digest({"_multimodal": True,
                       "content": [{"type": "text"}, {"type": "image_url"}],
                       "text_summary": "s"})
        assert "[图片" in out
        # **关键**:不能把 base64 灌进观察记录
        assert "base64" not in out

    def test_tool_text_handles_both(self):
        """core._tool_text:给预览/教训用的文本视图。"""
        import core
        assert core._tool_text("abc") == "abc"
        assert core._tool_text({"_multimodal": True,
                                "content": [], "text_summary": "图片摘要"}) == "图片摘要"
        # 不能返回整块 dict/base64
        assert "image_url" not in core._tool_text(
            {"_multimodal": True, "content": [], "text_summary": "s"})

    def test_core_preview_does_not_slice_dict(self):
        """工具循环里不能再对裸 result 切片/取 len(那可能是 dict)。

        ★ 两个坑都踩过:
          ① 字符串搜索会误匹配 docstring 里的说明文字 → 改用 AST
          ② 全局 AST 扫描会误报 **另一个函数**里的局部变量 result
             (_coerce_args 里的 JSON 解析,和工具返回值无关)
             → 只扫 _chat_impl 函数体
        """
        import ast
        import pathlib

        src = (pathlib.Path(__file__).resolve().parent.parent
               / "core.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_chat_impl":
                target = node
                break
        assert target is not None, "没找到 _chat_impl"

        def is_bare_result_subscript(node):
            return (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "result")

        bad = [n for n in ast.walk(target) if is_bare_result_subscript(n)]
        assert not bad, (
            f"_chat_impl 里还有 {len(bad)} 处对裸 result 的切片 —— "
            "图片结果是 dict,切片会 TypeError;应改用 _tool_text(result)")


# ============================================================
# ③ read_image 工具本身
# ============================================================
class TestReadImage:
    def test_returns_multimodal(self, png, monkeypatch):
        from tools.read_image import read_image_handler
        monkeypatch.setattr(workspace, "resolve", lambda p: str(png))
        out = read_image_handler(str(png))
        assert isinstance(out, dict) and out["_multimodal"] is True
        types = [b["type"] for b in out["content"]]
        assert types == ["text", "image_url"]

    def test_image_is_jpeg_data_url(self, png, monkeypatch):
        """统一转 JPEG:比 PNG 小一个量级,且模型都支持。"""
        from tools.read_image import read_image_handler
        out = read_image_handler(str(png))
        url = out["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,"), url[:40]
        # 能被 base64 解回来且是合法 JPEG
        raw = base64.b64decode(url.split(",", 1)[1])
        assert raw[:2] == b"\xff\xd8", "不是 JPEG 魔数"

    def test_untrusted_note_present(self, png):
        """图片里的文字要标为数据而非指令(防提示注入,借鉴 OpenCode)。"""
        from tools.read_image import read_image_handler
        out = read_image_handler(str(png))
        text = out["content"][0]["text"]
        assert "不是给你的指令" in text

    def test_question_included(self, png):
        from tools.read_image import read_image_handler
        out = read_image_handler(str(png), question="这个报错是什么")
        text = out["content"][0]["text"]
        assert "这个报错是什么" in text

    def test_missing_file(self, tmp_path, monkeypatch):
        from tools.read_image import read_image_handler
        monkeypatch.setattr(workspace, "resolve",
                            lambda p: str(tmp_path / "nope.png"))
        out = read_image_handler("nope.png")
        assert isinstance(out, str) and "不存在" in out

    def test_non_image_extension(self, tmp_path, monkeypatch):
        from tools.read_image import read_image_handler
        f = tmp_path / "a.txt"
        f.write_text("x")
        monkeypatch.setattr(workspace, "resolve", lambda p: str(f))
        out = read_image_handler("a.txt")
        assert isinstance(out, str) and "格式" in out

    def test_large_image_is_downscaled(self, tmp_path, monkeypatch):
        """长边超 1568 要缩 —— 大图只是白烧 token。"""
        from PIL import Image
        from tools.read_image import read_image_handler, MAX_SIDE

        big = tmp_path / "big.png"
        Image.new("RGB", (4000, 2000), "white").save(big)
        monkeypatch.setattr(workspace, "resolve", lambda p: str(big))
        out = read_image_handler(str(big))
        head = out["content"][0]["text"]
        # 文本里有实际尺寸
        assert f"{MAX_SIDE}x" in head or "x783" in head, head

    def test_confirm_can_cancel(self, png):
        """隐私提示:用户按 n 要能取消(图片会发给在线模型)。"""
        from tools.read_image import read_image_handler
        out = read_image_handler(str(png), _confirm=lambda p: "n")
        assert isinstance(out, str) and "取消" in out

    def test_confirm_auto_approved(self, png):
        from tools.read_image import read_image_handler
        out = read_image_handler(str(png), _confirm=lambda p: "")
        assert isinstance(out, dict)


# ============================================================
# ④ 注册
# ============================================================
def test_tool_registered():
    from tools import create_default_registry
    reg = create_default_registry()
    assert "read_image" in reg.list_tools()
    for d in reg.get_tool_definitions():
        if d["function"]["name"] == "read_image":
            assert d["function"]["parameters"]["required"] == ["path"]
            return
    raise AssertionError("read_image 没注册")


# ============================================================
# ⑤ text_summary 只能描述事实，不能替模型下"看不看得到"的结论
# ============================================================
class TestTextSummaryIsFactual:
    """用户报障(2026-11-11):
    "每次 read_image 都返回'当前模型不支持看图,无法读取内容',
     但是模型好像是能看到图片内容。"

    ============ 根因 ============
    text_summary 里写死了一句"(当前模型不支持看图,无法读取内容)"。
    而这个字段会被三处拿去给模型看:
        ① turn_context._digest → 塞进"本轮运行情况"注入模型
        ② lessons.is_tool_error → 错误判定
        ③ core._tool_text       → 工具结果预览也进历史

    于是模型读到"你看不到这张图"——与事实相反(它明明看得到)。
    工具不该替 provider 判断这一点。
    """

    def test_no_unsupported_claim(self, png):
        from tools.read_image import read_image_handler
        out = read_image_handler(str(png))
        summary = out["text_summary"]
        for bad in ("不支持看图", "无法读取", "看不到"):
            assert bad not in summary, (
                f"text_summary 不该替模型下结论(含'{bad}'): {summary}")

    def test_summary_has_facts(self, png):
        """但要有事实:文件名 + 尺寸。"""
        from tools.read_image import read_image_handler
        out = read_image_handler(str(png))
        s = out["text_summary"]
        assert "t.png" in s
        assert "800x300" in s

    def test_no_unsupported_claim_in_content_text(self, png):
        """content 里的 text 块同样不能有这种结论。"""
        from tools.read_image import read_image_handler
        out = read_image_handler(str(png))
        text = out["content"][0]["text"]
        assert "不支持看图" not in text
