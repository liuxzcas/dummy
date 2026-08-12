"""Phase 2 综合回归集(LongMemEval 五类题型,真实模型)。

用法:
    DUMMY_API=sk-xxx python scripts/phase2_regression.py [--limit N]

每题流程:
    1. 临时库 + 会话 A:按 turns 逐轮真实对话(对话结束自动抽取记忆)
    2. 会话 B(新会话):提问(chat 入口自动注入记忆/FTS 检索)
    3. 判定:norm() 去空白 + 多期望列表 + all()(与质量门一致)

五类题型(见 docs/phase2-test-suite.md §7):
    long_context / statistic / post_hoc / conversation / personal

依赖链(回归命门):
    conversation 题依赖 FTS 检索(细节→记忆→注入检索命中)
    personal 题依赖记忆注入(事实只在会话 A,会话 B 从未见过)
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import DummyAgent  # noqa: E402
from llm import LLMClient  # noqa: E402
from session_store import SessionStore  # noqa: E402
from tools import ToolRegistry  # noqa: E402

# ---------------------------------------------------------------
# 题目定义(五类各 6 题,共 30 题)
# turns: 会话 A 的用户输入序列(纯文本对话,不调工具)
# question: 会话 B 的提问
# expects: 判定期望(全部必须出现在回答中,norm 后)
# ---------------------------------------------------------------
CASES = [
    # ---- 1. long_context 长上下文问答(跨段整合) ----
    {"type": "long_context", "name": "技术栈组合", "turns": ["前端框架用 Vue", "后端用 FastAPI 开发"],
     "question": "前后端分别用什么技术栈?", "expects": ["Vue", "FastAPI"]},
    {"type": "long_context", "name": "预算与日期", "turns": ["项目预算是 3000 元", "上线日期定在 8 月 20 日"],
     "question": "项目的预算和上线日期是什么?", "expects": ["3000", "8 月 20 日"]},
    {"type": "long_context", "name": "测试分工", "turns": ["单元测试用 pytest 跑", "手工回归用黑盒测试"],
     "question": "测试方案怎么分工的?", "expects": ["pytest", "黑盒"]},
    {"type": "long_context", "name": "项目与目录", "turns": ["项目代号叫 atlas", "代码放在 D:/atlas/src"],
     "question": "项目叫什么,代码在哪个目录?", "expects": ["atlas", "D:/atlas/src"]},
    {"type": "long_context", "name": "环境数据库", "turns": ["开发环境用 SQLite", "生产环境换 PostgreSQL"],
     "question": "开发和生产环境分别用什么数据库?", "expects": ["SQLite", "PostgreSQL"]},
    {"type": "long_context", "name": "依赖清单", "turns": ["这个项目依赖 numpy", "另外还依赖 pandas"],
     "question": "项目依赖哪些库?", "expects": ["numpy", "pandas"]},

    # ---- 2. statistic 统计问答(计数;清单式构造避免被冲突覆盖合并) ----
    {"type": "statistic", "name": "测试工具数", "turns": ["测试工具用过 pytest、unittest 和 pytest-bdd 这三个"],
     "question": "对话中提到过几个测试工具?", "expects": ["3"]},
    {"type": "statistic", "name": "端口数", "turns": ["服务端口 8000、数据库 5432、缓存 6379、管理 9000 这四个端口都要开"],
     "question": "对话中提到了几个端口号?", "expects": ["4"]},
    {"type": "statistic", "name": "城市数", "turns": ["团队在北京、上海、深圳三个城市都有分部"],
     "question": "提到过几个城市?", "expects": ["3"]},
    {"type": "statistic", "name": "框架数", "turns": ["前端试过 React 和 Svelte 两个框架"],
     "question": "对话中提到过几个前端框架?", "expects": ["2"]},
    {"type": "statistic", "name": "文件数", "turns": ["项目有 a.py、b.py、c.py、d.py、e.py 这五个 py 文件"],
     "question": "对话中提到了几个 py 文件?", "expects": ["5"]},
    {"type": "statistic", "name": "数据库数", "turns": ["用过 MySQL、PostgreSQL、MongoDB 三种数据库"],
     "question": "提到过几个数据库?", "expects": ["3"]},

    # ---- 3. post_hoc 事后问答(最终状态,覆盖链) ----
    {"type": "post_hoc", "name": "最终测试框架", "turns": ["测试先用 pytest", "决定改用 unittest"],
     "question": "最终确定的测试框架是什么?", "expects": ["unittest"]},
    {"type": "post_hoc", "name": "最终方案", "turns": ["计划用方案 A", "重新考虑后决定方案 B"],
     "question": "最后定的是哪个方案?", "expects": ["B"]},
    {"type": "post_hoc", "name": "最终预算", "turns": ["预算先定 5000 元", "批下来改成 8000 元"],
     "question": "最终预算是多少?", "expects": ["8000"]},
    {"type": "post_hoc", "name": "最终环境", "turns": ["先部署到 staging", "验证后上线 prod"],
     "question": "最终部署在哪个环境?", "expects": ["prod"]},
    {"type": "post_hoc", "name": "最终语言", "turns": ["文档先用英文写", "考虑受众后改用中文"],
     "question": "文档最终用什么语言?", "expects": ["中文"]},
    {"type": "post_hoc", "name": "最终版本", "turns": ["当前是 v1 版本", "马上发 v2"],
     "question": "现在的版本号是多少?", "expects": ["v2"]},

    # ---- 4. conversation 对话问答(具体细节,FTS 检索联动) ----
    {"type": "conversation", "name": "部署端口", "turns": ["部署端口定为 8080"],
     "question": "部署端口是多少?", "expects": ["8080"]},
    {"type": "conversation", "name": "数据目录", "turns": ["数据文件统一放 D:/data"],
     "question": "数据文件放在哪个目录?", "expects": ["D:/data"]},
    {"type": "conversation", "name": "数据库名", "turns": ["数据库名定为 mydb"],
     "question": "数据库名叫什么?", "expects": ["mydb"]},
    {"type": "conversation", "name": "密钥位置", "turns": ["密钥统一放在 .env 文件里"],
     "question": "密钥放在哪个文件?", "expects": [".env"]},
    {"type": "conversation", "name": "服务器 IP", "turns": ["服务器地址是 192.168.1.10"],
     "question": "服务器 IP 是多少?", "expects": ["192.168.1.10"]},
    {"type": "conversation", "name": "分支名", "turns": ["开发分支叫 feature/login"],
     "question": "开发分支叫什么?", "expects": ["feature/login"]},

    # ---- 5. personal 个人信息(长期偏好,记忆注入) ----
    {"type": "personal", "name": "交流语言", "turns": ["以后都用中文交流"],
     "question": "我偏好用什么语言交流?", "expects": ["中文"]},
    {"type": "personal", "name": "回答风格", "turns": ["回答要简洁直接"],
     "question": "我喜欢的回答风格是什么?", "expects": ["简洁"]},
    {"type": "personal", "name": "测试习惯", "turns": ["测试统一用 pytest"],
     "question": "我测试喜欢用什么?", "expects": ["pytest"]},
    {"type": "personal", "name": "编辑器", "turns": ["我编辑器用 Vim"],
     "question": "我喜欢用什么编辑器?", "expects": ["vim"]},
    {"type": "personal", "name": "工作习惯", "turns": ["我习惯先看文档再动手"],
     "question": "我的工作习惯是什么?", "expects": ["文档"]},
    {"type": "personal", "name": "主力系统", "turns": ["我主力系统是 Windows"],
     "question": "我主要用什么操作系统?", "expects": ["windows"]},
]


def norm(s: str) -> str:
    """归一化:小写 + 去所有空白(与质量门一致)。"""
    import re

    return re.sub(r"\s+", "", (s or "").lower())


class EmptyRegistry(ToolRegistry):
    """无工具注册表:对话纯文本,省工具定义的常驻 token。"""

    def list_tools(self):
        return []


def make_agent(store: SessionStore) -> DummyAgent:
    api_key = os.environ.get("DUMMY_API") or os.environ.get("DUMMY_AGENT_API_KEY", "")
    if not api_key:
        raise SystemExit("缺少 DUMMY_API 环境变量")
    llm = LLMClient(
        api_key=api_key,
        base_url=os.environ.get("DUMMY_AGENT_BASE_URL") or "https://api.deepseek.com",
        model=os.environ.get("DUMMY_AGENT_MODEL", "deepseek-chat"),
    )
    agent = DummyAgent(llm, EmptyRegistry(), system_prompt="你是一个助手,用中文简洁回答。")
    agent.session_store = store
    agent.memory.store = store  # 同步替换,防测试污染真实库
    agent.current_session_id = store.create_session()
    return agent


def run_case(case: dict, tmp_dir: str, index: int) -> tuple[bool, str]:
    """跑单题:会话 A 逐轮对话(自动抽取记忆)→ 会话 B 提问 → 判定。

    返回 (通过, 失败原因或回答片段)。
    """
    # 每题目独立 db(经验:共用 db 时前题记忆污染注入检索)
    store = SessionStore(os.path.join(tmp_dir, f"case_{index}.db"))
    agent = make_agent(store)
    try:
        for turn in case["turns"]:
            agent.chat(turn)  # 对话结束自动抽取记忆(2.4)
        # 会话 B:新 agent、新 session(记忆跨会话注入生效)
        agent_b = make_agent(store)
        answer = agent_b.chat(case["question"])
    except Exception as e:
        return False, f"[异常] {type(e).__name__}: {e}"
    finally:
        agent.llm.client.close()
        try:
            agent_b.llm.client.close()
        except Exception:
            pass
    a = norm(answer)
    missing = [e for e in case["expects"] if norm(e) not in a]
    if missing:
        return False, f"缺 {missing}, 回答: {str(answer)[:120]!r}"
    return True, f"回答: {str(answer)[:80]!r}"


def main():
    parser = argparse.ArgumentParser(description="Phase 2 综合回归集")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题(0=全部)")
    args = parser.parse_args()

    cases = CASES[: args.limit] if args.limit else CASES
    tmp = tempfile.mkdtemp(prefix="phase2-reg-")
    results: list[dict] = []
    try:
        for i, case in enumerate(cases, 1):
            t0 = time.time()
            ok, detail = run_case(case, tmp, i)
            dur = time.time() - t0
            results.append({"case": case, "ok": ok, "detail": detail})
            mark = "✅" if ok else "❌"
            print(f"  {mark} [{i}/{len(cases)}] {case['type']}: {case['name']} "
                  f"({dur:.0f}s) {'' if ok else detail}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 汇总:分类通过率 + 总通过率
    print("\n=== 分类汇总 ===")
    by_type: dict[str, list[bool]] = {}
    for r in results:
        by_type.setdefault(r["case"]["type"], []).append(r["ok"])
    for t, oks in by_type.items():
        print(f"  {t}: {sum(oks)}/{len(oks)}")
    total_ok = sum(1 for r in results if r["ok"])
    print(f"\n=== 总通过率: {total_ok}/{len(results)} "
          f"({100.0 * total_ok / len(results):.0f}%) ===")
    sys.exit(0 if total_ok == len(results) else 1)


if __name__ == "__main__":
    main()
