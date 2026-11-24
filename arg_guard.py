"""
arg_guard.py — 工具参数截断的证据保全与提示

============ 为什么需要它(2026-11-18) ============
日志实测(logs/conversation_20260814_001407.json #62):
模型生成 write_file 的 10478 字符参数,在 "ax.grid(" 处被截断。
后果链:
    截断 → path 排在参数最后(占实测 88.7%)→ path 随之丢失
    → _parse_tool_arguments 策略3 用正则"抢救"出 {"content": 残段}
    → registry 报 TypeError: missing 1 required positional argument
模型拿到这条消息只能猜"我少给了 path"(治标),猜不到"是内容太长被截断"(治本)。
同一会话 #64 它靠运气猜对,主动拆成两次写。

============ 它做什么 ============
截断/解析失败时,把**原始残缺串**与断点证据落到 garage 下的 partial 文件,
然后返回一条让模型能自己判断的消息:
    - 断在哪个字符、尾部长什么样
    - 盘上哪个文件存着完整证据
    - 缺哪些 key
    - 目标文件**未写入**(不落半成品)

============ 核心边界:框架不判断"内容完整不完整" ============
本模块只做**结构判据**(JSON 能否解析、引号/括号是否配平、缺哪些 key)。
"这段代码写完了吗"是语义判断,归模型 —— 模型自己 read_file 那个 partial 去判断。
这与 turn_context.py 的原则一致:协议归代码,语义归模型。
(2026-09 的 verify_stop 就是因为替模型做语义判断被推翻,见
 docs/91-experiences/verification-gate-rollback-star.md)

============ 推广到其他工具 ============
判定逻辑不绑定 write_file:
  - 任何工具的 arguments 解析失败都会进入 guard()
  - 是否保全由 TOOLS_WITH_PARTIAL 白名单控制(当前只有 write_file)
  - partial 文件名带工具名,证据字段通用
将来 terminal 的 command、patch 的 content 要纳入,只需把工具名加进白名单。
"""

from __future__ import annotations

import json
import os
import secrets
import time

import workspace

# 启用 partial 保全的工具(白名单)。
# 为什么用白名单而不是全开:partial 文件是给模型看的证据,只有"参数可能很长、
# 且截断后确实值得续写"的工具才需要。终端命令被截断时重写一遍更简单。
TOOLS_WITH_PARTIAL = frozenset({"write_file"})

# 证据文件保留上限(按数量,最旧的先删)。garage 已 385MB,不能让证据文件失控。
MAX_PARTIALS = 20

# 尾部/头部截取长度(字符)。给模型判断断点用,不需要全文。
EDGE = 200

# 连续截断多少次后加"换个写法"的建议。
# ============ 为什么要有这个(2026-11-23 实测) ============
# 日志里有一条 14097 字符的参数**在 6 个不同会话里以完全相同的内容失败**
# (哈希一致),全部缺 path。说明不是长度问题(max_tokens=8192,它只占 ~4700 token),
# 而是**某种内容形态**让模型反复生成非法的 JSON 字符串:
# 那一份是含大量中文 + 嵌套单双引号 + 反斜杠转义的列表结构。
# 长度阈值救不了它(3577 字符就开始失败,而 16724 字符也成功过),
# 能起作用的是提示模型"这份内容的表达方式本身有问题"。
REPEAT_HINT_THRESHOLD = 2

# 单进程内的截断计数(成功一次就清零)。
# 为什么用进程级而不是会话级:同类内容重复失败都发生在**同一次 chat 轮**里
# (模型被打断后立刻重试),进程级足够,也避免把计数写进 DB 增加耦合。
_truncation_streak = 0


def reset_streak() -> None:
    """工具成功执行后调用,清零连续截断计数。"""
    global _truncation_streak
    _truncation_streak = 0


def _partial_dir() -> str:
    d = os.path.join(workspace.ensure(), "_partials")
    os.makedirs(d, exist_ok=True)
    return d


def _prune(keep_name: str) -> None:
    """只保留最近 MAX_PARTIALS 个证据文件。"""
    d = _partial_dir()
    files = sorted(
        (os.path.join(d, n) for n in os.listdir(d) if n.endswith(".json")),
        key=os.path.getmtime,
    )
    for p in files[:-MAX_PARTIALS]:
        if os.path.basename(p) != keep_name:
            try:
                os.remove(p)
            except OSError:
                pass


def looks_truncated(raw: str) -> bool:
    """结构判据:这段串是否明显被截断。

    三条任一条成立即认为可疑(不追求严谨 —— 后面还有 json 解析兜底):
      1. 引号数为奇数(字符串没闭合)
      2. 括号计数不平衡 { }
      3. 结尾不是 } 或 ](正常 JSON 对象/数组必须以闭合符收尾)
    只看结构,不看内容语义。
    """
    if not raw:
        return False
    if raw.count('"') % 2:
        return True
    if raw.count("{") != raw.count("}") or raw.count("[") != raw.count("]"):
        return True
    return raw.rstrip()[-1:] not in ("}", "]")


def save_partial(tool_name: str, raw: str, parsed: dict | None,
                 missing: list[str] | None = None) -> str | None:
    """把截断证据写到 garage/_partials/,返回文件路径(失败返回 None)。

    证据字段刻意包含 raw_head / raw_tail:模型靠这两段判断"断在哪、缺什么",
    比只看解析结果准。

    文件名必须唯一:只用"秒级时间戳 + pid"会在同一秒内互相覆盖(实测 6 次调用
    只落 1 个文件),所以再加 6 位随机后缀。
    """
    if tool_name not in TOOLS_WITH_PARTIAL:
        return None
    try:
        name = (f"partial_{tool_name}_{time.strftime('%Y%m%d_%H%M%S')}"
                f"_{os.getpid()}_{secrets.token_hex(3)}.json")
        path = os.path.join(_partial_dir(), name)
        payload = {
            "tool": tool_name,
            "raw_length": len(raw),
            "truncated": looks_truncated(raw),
            "raw_head": raw[:EDGE],
            "raw_tail": raw[-EDGE:],
            "parsed_keys": sorted(parsed or {}),
            "parsed_sizes": {
                k: (len(v) if isinstance(v, str) else None)
                for k, v in (parsed or {}).items()
            },
            "missing_keys": list(missing or []),
            "note": "文件未写入。已生成内容与断点证据都在本文件里，"
                    "由模型自己判断缺什么、续写还是重写。",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        _prune(name)
        return path
    except OSError:
        return None


def truncation_message(tool_name: str, raw: str, partial_path: str | None,
                       missing: list[str] | None = None,
                       streak: int = 1) -> str:
    """给模型的提示:陈述事实 + 指出证据在哪 + 不替它决定怎么办。

    措辞上**不使用判决性语言**(不要说"你应该续写")—— 只摆事实:
    断在哪、缺什么、证据在哪、文件没写。怎么办由模型判断。
    """
    tail = raw[-EDGE:].replace("\n", "\\n")
    lines = [
        f"[参数被截断] {tool_name} 的参数串共 {len(raw)} 字符，结构不完整"
        "（引号/括号未闭合或结尾无右花括号）。目标文件未写入。",
        f"  断点尾部: …{tail}",
    ]
    if missing:
        lines.append(f"  缺失的 key: {', '.join(missing)}")
    if partial_path:
        try:
            rel = os.path.relpath(partial_path, os.getcwd()).replace("\\", "/")
        except ValueError:
            # Windows 跨盘符时 relpath 抛异常 —— 退回绝对路径(路径照样能用)
            rel = partial_path.replace("\\", "/")
        lines.append(f"  已生成部分与断点证据存于: {rel}")
        lines.extend([
            "",
            "  两条路径，都不需要重发全部内容：",
            "  ① 重写（推荐）：read_file 上面的证据文件取回已生成的内容，补完断点之后的部分，",
            '     再一次性 write_file(mode="overwrite") 整份写入。',
            '  ② 分段：先 write_file(mode="overwrite") 写前半，再 write_file(mode="append") 写后半。',
            "     ⚠ append 只在文件已存在时才是追加；文件不存在时会新建，前半会丢。",
        ])
    else:
        lines.append("  （证据未落盘）")
    if streak >= REPEAT_HINT_THRESHOLD:
        lines.extend([
            "",
            f"  这是本进程内连续第 {streak} 次截断。实测里有一种内容会让同样的参数"
            "反复生成失败（不是长度问题：既出现过 3577 字符就失败，也出现过 16724 字符成功）：",
            "  含大量嵌套引号、反斜杠转义、中文标点的长列表/字典字面量。",
            "  若你重试后仍然截断，可以换个表达方式再写：内容里的字符串改用三引号或",
            "  拆成独立的数据文件，减少同一行里的引号层数。",
        ])
    return "\n".join(lines)


def guard(tool_name: str, raw: str, parsed: dict | None,
          missing: list[str] | None = None) -> str | None:
    """统一入口:判断是否需要保全,返回给模型的提示串;不需要则返回 None。

    调用方(core)在 _parse_tool_arguments 之后、dispatch 之前调一次。
    命中时累加连续截断计数;工具成功执行后由调用方 reset_streak() 清零。
    """
    global _truncation_streak
    if tool_name not in TOOLS_WITH_PARTIAL:
        return None
    if not raw or not looks_truncated(raw):
        return None
    _truncation_streak += 1
    path = save_partial(tool_name, raw, parsed, missing)
    return truncation_message(tool_name, raw, path, missing, _truncation_streak)
