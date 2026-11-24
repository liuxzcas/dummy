"""
scripts/logs_dedup.py — 清理 logs/ 里的测试级重复会话

判定(两条正交):
  A 测试型:用户消息去重后全是标记词(测试/继续/你好/开始/记住这句话/做两件事/随便问/hi/嗨/执行…),
    或以"修好测试/测试拒绝/问题一/问题二"开头。这类是开发自测的产物。
  B 真实任务:A 之外的一切。

去重(在各自类别内,同首句才比):
  按(消息数, 字节数)从大到小贪心保留;后来者若其 ≥90% 消息(带重数)能在保留者里找到 → 判为重复。
  这样一定留下"消息最全"的那份,不会把全的删了留个残的。

用法:
  python scripts/logs_dedup.py            # 只出报告(默认)
  python scripts/logs_dedup.py --apply    # 真删
"""

import collections
import json
import os
import sys

LOGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
MARK = ("测试", "继续", "你好", "开始", "记住这句话", "做两件事", "随便问",
        "hi", "嗨", "执行", "再问一句", "再聊一句", "问题一", "问题二")
TESTISH = ("修好测试", "把测试修好", "测试拒绝")
KEEP_FIELDS = ("role", "content", "tool_calls", "tool_call_id")


def _text(v):
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def scan(root=None):
    items = {}
    root = root or LOGS
    for name in sorted(os.listdir(root)):
        if not name.startswith("conversation_"):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            print(f"  跳过坏文件 {name}: {e}")
            continue
        msgs = [{k: v for k, v in m.items() if k in KEEP_FIELDS}
                for m in raw if m.get("role") != "system"]
        users = [_text(m.get("content")) for m in raw if m.get("role") == "user"]
        human, seen = [], set()
        for u in users:
            if u.startswith("[本轮运行情况]"):
                continue
            if u.strip() and u.strip() not in seen:
                seen.add(u.strip())
                human.append(u.strip())
        keys = [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in msgs]
        is_test = bool(human) and (all(h in MARK for h in human)
                                   or any(h.startswith(TESTISH) for h in human))
        items[name] = {"size": os.path.getsize(path), "keys": keys,
                       "counts": collections.Counter(keys),
                       "first": human[:1], "is_test": is_test, "users": len(human)}
    return items


def dups(pool, items, thr=0.90):
    """返回 (保留, {删除: (被谁吸收, 覆盖率)})。

    贪心按(消息数, 字节数)从大到小;但"吸收者"必须严格更大或更全,
    否则两个同长度文件会互相吸收、双双留下。
    """
    order = sorted(pool, key=lambda n: (-len(items[n]["keys"]), -items[n]["size"]))
    kept, drop = [], {}
    for big in order:
        if big in drop:
            continue
        kept.append(big)
        b = items[big]
        for small in order:
            if small in drop or small == big:
                continue
            s = items[small]
            if s["first"] != b["first"] or not s["keys"]:
                continue
            # 内容完全一致(常见于同一秒重跑):下标靠前的那个当保留者,其余删
            if s["keys"] == b["keys"]:
                if order.index(small) > order.index(big):
                    drop[small] = (big, 1.0)
                continue
            if (len(b["keys"]), b["size"]) <= (len(s["keys"]), s["size"]):
                continue  # big 不比 small 更全,没资格当吸收者
            hit = sum(min(c, b["counts"][k]) for k, c in s["counts"].items())
            cov = hit / len(s["keys"])
            if cov >= thr:
                drop[small] = (big, cov)
    return kept, drop


def plan(root=None, thr=0.90):
    """纯计算:返回 (items, 保留, 删除) —— 不碰磁盘,便于测试。"""
    items = scan(root)
    test = [n for n, i in items.items() if i["is_test"]]
    real = [n for n, i in items.items() if not i["is_test"]]
    kt, dt = dups(test, items, thr)
    kr, dr = dups(real, items, thr)
    return items, {**dt, **dr}, {"test": (kt, dt), "real": (kr, dr)}


def main():
    apply = "--apply" in sys.argv
    root = LOGS
    items, drop, parts = plan(root)
    (kt, dt), (kr, dr) = parts["test"], parts["real"]
    total = sum(i["size"] for i in items.values())
    freed = sum(items[n]["size"] for n in drop)

    print(f"logs/: {len(items)} 个 conversation, {total / 1e6:.1f} MB")
    print(f"  测试型 {len(kt) + len(dt)} → 留 {len(kt)} 删 {len(dt)}")
    print(f"  真实任务 {len(kr) + len(dr)} → 留 {len(kr)} 删 {len(dr)}")
    print(f"  合计删 {len(drop)} 个, 释放 {freed / 1e6:.1f} MB, 剩 {len(items) - len(drop)} 个")

    print("\n  真实任务里删除的最大 10 个(被谁吸收):")
    for n in sorted(dr, key=lambda x: -items[x]["size"])[:10]:
        big, cov = dr[n]
        print(f"    {items[n]['size'] / 1e6:6.2f}MB cov={cov:.2f} {n} → {big}")

    if not apply:
        print("\n(只出报告,没删。加 --apply 真删)")
        return
    for n in drop:
        os.remove(os.path.join(root, n))
    left = sum(os.path.getsize(os.path.join(root, n))
               for n in os.listdir(root) if os.path.isfile(os.path.join(root, n)))
    print(f"\n已删除 {len(drop)} 个;logs/ 现在 {left / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
