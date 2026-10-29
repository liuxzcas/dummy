"""输入层:拖拽路径识别 + 斜杠命令不冲突。

============ 为什么这个模块存在(2026-10-16) ============
用户目标 1:把文件拖到终端里,dummy 能识别并告诉模型路径。

终端的行为很简单:**拖拽 = 把路径文本插入命令行**。
所以问题的全部内容就是"判断这串文本是不是路径"。

============ 最危险的坑:斜杠命令与 git-bash 路径同前缀 ============
dummy 有 14 个斜杠命令(`/help` `/lessons` ...),
而 git-bash 的绝对路径是 `/c/Users/...` —— **都以 / 开头**。

Hermes 踩过这个坑(cli.py:2869 注释原文):
    "`starts_like_path` prefilter accepts any input starting with `/`,
     then this resolver tries to stat it ... Without this guard the
     OSError propagates up to the process_loop catch-all and the user
     input is silently lost"
→ 用户的输入被静默吞掉,提示符就那么挂着。

dummy 的做法:**先查命令白名单**,是命令就绝不走路径分支。
本文件锁住这一点。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from input_layer import (                                    # noqa: E402
    detect_file_drop, format_drop_message, looks_like_path,
    is_known_command, read_input,
)


@pytest.fixture
def files(tmp_path):
    """造几个真实文件用于测试。"""
    a = tmp_path / "a.txt"
    a.write_text("hello", encoding="utf-8")
    spaced = tmp_path / "dir with space"
    spaced.mkdir()
    b = spaced / "b.log"
    b.write_text("log", encoding="utf-8")
    return {"a": str(a), "spaced": str(b), "dir": str(tmp_path)}


# ============================================================
# ① 斜杠命令必须优先(最危险的坑)
# ============================================================
class TestCommandWhitelist:
    @pytest.mark.parametrize("cmd", [
        "/help", "/quit", "/exit", "/q", "/reset", "/resume",
        "/sessions", "/history", "/search", "/memories", "/skills",
        "/lessons", "/improve", "/tools",
    ])
    def test_known_commands_not_treated_as_path(self, cmd):
        assert is_known_command(cmd)
        assert detect_file_drop(cmd) is None, \
            f"{cmd} 是命令,绝不能被当成路径"

    def test_commands_with_args_not_path(self):
        """/lessons -a 这种带参数的也必须是命令。"""
        assert is_known_command("/lessons -a")
        assert detect_file_drop("/lessons -a") is None

    def test_command_uppercase(self):
        """/HELP 也是命令(大小写不敏感)。"""
        assert is_known_command("/HELP")
        assert detect_file_drop("/HELP") is None

    def test_unknown_slash_string_may_be_path(self, files):
        """不在白名单的 / 开头串,如果文件真存在 → 当路径。

        这就是 git-bash 风格路径的情况。
        """
        p = files["a"]
        # 转成 /c/... 形式(Windows 上 os.path.isfile 也认正斜杠)
        fwd = p.replace("\\", "/")
        drop = detect_file_drop(fwd)
        assert drop is not None, f"{fwd} 是真实文件,应识别为路径"
        assert drop["path"].endswith("a.txt")


# ============================================================
# ② 形态预筛
# ============================================================
class TestLooksLikePath:
    @pytest.mark.parametrize("s", [
        "/c/Users/me/a.txt", "C:\\Users\\me\\a.txt", "C:/Users/me/a.txt",
        "./a.txt", "../a.txt", "~/a.txt", '"/c/Users/me/a.txt"',
    ])
    def test_path_like(self, s):
        assert looks_like_path(s), f"{s!r} 应该像路径"

    @pytest.mark.parametrize("s", [
        "你好", "hello world", "帮我看看这个文件", "1+1等于几",
        "", "   ",
    ])
    def test_not_path_like(self, s):
        assert not looks_like_path(s), f"{s!r} 不该像路径"


# ============================================================
# ③ 必须真实存在才算路径
# ============================================================
class TestMustExist:
    def test_nonexistent_path_returns_none(self):
        """形态像路径但文件不存在 → 不当路径。

        这条很重要:用户可能只是打了 `/some/word`,
        不能因为长得像路径就吃掉它。
        """
        assert detect_file_drop("/definitely/not/exists/xyz.txt") is None
        assert detect_file_drop("C:\\no\\such\\file\\abc.txt") is None

    def test_directory_not_accepted(self, files):
        """目录不算(只认文件)。"""
        assert detect_file_drop(files["dir"]) is None

    def test_real_file_accepted(self, files):
        drop = detect_file_drop(files["a"])
        assert drop is not None
        assert os.path.isfile(drop["path"])
        assert drop["remainder"] == ""


# ============================================================
# ④ 路径 + 后续文字
# ============================================================
class TestPathWithRemainder:
    def test_path_then_text(self, files):
        drop = detect_file_drop(f"{files['a']} 帮我看看这个报错")
        assert drop is not None
        assert drop["path"].endswith("a.txt")
        assert drop["remainder"] == "帮我看看这个报错"

    def test_path_with_spaces_then_text(self, files):
        """路径本身含空格,后面还有文字 —— 要从后往前切。"""
        drop = detect_file_drop(f"{files['spaced']} 这是什么")
        assert drop is not None, "含空格的路径应能识别"
        assert drop["path"].endswith("b.log")
        assert drop["remainder"] == "这是什么"

    def test_quoted_path_with_spaces(self, files):
        """带引号的路径(资源管理器拖拽含空格路径时会加)。"""
        drop = detect_file_drop(f'"{files["spaced"]}"')
        assert drop is not None
        assert drop["path"].endswith("b.log")

    def test_quoted_path_then_text(self, files):
        drop = detect_file_drop(f'"{files["spaced"]}" 看看')
        assert drop is not None
        assert drop["path"].endswith("b.log")
        assert drop["remainder"] == "看看"


# ============================================================
# ⑤ 给模型的消息措辞
# ============================================================
class TestDropMessage:
    def test_contains_path_and_size(self, files):
        drop = detect_file_drop(files["a"])
        msg = format_drop_message(drop)
        assert files["a"] in msg or "a.txt" in msg
        assert "字节" in msg, "应报出文件大小(帮助模型判断要不要读)"

    def test_includes_remainder(self, files):
        drop = detect_file_drop(f"{files['a']} 看看")
        msg = format_drop_message(drop)
        assert "看看" in msg

    def test_no_directive_wording(self, files):
        """消息里不能有指令性措辞 —— 读不读归模型判断。

        (项目原则:代码只提供事实,不替模型做决定)
        """
        drop = detect_file_drop(files["a"])
        msg = format_drop_message(drop)
        for directive in ("请读取", "请打开", "你必须", "应该读取"):
            assert directive not in msg, \
                f"出现了指令性措辞 {directive!r} —— 应只陈述事实"


# ============================================================
# ⑥ 统一入口存在(换库时的唯一改动点)
# ============================================================
class TestUnifiedEntry:
    def test_read_input_exists_and_returns_str(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "测试")
        assert read_input("> ") == "测试"

    def test_main_uses_input_layer(self):
        """main.py 不该再裸调 input(主循环) —— 必须走输入层。

        锁住这一点的意义:将来换 prompt_toolkit 时,
        如果有人新增了裸 input(),这条测试会失败。
        """
        import inspect
        import re
        import main as main_mod
        src = inspect.getsource(main_mod.main)
        assert "read_input_with_drop" in src, "主循环应走输入层"
        # 主循环里不该有 input(ui.prompt()) 这种裸调用
        assert not re.search(r"input\(ui\.prompt\(\)\)", src), \
            "主循环不应再裸调 input(ui.prompt())"


# ============================================================
# ⑦ 白名单的**真正**作用(不是靠"文件不存在"侥幸通过)
# ============================================================
class TestWhitelistActuallyMatters:
    """上面那组测试是**碰巧**通过的 —— 必须补这一组。

    实测教训(2026-10-16):把白名单检查整段删掉后,原有测试仍然全过 ——
    因为 `/lessons` 走 looks_like_path 后,文件不存在也会返回 None。
    它们靠"文件不存在"侥幸过关,没真正验证白名单。

    为什么不用"cwd 下建同名文件"来构造:实测 os.path.abspath 把
    `/lessons` 解析成盘符根目录下的文件,不是 cwd 下的 —— 触发不了。

    所以改用 monkeypatch 让 _existing_file 一律返回"存在",
    这样就**必然**要靠白名单来否决,否则命令会被当成路径。
    """

    @pytest.mark.parametrize("cmd", [
        "/lessons", "/help", "/skills", "/improve", "/tools",
    ])
    def test_whitelist_vetoes_even_if_file_exists(self, cmd, monkeypatch):
        """即使 _existing_file 说"文件存在",命令也必须优先。"""
        import input_layer as IL
        # 让任何"文件存在性检查"都说 Yes
        monkeypatch.setattr(IL, "_existing_file", lambda c: r"C:\fake\x")
        drop = IL.detect_file_drop(cmd)
        assert drop is None, (
            f"{cmd} 是已知命令 —— 即使同名文件真的存在,"
            "也必须走命令分支,不能被当成拖拽路径"
            "(否则该命令彻底不可用)"
        )

    def test_non_command_slash_still_works_as_path(self, monkeypatch):
        """反向确认:不在白名单的 / 串,文件存在时**仍然**当路径。

        这条保证白名单没有"过度拦截"(把所有 / 开头都挡掉),
        否则 git-bash 风格路径就没法拖了。
        """
        import input_layer as IL
        monkeypatch.setattr(IL, "_existing_file",
                            lambda c: "/c/Users/me/a.txt")
        drop = IL.detect_file_drop("/c/Users/me/a.txt")
        assert drop is not None, "未列入白名单的路径应能识别"
        assert drop["path"] == "/c/Users/me/a.txt"


# ============================================================
# ⑧ prompt_toolkit 接入 + 兜底
# ============================================================
class TestPromptToolkitIntegration:
    """输入实现从 input() 换成 prompt_toolkit(2026-10-17)。

    ============ 为什么换(实测依据) ============
    python 的 input() 以换行符为结束标记 → 粘贴多行只返回第一行。
    prompt_toolkit 的 Keys.BracketedPaste 把整个粘贴块作为一个事件交付,
    换行不触发提交。

    ============ 为什么必须有兜底 ============
    实测:prompt_toolkit 在 git-bash/mintty 下抛
    NoConsoleScreenBufferError(TERM=xterm-256color 但拿不到 Win32 控制台)。
    在无控制台的场合(管道/重定向)也一样。
    dummy 不该因为跑在这些环境里就完全用不了 → 退回 input()。
    """

    def test_bracketed_paste_writes_file_and_placeholder(self, tmp_path, monkeypatch):
        """核心能力:多行长文本粘贴 → 立刻落盘 + 缓冲区只剩占位符。

        ★ 行为在 2026-10-19 变了(用户反馈后):
          改前:粘贴内容留在行缓冲区(用户以为没反应),回车后才转存。
          改后:粘贴瞬间就落盘、打灰字提示、缓冲区只放占位符。
          所以这里断言的是**文件被创建 + 返回的是占位符**,而不是原文。
        """
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from prompt_toolkit import PromptSession
        import input_layer as IL

        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        monkeypatch.setattr(IL, "_emit_paste_notice", lambda p: None)  # 无关屏显
        IL.take_round_files()

        with create_pipe_input() as inp:
            sess = PromptSession(
                input=inp, output=DummyOutput(),
                key_bindings=IL._build_key_bindings())
            real = IL._get_session
            IL._get_session = lambda: sess
            try:
                # 模拟终端发来的 bracketed paste:ESC[200~ 内容 ESC[201~ + 回车
                payload = chr(27) + "[200~" + chr(10).join(
                    ["第一行", "第二行", "第三行"]) + chr(27) + "[201~" + chr(13)
                inp.send_text(payload)
                out = IL.read_input("> ")
            finally:
                IL._get_session = real

        # 返回的是占位符,不是原文
        assert out.startswith("[Pasted text #"), f"应返回占位符: {out!r}"
        # 原文不该整段进对话 —— 判据用"行数/字符数标注"而不是查文字,
        # 因为文件名本身就是内容前 20 字(会包含"第一行"这类字样)。
        assert "3 行 / " in out, f"占位符应报告行数: {out!r}"
        # 文件真的写了,内容与原文一致
        files = IL.take_round_files()
        assert len(files) == 1 and os.path.isfile(files[0])
        assert open(files[0], encoding="utf-8", newline="").read() == \
            "第一行\n第二行\n第三行"

    def test_paste_then_typing_concatenates(self):
        """粘贴后接着打字 → 两者拼接。"""
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from prompt_toolkit import PromptSession
        import input_layer as IL

        with create_pipe_input() as inp:
            sess = PromptSession(
                input=inp, output=DummyOutput(),
                key_bindings=IL._build_key_bindings())
            real = IL._get_session
            IL._get_session = lambda: sess
            try:
                inp.send_text("\x1b[200~粘贴的\n内容\x1b[201~后续打字\r")
                out = IL.read_input("> ")
            finally:
                IL._get_session = real

        assert "粘贴的" in out and "后续打字" in out

    def test_unavailable_session_falls_back_to_input(self, monkeypatch):
        """prompt_toolkit 不可用 → 退回 input(),不能崩。"""
        import input_layer as IL
        monkeypatch.setattr(IL, "_get_session", lambda: None)
        monkeypatch.setattr("builtins.input", lambda prompt="": "兜底输入")
        assert IL.read_input("> ") == "兜底输入"

    def test_session_failure_falls_back(self, monkeypatch):
        """prompt() 中途抛异常 → 退回 input(),且本进程内不再尝试。"""
        import input_layer as IL
        import input_layer

        class Boom:
            def prompt(self, p):
                raise RuntimeError("控制台被夺走")

        input_layer._session = None
        monkeypatch.setattr(IL, "_get_session", lambda: Boom())
        monkeypatch.setattr("builtins.input", lambda prompt="": "兜底")
        assert IL.read_input("> ") == "兜底"


# ============================================================
# ⑨ 长文本 → 临时文件
# ============================================================
class TestPasteCollapse:
    """用户 2026-10-16 的需求:大段文本存成临时 txt,避免填满窗口。

    ★ 关键:"含换行符"这个判据**之前不可达**(input() 把换行当结束标记
      吃掉),换 prompt_toolkit 后才第一次成立。
    """

    def test_short_single_line_untouched(self):
        from input_layer import maybe_collapse_paste
        assert maybe_collapse_paste("帮我看下这个报错") == ("帮我看下这个报错", None)

    def test_multiline_collapsed(self, tmp_path, monkeypatch):
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        payload = "第一段\n第二段\n第三段"
        out, path = IL.maybe_collapse_paste(payload)
        assert path and os.path.isfile(path)
        assert out.startswith("[Pasted text #")
        assert payload not in out, "占位符不该塞全文"
        assert open(path, encoding="utf-8", newline="").read() == payload

    def test_char_threshold_boundary(self, tmp_path, monkeypatch):
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        assert IL.maybe_collapse_paste("字" * 1999)[1] is None
        assert IL.maybe_collapse_paste("字" * 2000)[1] is not None

    def test_filename_is_first_20_chars(self, tmp_path, monkeypatch):
        """用户描述 Hermes 的体验:文件名就是内容前 20 字,要一眼认得出。"""
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        text = "这是一段从网页复制来的文字\n第二行"
        _out, path = IL.maybe_collapse_paste(text)
        name = os.path.basename(path)
        assert name.endswith(".txt")
        assert name[:-4].startswith("这是一段从网页复制来的文"), name
        assert len(name[:-4]) <= 20

    def test_filename_strips_illegal_chars(self, tmp_path, monkeypatch):
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        _out, path = IL.maybe_collapse_paste('a<b>c:d"e|f?g*h\ni')
        assert os.path.isfile(path), f"含非法字符的文件名必须能创建: {path}"

    def test_duplicate_names_get_suffix(self, tmp_path, monkeypatch):
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        t = "重复\n内容"
        _o1, p1 = IL.maybe_collapse_paste(t)
        _o2, p2 = IL.maybe_collapse_paste(t)
        assert p1 != p2, "同名不能互相覆盖"
        assert os.path.basename(p2).endswith("_2.txt")

    def test_crlf_preserved(self, tmp_path, monkeypatch):
        """粘贴内容可能来自 Windows 网页/PDF,换行要原样保留。"""
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        payload = "第一行\r\n第二行\r\n"
        _out, path = IL.maybe_collapse_paste(payload)
        assert open(path, "rb").read() == payload.encode("utf-8")

    def test_placeholder_has_increasing_seq(self, tmp_path, monkeypatch):
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        o1, _ = IL.maybe_collapse_paste("第一段\nx")
        o2, _ = IL.maybe_collapse_paste("第二段\ny")
        assert o1 != o2, "占位符编号应递增(否则用户分不清是第几段)"


# ============================================================
# ⑩ 提示符在 prompt_toolkit 下不能带 readline 标记
# ============================================================
class TestPromptMarkersStripped:
    """用户实测截图发现的 bug(2026-10-17)。

    ============ 现象 ============
    用户启动 dummy 后,提示符显示成:
        ^A^[[38;2;152;195;121m^B你 > ^A^[[0m^C
        ↑↑                          ↑↑
       SOH(\x01)                  STX(\x02)

    ============ 根因 ============
    colors.paint_prompt() 在"有 readline"时用 \001/\002 包裹 ANSI 码
    —— 那是**给 readline 的约定**(不计入提示符宽度)。
    但 prompt_toolkit 不认识这两个字节,直接当可见字符渲染。

    ============ 修法 ============
    input_layer 在交给 prompt_toolkit 前剥掉标记,再用 ANSI() 包装。
    兜底路径(input)保留原样 —— readline 需要它们。
    """

    def test_strip_markers(self):
        from input_layer import _strip_readline_markers
        SOH, STX = chr(1), chr(2)
        raw = f"{SOH}\x1b[38;2;152;195;121m{STX}你 > {SOH}\x1b[0m{STX}"
        cleaned = _strip_readline_markers(raw)
        assert SOH not in cleaned and STX not in cleaned
        assert "你 > " in cleaned
        assert "\x1b[38;2;152;195;121m" in cleaned, "ANSI 色码本身要保留"

    def test_pt_prompt_parses_color_without_leaking_controls(self):
        """交给 prompt_toolkit 的提示符:颜色被解析成真实样式,而非 ZeroWidthEscape。

        ★ 断言为什么改成"样式是颜色名"(2026-10-17 踩坑):
          实测:不剥标记时,ANSI() 包装器会**自己**把控制字符连同 ANSI 码
          一起标成 ZeroWidthEscape:
              不剥 → ('[ZeroWidthEscape]', '\\x1b[38;2;...m')
              剥掉 → ('#98c379', '你')                ← 颜色被真正解析
          所以"有没有 ZeroWidthEscape"两种都能通过,那个断言无效。
          改成断言**样式被解析成了颜色值** —— 只有剥掉标记才成立。
        """
        from prompt_toolkit.formatted_text import to_formatted_text
        from input_layer import _to_pt_prompt

        SOH, STX = chr(1), chr(2)
        raw = f"{SOH}\x1b[38;2;152;195;121m{STX}你 > {SOH}\x1b[0m{STX}"
        frags = to_formatted_text(_to_pt_prompt(raw))

        # 提示符文本要在
        assert any("你" in f[1] for f in frags), f"提示符文本丢了: {frags}"
        # 关键:颜色被真正解析(样式名是颜色值),而不是塞进 ZeroWidthEscape
        colored = [f for f in frags if f[1].strip() and f[0]]
        assert colored, f"没有任何带样式的文本片段: {frags}"
        for style, text in colored:
            assert "ZeroWidthEscape" not in str(style), (
                f"控制字符没被剥掉 —— 样式仍是 {style!r},"
                f"终端上会显示成 ^A/^B: {frags}"
            )

    def test_pt_prompt_without_markers_unchanged(self):
        """没有标记的提示符(无色模式)不该被破坏。"""
        from prompt_toolkit.formatted_text import to_formatted_text
        from input_layer import _to_pt_prompt
        frags = to_formatted_text(_to_pt_prompt("你 > "))
        assert "".join(f[1] for f in frags) == "你 > "


# ============================================================
# ⑪ 临时文件的生命周期(用户 2026-10-19 反馈)
# ============================================================
class TestRoundFileLifecycle:
    """用户:"粘贴完文本之后,发送完消息可以自动清除临时文件。
            现在临时文件完全不清。"

    ============ 为什么必须等本轮结束才删 ============
    时序:
        粘贴 → 存文件 → 发送 → 模型决定 read_file → 读取 → 回复 → 本轮结束
                                                              ↑ 只有这里能删
    删早了模型会读不到文件(报不存在)。
    """

    def test_paste_registers_file_for_cleanup(self, tmp_path, monkeypatch):
        """转存时要登记 —— 否则轮末不知道要删谁。"""
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        IL.take_round_files()          # 清空
        _out, path = IL.maybe_collapse_paste("第一行\n第二行")
        assert path in IL.take_round_files()

    def test_take_round_files_clears_after_reading(self, tmp_path, monkeypatch):
        """取出后要清空 —— 否则同一批文件会被重复删。"""
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        IL.maybe_collapse_paste("a\nb")
        first = IL.take_round_files()
        second = IL.take_round_files()
        assert first and second == [], "第二次取应为空(已消费)"

    def test_cleanup_deletes_files(self, tmp_path, monkeypatch):
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        IL.take_round_files()
        _out, path = IL.maybe_collapse_paste("x\ny")
        assert os.path.isfile(path)
        n = IL.cleanup_files(IL.take_round_files())
        assert n == 1 and not os.path.isfile(path)

    def test_cleanup_tolerates_missing_files(self, tmp_path):
        """文件已被删/无权限 → 不抛异常,只少删一个。"""
        import input_layer as IL
        n = IL.cleanup_files([str(tmp_path / "不存在.txt")])
        assert n == 0

    def test_core_cleans_up_after_chat(self, tmp_path, monkeypatch):
        """chat() 结束后,本轮登记的临时文件应被删除。"""
        import input_layer as IL
        import core
        from conftest import ScriptedLLM
        from tools import create_default_registry

        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        IL.take_round_files()
        _out, path = IL.maybe_collapse_paste("临时\n内容")
        assert os.path.isfile(path)

        agent = core.DummyAgent(ScriptedLLM([]), create_default_registry())
        agent.chat("测试")

        assert not os.path.isfile(path), "本轮结束后临时文件应被清理"

    def test_short_input_registers_nothing(self):
        import input_layer as IL
        IL.take_round_files()
        IL.maybe_collapse_paste("一句短话")
        assert IL.take_round_files() == []
