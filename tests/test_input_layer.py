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
import re
import sys

import pytest

# 文件名形如 paste_20261019_143022_001.txt
date_re = re.compile(r"paste_\d{8}_\d{6}_\d{3}\.txt$")

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

    def test_bracketed_paste_end_to_end(self, tmp_path, monkeypatch):
        """端到端:粘贴多行短文本 → 一档内联,不落盘、不留占位符。

        ★ 行为在 2026-10-19 变了(用户定的两档方案):
          改前:一律存文件 + 缓冲区放占位符
          改后:小内容直接内联(对模型来说等同用户打的字),不落盘
        """
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from prompt_toolkit import PromptSession
        import input_layer as IL

        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        monkeypatch.setattr(IL, "_emit_notice", lambda n: None)
        monkeypatch.setattr(IL, "_current_model", lambda: "deepseek-v4-flash")
        IL.take_round_files()

        with create_pipe_input() as inp:
            sess = PromptSession(
                input=inp, output=DummyOutput(),
                key_bindings=IL._build_key_bindings())
            real = IL._get_session
            IL._get_session = lambda: sess
            try:
                payload = (chr(27) + "[200~" + chr(10).join(
                    ["第一行", "第二行", "第三行"]) + chr(27) + "[201~" + chr(13))
                inp.send_text(payload)
                out = IL.read_input("> ")
            finally:
                IL._get_session = real

        # 一档:原文内联进返回值,没有文件、没有占位符
        assert "第一行" in out and "第三行" in out, f"应内联原文: {out!r}"
        assert "[Pasted text" not in out, "不该再有占位符"
        assert IL.take_round_files() == [], "一档不落盘,不该登记文件"
        assert not list(tmp_path.glob("*.txt")), "一档不该写文件"

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
# ⑨ 粘贴分档:一档内联 / 二档存文件(2026-10-19 用户定的方案)
# ============================================================
class TestPasteTiers:
    """用户定的两档方案:

    · 一档(token 估算 < 模型窗口 × 15%):**直接内联**进 user 消息,
      不落盘、无"请读文件"指令 —— 对模型来说等同于用户自己打的字。
    · 二档(≥ 15%):存 pastes/paste_<时间戳>_<序号>.txt,只给路径,
      避免一次塞几十万 token 打爆上下文。

    为什么按 token 而不是字符数:用户说"按模型上下文能力来定,
    比如以窗口长度的 15% 来定"。粗估系数(CJK 1.5/字、ASCII 0.25/字)
    足够用于分档 —— 这不是精确计费。
    """

    def test_estimate_tokens_cjk_vs_ascii(self):
        import input_layer as IL
        # 100 个汉字 ≈ 150 token;100 个 ASCII ≈ 25 token
        assert 140 <= IL.estimate_tokens("字" * 100) <= 160
        assert 20 <= IL.estimate_tokens("a" * 100) <= 30
        assert IL.estimate_tokens("") == 0

    def test_inline_threshold_is_15pct_of_window(self):
        import input_layer as IL
        from llm import resolve_window_tokens
        win = resolve_window_tokens("deepseek-v4-flash")
        assert IL._inline_threshold("deepseek-v4-flash") == int(win * 0.15)

    def test_head_tail_excerpt(self):
        """用户要的格式:'已粘贴47行:"致尊敬的...此致敬礼"(直接发送)'。"""
        import input_layer as IL
        text = "致尊敬的各位领导" + "正文" * 50 + "此致敬礼"
        ex = IL._head_tail_excerpt(text, n=5)
        assert ex.startswith("致尊敬的") and ex.endswith("此致敬礼")
        assert "…" in ex

    def test_head_tail_excerpt_short_text(self):
        """短文本不做省略。"""
        import input_layer as IL
        assert IL._head_tail_excerpt("很短", n=12) == "很短"

    def test_filename_uses_timestamp_and_seq(self, tmp_path, monkeypatch):
        """用户 2026-10-19:"时间戳+序号。反正对完话就清了,不需要可读性。"。"""
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        path = IL._save_paste("x\ny")
        name = os.path.basename(path)
        assert name.startswith("paste_"), name
        assert name.endswith(".txt"), name
        # 形如 paste_20261019_143022_001.txt
        assert date_re.search(name), f"应含时间戳+序号: {name}"

    def test_crlf_preserved(self, tmp_path, monkeypatch):
        """粘贴内容可能来自 Windows 网页/PDF,换行要原样保留。"""
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        payload = "第一行\r\n第二行\r\n"
        path = IL._save_paste(payload)
        assert open(path, "rb").read() == payload.encode("utf-8")


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
# ⑪ 临时文件的生命周期
# ============================================================
class TestRoundFileLifecycle:
    """用户 2026-10-19:"发送完消息可以自动清除临时文件。现在完全不清。"

    ============ 为什么必须等本轮结束才删 ============
    时序:粘贴 → 存文件 → 发送 → 模型 read_file → 读取 → 回复 → 本轮结束
                                                          ↑ 只有这里能删
    删早了模型会读不到文件(报不存在)。
    """

    def test_save_registers_for_cleanup(self, tmp_path, monkeypatch):
        """二档存盘时要登记 —— 否则轮末不知道要删谁。"""
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        IL.take_round_files()
        path = IL._save_paste("第一行\n第二行")
        IL._round_files.append(path)        # 粘贴事件里会这么做
        assert path in IL.take_round_files()

    def test_take_round_files_clears_after_reading(self, tmp_path, monkeypatch):
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        IL.take_round_files()
        path = IL._save_paste("a\nb")
        IL._round_files.append(path)
        assert IL.take_round_files() != []
        assert IL.take_round_files() == [], "第二次取应为空(已消费)"

    def test_cleanup_deletes_files(self, tmp_path, monkeypatch):
        import input_layer as IL
        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        path = IL._save_paste("x\ny")
        assert os.path.isfile(path)
        assert IL.cleanup_files([path]) == 1
        assert not os.path.isfile(path)

    def test_cleanup_tolerates_missing_files(self, tmp_path):
        import input_layer as IL
        assert IL.cleanup_files([str(tmp_path / "不存在.txt")]) == 0

    def test_core_cleans_up_after_chat(self, tmp_path, monkeypatch):
        """chat() 结束后,本轮登记的临时文件应被删除。"""
        import input_layer as IL
        import core
        from conftest import ScriptedLLM
        from tools import create_default_registry

        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        IL.take_round_files()
        path = IL._save_paste("临时\n内容")
        IL._round_files.append(path)
        assert os.path.isfile(path)

        agent = core.DummyAgent(ScriptedLLM([]), create_default_registry())
        agent.chat("测试")

        assert not os.path.isfile(path), "本轮结束后临时文件应被清理"





# ============================================================
# ⑬ 全项目不许裸调 input()（丢字的根本防线）
# ============================================================
class TestNoBareInput:
    """用户实测:"有时还是不行。问题出在 web_search 工具询问用户输入
    api key 时。"

    ============ 为什么这条测试比修某一个点更重要 ============
    丢字的根因是【两处同时读控制台】:
        /p 打断监听线程(msvcrt 轮询) ←→ 谁在 input() 就抢谁
    所以只要项目里还有一处裸 input(),那条路径就会丢字。

    修一个补一个是不行的 —— 必须**从结构上保证**:
    所有读用户输入的地方都走 input_layer(它负责 pause/resume 监听器)。

    ============ 为什么用 AST 而不是正则(2026-10-19 踩坑) ============
    初版用正则扫 "input(" —— 误报一堆:
      · 注释/docstring 里的说明文字 ("input() 不会走 readline")
      · **函数名后缀**(`web_search_handler(` 里含 "input(" ?不,是
        子串匹配把 `def web_search_handler(` 也算进去了)
    AST 只看真实的 Call 节点,不会误伤。
    """

    # 允许裸 input() 的位置及理由
    ALLOWED = {
        # 启动期的 API Key / Base URL 询问 —— 此时 _InterruptListener
        # 还没创建(core 是在 DummyAgent.__init__ 里才起线程的),
        # 不存在抢控制台的竞争。
        ("main.py", "input"),
    }

    def _scan(self):
        """用 AST 扫裸 input() 调用(自动跳过注释/docstring)。"""
        import ast
        import pathlib as pl

        root = pl.Path(__file__).resolve().parent.parent
        offenders = []
        for py in root.rglob("*.py"):
            rel = py.relative_to(root).as_posix()
            if rel.startswith(("tests/", ".git", "__pycache__")):
                continue
            if rel == "input_layer.py":
                continue            # 唯一允许真正调用 input 的地方
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "input"):
                    offenders.append(f"{rel}:{node.lineno}")
        return offenders

    def test_no_bare_input_outside_allowlist(self):
        """除启动期询问外,项目里不该有裸 input()。"""
        offenders = self._scan()
        # main.py 的两处是启动期,放行
        offenders = [o for o in offenders
                     if not (o.startswith("main.py:") and o in {
                         f"main.py:{self._startup_line()}",
                     })]
        unexpected = [o for o in offenders
                      if not o.startswith("main.py:")]
        msg = ("发现裸调 input() —— 它会和 /p 监听线程抢控制台导致丢字;"
               "请改用 input_layer.read_plain(prompt, agent)。违规处: "
               + ", ".join(unexpected))
        assert not unexpected, msg

    @staticmethod
    def _startup_line():
        """main.py 里第一个 input() 的行号(API Key),用于放行判断。"""
        import ast
        import pathlib as pl
        src = (pl.Path(__file__).resolve().parent.parent / "main.py").read_text(
            encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "input"):
                return node.lineno
        return -1

    def test_web_search_reads_env_not_prompt(self):
        """web_search 不该再问用户要 key(每次问一遍 + 抢控制台)。"""
        import ast
        import inspect
        import tools.web_search as ws

        src = inspect.getsource(ws.web_search_handler)
        # 用 AST 判断有没有真实调用 input(),而不是查子串
        has_input = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "input"
            for n in ast.walk(ast.parse(src)))
        assert not has_input, "web_search 不该调 input()"
        assert "TAVILY_API_KEY" in src and "environ" in src, \
            "应从环境变量 TAVILY_API_KEY 读取"


# ============================================================
# ⑭ 读输入期间必须暂停 /p 打断监听（丢字 bug，2026-10-19）
# ============================================================
class TestInterruptListenerPausedDuringInput:
    """用户实测:粘贴单行短文本被截断成 "https://io1#"、"ht:c1n"。

    根因:core 的 _InterruptListener 后台线程持续用 msvcrt 抢控制台,
    与 prompt_toolkit 的 Win32Input 分吃同一个字符流 → 丢字。
    而且 pause() 原来只在 _confirm_provider 里调,主循环漏了。
    """

    def test_read_pauses_and_resumes_listener(self, monkeypatch):
        import input_layer as IL
        calls = []

        class FakeListener:
            def pause(self, timeout=0.5):
                calls.append("pause")

            def resume(self):
                calls.append("resume")

        class FakeAgent:
            _interrupt_listener = FakeListener()

        monkeypatch.setattr(IL, "read_input", lambda p: "测试输入")
        raw, _drop = IL.read_input_with_drop("> ", FakeAgent())
        assert calls == ["pause", "resume"], f"应成对暂停/恢复: {calls}"
        assert raw == "测试输入"

    def test_resume_called_even_if_input_raises(self, monkeypatch):
        """抛异常(Ctrl+C)→ 也必须 resume,否则 /p 永久失效。"""
        import input_layer as IL
        calls = []

        class FakeListener:
            def pause(self, timeout=0.5):
                calls.append("pause")

            def resume(self):
                calls.append("resume")

        class FakeAgent:
            _interrupt_listener = FakeListener()

        def boom(p):
            raise KeyboardInterrupt

        monkeypatch.setattr(IL, "read_input", boom)
        with pytest.raises(KeyboardInterrupt):
            IL.read_input_with_drop("> ", FakeAgent())
        assert calls == ["pause", "resume"]

    def test_pause_waits_for_listener_to_stop(self):
        """pause() 必须等后台线程真停下,而不是设个标志就返回。"""
        from core import _InterruptListener
        l = _InterruptListener()
        try:
            l.pause()
            assert l._paused_seen.is_set()
        finally:
            l.resume()
            l._stop = True


# ============================================================
# ⑮ 粘贴不能抹掉用户已打的字（2026-10-30 用户实测）
# ============================================================
class TestPastePreservesTypedText:
    """用户实测:"粘贴动作会直接抹掉用户原先打好的字"。

    根因:_on_paste 里无条件 `current_buffer.reset()` 清空行缓冲。
    正确做法:缓冲区保持不动(用户打的字、光标位置都不变),
    粘贴内容走 _pending_inline / _pending_files,回车时合并。
    """

    def _run(self, prefix, pasted, suffix, tmp_path, monkeypatch):
        """模拟:打 prefix → 粘贴 pasted → 打 suffix → 回车。"""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        import input_layer as IL

        monkeypatch.setattr(IL, "_PASTE_DIR", str(tmp_path))
        monkeypatch.setattr(IL, "_emit_notice", lambda n: None)
        monkeypatch.setattr(IL, "_current_model", lambda: "deepseek-v4-flash")

        with create_pipe_input() as inp:
            sess = PromptSession(
                input=inp, output=DummyOutput(),
                key_bindings=IL._build_key_bindings())
            real = IL._get_session
            IL._get_session = lambda: sess
            try:
                payload = (prefix + chr(27) + "[200~" + pasted
                           + chr(27) + "[201~" + suffix + chr(13))
                inp.send_text(payload)
                return IL.read_input("> ")
            finally:
                IL._get_session = real

    def test_typed_before_paste_survives(self, tmp_path, monkeypatch):
        """先打字再粘贴 → 打的字必须还在。"""
        out = self._run("我打的字", "第一行\n第二行", "", tmp_path, monkeypatch)
        assert "我打的字" in out, f"用户打的字被抹掉了: {out!r}"
        assert "第一行" in out and "第二行" in out

    def test_typed_after_paste_also_kept(self, tmp_path, monkeypatch):
        """粘贴后再打字 → 两者都在。"""
        out = self._run("", "粘贴内容\n第二行", "后面打的", tmp_path, monkeypatch)
        assert "粘贴内容" in out and "后面打的" in out

    def test_typed_both_sides(self, tmp_path, monkeypatch):
        """前后都打字 → 三部分都在,且顺序为 前 / 粘贴 / 后。"""
        out = self._run("前面", "中间内容", "后面", tmp_path, monkeypatch)
        assert "前面" in out and "中间内容" in out and "后面" in out
        assert out.index("前面") < out.index("中间内容") < out.index("后面")

    def test_newlines_preserved_in_inline(self, tmp_path, monkeypatch):
        """内联内容必须保留换行(用户实测:'粘贴的内容会丢失换行号')。"""
        out = self._run("", "第一行\n第二行\n第三行", "", tmp_path, monkeypatch)
        assert out.count("\n") == 2, f"换行丢了: {out!r}"
