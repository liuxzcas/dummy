"""colors.py — 终端消息配色(前缀着色,内容默认)。

设计(2026-08-14):
- 语义层级:主输出最亮、次要层灰暗、动作层鲜艳、需决策层黄色
- 前缀着色:消息前缀(emoji + 标签)着色,内容保持终端默认——
  大段工具返回/代码保持原样,避免整屏色块
- 自动检测:stdout 不是 TTY(测试/管道)时自动无色,不污染断言
- 环境变量强制:DUMMY_COLOR=0 强制关闭,DUMMY_COLOR=1 强制开启

色值基于 VS Code Dark+ / One Dark 语义色,深色背景(#0C0C0C)
上对比度达标。
"""

import os
import sys

# 24-bit ANSI 前景色
_RESET = "\033[0m"

CYAN = "\033[38;2;86;182;194m"       # banner / 读取确认
GREEN = "\033[38;2;152;195;121m"     # 用户输入 / 成功
GRAY_DIM = "\033[38;2;127;132;142m"  # 思考中(loading)
GRAY = "\033[38;2;154;164;176m"      # 思考内容(reasoning)
BLUE = "\033[38;2;79;193;255m"       # 工具调用
SLATE = "\033[38;2;108;122;137m"     # 工具返回
WHITE = "\033[38;2;232;232;232m"     # Agent 回答
PURPLE = "\033[38;2;198;120;221m"    # 记忆
YELLOW = "\033[38;2;229;192;123m"    # 打断 / 确认 / 警告
RED = "\033[38;2;224;108;117m"       # 错误
NEUTRAL = "\033[38;2;171;178;191m"   # 命令输出

# 是否启用:环境变量强制优先,否则看 stdout 是否为 TTY
_env = os.environ.get("DUMMY_COLOR", "")
if _env == "0":
    _ENABLED = False
elif _env == "1":
    _ENABLED = True
else:
    try:
        _ENABLED = bool(sys.stdout.isatty())
    except Exception:
        _ENABLED = False


def paint(prefix: str, color_code: str) -> str:
    """给消息前缀着色。

    返回着色后的前缀(内容由调用方按原样拼接打印);
    禁用时原样返回,不产生任何 ANSI 码。
    """
    if not _ENABLED:
        return prefix
    return f"{color_code}{prefix}{_RESET}"


def _detect_readline() -> bool:
    """探测 readline 是否可用(用于 paint_prompt 决定要不要加 \\001/\\002)。

    为什么在 colors.py 里自己探测、而不是从 main.py 传进来:
      colors.py 是最底层模块(被 ui.py import),反过来 import main
      会形成循环依赖。而"readline 是否可用"是**进程级事实**,
      自己探测一次即可,不需要外部注入。

    平台差异:
      · Linux/macOS:CPython 内置 readline → 通常为 True
      · Windows:CPython 不提供;装了 pyreadline3 才为 True
    """
    try:
        import readline      # noqa: F401
        return True
    except ImportError:
        return False


# readline 是否可用:决定提示符要不要用 \001/\002 包裹 ANSI 码。
# 这里只探测一次(import 期的进程级事实,不会变)。
_HAS_READLINE = _detect_readline()


def paint_prompt(prefix: str, color_code: str) -> str:
    """给**输入提示符**着色 —— 与 paint() 的区别在于是否用 \\001/\\002 包裹。

    ============ 为什么提示符要特殊处理(2026-09-30 实测) ============
    直接把手涂色的字符串(含 \\033[…m)交给 input() 会踩第二个坑:
    readline 计算提示符宽度时,会把 ANSI 字节**也当成可见字符**,
    导致光标定位/擦除偏移(表现为输入时字符错位、退格擦不干净)。

    readline 的标准解法是用 \\001(SOH) … \\002(STX) 把每个转义序列包住 ——
    它见到这两个字节就跳过,不计宽度。
    实测:pyreadline3 的源码用的正则也是 `\\001?\\033\\[…m\\002?`(两个量词可选),
    说明这个约定在 GNU readline 和 pyreadline3 上都成立。

    ============ 为什么必须判断 _HAS_READLINE ============
    没有 readline 时,input() 不认识这两个字节,它们会**原样漏出去**:

        实测(Windows,无 readline):
            b'\\x01\\x1b[92m\\x02你 > \\x01\\x1b[0m\\x02'   ← 含 \\x01 \\x02
        终端显示:  ^A^[[92m^B你 > ^A^[[0m^B            ← 乱码

    而"不加包裹"在那种环境下本来就是对的:
    input() 直接把提示符写给终端,终端的 ANSI 解析自己知道
    \\033[92m 是颜色、不占宽度 —— 不需要额外标记。

    三种环境的行为(实测确认,无 readline 时字节与旧版完全一致):
      Linux (GNU readline)  → 加包裹  ✅
      Windows + pyreadline3 → 加包裹  ✅ (源码确认识别)
      Windows 无 readline   → 不加    ✅ (干净输出)
    """
    if not _ENABLED:
        return prefix
    if not _HAS_READLINE:
        return f"{color_code}{prefix}{_RESET}"
    return f"\001{color_code}\002{prefix}\001{_RESET}\002"
