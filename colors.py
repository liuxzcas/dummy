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
