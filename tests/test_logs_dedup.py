"""
tests/test_logs_dedup.py — scripts/logs_dedup.py:清理 logs/ 的可删判定

覆盖(全部在 tmp_path 里造合成日志,不碰真实 logs/):
1. 同内容重跑 → 只留字节最大的一个
2. ≥90% 内容是另一份的子集 → 删小的、留全的;覆盖率 <90% 的两份都留
3. 同一份内容长度相等 → 不互相吸收、不双删(留一份)
4. 首句不同 → 即使内容重合也不归并
5. 坏 JSON / 0 字节 / 非 conversation_ 前缀 → 跳过或忽略,不崩
6. 测试型与真实任务分开判断,同一内容不会跨类互相吸收
7. 幂等:apply 之后重跑,删 0 个
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pytest  # noqa: E402

import logs_dedup as ld  # noqa: E402


pytestmark = pytest.mark.fast   # 测试分组见 pytest.ini


def msg(role, content):
    return {"role": role, "content": content}


def convo(users, tool_rounds=0, pad=""):
    """system + 每轮 user/assistant(/tool) —— tool 消息用来控体积和内容差异。"""
    out = [msg("system", "SYSTEM PROMPT " + pad)]
    for i, u in enumerate(users):
        out.append(msg("user", u))
        out.append(msg("assistant", f"reply to {u}"))
        for r in range(tool_rounds):
            out.append(msg("tool", f"tool out {i}-{r} {pad}"))
            out.append(msg("assistant", f"after tool {i}-{r}"))
    return out


def write(root, name, payload):
    path = os.path.join(root, name)
    if isinstance(payload, str):
        open(path, "w", encoding="utf-8").write(payload)
    else:
        open(path, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False))
    return path


@pytest.fixture()
def logs(tmp_path):
    return tmp_path


def test_identical_keeps_largest(logs):
    base = convo(["开始"], tool_rounds=1)
    write(logs, "conversation_20260101_000001.json", base)
    write(logs, "conversation_20260101_000002.json", base)
    write(logs, "conversation_20260101_000003.json", base)
    items, drop, _ = ld.plan(str(logs))
    assert len(items) == 3
    assert len(drop) == 2, drop  # 留一份,删两份
    kept = [n for n in items if n not in drop]
    assert len(kept) == 1


def test_subset_90_percent_deletes_small_keeps_full(logs):
    full = ["开始"] + [f"step {i}" for i in range(10)]
    write(logs, "conversation_20260101_000001.json", convo(full))
    write(logs, "conversation_20260101_000002.json", convo(full[:9]))  # 9/10 轮是子集
    items, drop, _ = ld.plan(str(logs))
    assert list(drop) == ["conversation_20260101_000002.json"], (drop, list(items))
    assert drop["conversation_20260101_000002.json"][0] == "conversation_20260101_000001.json"
    assert drop["conversation_20260101_000002.json"][1] >= 0.90


def test_under_90_percent_kept(logs):
    a = ["开始", "alpha", "beta"]
    b = ["开始", "gamma", "delta"]  # 只有 1/3 重合
    write(logs, "conversation_20260101_000001.json", convo(a))
    write(logs, "conversation_20260101_000002.json", convo(b))
    _, drop, _ = ld.plan(str(logs))
    assert drop == {}, drop


def test_equal_length_do_not_absorb_each_other(logs):
    # 同消息数(10)、同字节(438)、互相覆盖恰好 90%:谁也没比谁更全,
    # 只有"严格更全"的才有资格当吸收者 —— 两份都留。
    # (去掉这道判断,两份会互相吸收、其中一份被误删)
    a = [msg("system", "SYS"), msg("user", "开始")] + [msg("assistant", f"s{i}") for i in range(8)] + [msg("assistant", "AAA")]
    b = [msg("system", "SYS"), msg("user", "开始")] + [msg("assistant", f"s{i}") for i in range(8)] + [msg("assistant", "BBB")]
    write(logs, "conversation_20260101_000001.json", a)
    write(logs, "conversation_20260101_000002.json", b)
    items, drop, _ = ld.plan(str(logs))
    assert len({len(v["keys"]) for v in items.values()}) == 1  # 同消息数
    assert len({v["size"] for v in items.values()}) == 1  # 同字节
    assert drop == {}, drop


def test_identical_content_dedupes_to_one(logs):
    # 与上一条互补:内容一模一样的多份,仍要收成一份
    body = convo(["开始"], tool_rounds=1)
    for i in range(1, 4):
        write(logs, f"conversation_20260101_00000{i}.json", body)
    items, drop, _ = ld.plan(str(logs))
    assert len(items) == 3 and len(drop) == 2, drop


def test_different_first_utterance_not_merged(logs):
    # 同首句 → 归并;首句不同 → 即使其余 90% 相同也各留一份
    merged = [msg("system", "SYS"), msg("user", "问题甲"), msg("assistant", "r0")]
    merged2 = [msg("system", "SYS"), msg("user", "问题乙"), msg("assistant", "r0")]
    for body in (merged, merged2):
        body.extend(msg("assistant", f"com{i}") for i in range(8))
    merged2.append(msg("assistant", "ONLY-B"))
    write(logs, "conversation_20260101_000001.json", merged)
    write(logs, "conversation_20260101_000002.json", merged2)
    _, drop, _ = ld.plan(str(logs))
    assert drop == {}, drop  # 首句不同,不归并(去掉首句判断这里会误删一份)

    # 同一首句、同样的 90% 重合 → 归并
    for n in ("conversation_20260101_000001.json", "conversation_20260101_000002.json"):
        os.remove(os.path.join(str(logs), n))
    same = [msg("system", "SYS"), msg("user", "问题甲"), msg("assistant", "r0")]
    same.extend(msg("assistant", f"com{i}") for i in range(8))
    same_more = same + [msg("assistant", "ONLY-B")]
    write(logs, "conversation_20260101_000001.json", same)
    write(logs, "conversation_20260101_000002.json", same_more)
    _, drop, _ = ld.plan(str(logs))
    assert list(drop) == ["conversation_20260101_000001.json"], drop


def test_bad_and_foreign_files_skipped(logs):
    write(logs, "conversation_20260101_000001.json", convo(["开始"]))
    write(logs, "conversation_20260101_000002.json", "{not json")
    write(logs, "conversation_20260101_000003.json", "")
    write(logs, "memory.jsonl", '{"a":1}\n')
    write(logs, "notes.txt", "hello")
    items, drop, _ = ld.plan(str(logs))
    assert list(items) == ["conversation_20260101_000001.json"], list(items)
    assert drop == {}


def test_test_and_real_classified_separately(logs):
    # 同一内容:一份是测试词、一份不是 → 不同类,不互相吸收
    write(logs, "conversation_20260101_000001.json", convo(["测试"]))
    write(logs, "conversation_20260101_000002.json", convo(["测试", "帮我看看这个文件"]))
    items, drop, parts = ld.plan(str(logs))
    assert items["conversation_20260101_000001.json"]["is_test"] is True
    assert items["conversation_20260101_000002.json"]["is_test"] is False
    assert drop == {}, drop
    assert len(parts["test"][0]) == 1 and len(parts["real"][0]) == 1


def test_apply_then_idempotent(logs, monkeypatch, capsys):
    base = convo(["继续"], tool_rounds=1)
    write(logs, "conversation_20260101_000001.json", base)
    write(logs, "conversation_20260101_000002.json", base)
    monkeypatch.setattr(ld, "LOGS", str(logs))
    monkeypatch.setattr(sys, "argv", ["logs_dedup.py", "--apply"])
    ld.main()
    left = [n for n in os.listdir(str(logs)) if n.startswith("conversation_")]
    assert len(left) == 1, left
    capsys.readouterr()
    ld.main()  # 第二次:删 0
    assert "合计删 0 个" in capsys.readouterr().out
