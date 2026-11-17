"""清理 session.db 里历史消息中的终端控制序列。

2026-11-05 起 terminal 工具输出尾部带着 \\x1b[H\\x1b[2J\\x1b[3J
(清屏 + 清 scrollback),已存进 db。恢复旧会话时每次打印都会清屏。

代码侧已修(terminal 入口剥 + ui 打印剥 + resume 载入剥),
这个脚本处理**存量**:把 db 里已有的脏数据洗干净,免得
任何没走 resume_session 的路径(直接 load_history)还带着。

用法:
    python scripts/clean_messages_esc.py          # 只看,不改
    python scripts/clean_messages_esc.py --apply  # 真写
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ui import _strip_control       # noqa: E402

DB = ROOT / "session.db"
ESC = chr(27)


def main(apply: bool) -> None:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT id, session_id, content FROM messages "
                "WHERE content LIKE ?", ("%" + ESC + "%",))
    rows = cur.fetchall()
    print(f"待清理 {len(rows)} 条消息")

    changes = []
    for mid, sid, content in rows:
        new = _strip_control(content)
        if new != content:
            changes.append((mid, content, new))

    print(f"实际会改动 {len(changes)} 条")
    for mid, old, new in changes[:3]:
        print(f"\n  id={mid}")
        print(f"    旧尾部: {old[-40:]!r}")
        print(f"    新尾部: {new[-40:]!r}")

    if not apply:
        print("\n(只预览,未写入。要真写加 --apply)")
        return

    cur.executemany("UPDATE messages SET content = ? WHERE id = ?",
                    [(new, mid) for mid, _old, new in changes])
    con.commit()
    print(f"\n已写入 {cur.rowcount if cur.rowcount >= 0 else len(changes)} 条")

    # 复查
    cur.execute("SELECT COUNT(*) FROM messages WHERE content LIKE ?",
                ("%" + ESC + "%",))
    print(f"剩余含 ESC 的消息: {cur.fetchone()[0]} 条")
    con.close()


if __name__ == "__main__":
    main("--apply" in sys.argv)
