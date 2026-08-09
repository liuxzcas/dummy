"""Phase 2.2 Step 8 真实冒烟:DeepSeek 长对话自然逼出压缩触发。

用法:python scripts/smoke_compress.py(需环境变量 DUMMY_API)
配置:window_tokens=8000(阈值 5600)——全部真实代码路径,
小窗口仅为让冒烟在 30 轮内可及(默认 64K 阈值需 45K+ tokens)。

记录:
- 压缩触发点(第几轮)、strategy、chars_before/after(压缩率)、
  duration_ms(摘要延迟,含真实 LLM 摘要调用)
- 事件日志新增行(compression.jsonl)
- 压缩后事实召回:早期埋的事实(已被 L2 摘要)再提问能否答对

收尾自动清理:临时 session.db、事件日志恢复原状、删除本次对话快照。
"""
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from llm import LLMClient  # noqa: E402
from tools import create_default_registry  # noqa: E402
from core import DummyAgent  # noqa: E402
from compressor import CompressionConfig, ContextCompressor  # noqa: E402
from session_store import SessionStore  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENT_LOG = os.path.join(REPO, "logs", "compression.jsonl")
LOG_DIR = os.path.join(REPO, "logs")

# 对话脚本:前 5 轮埋事实(会被 L2 摘要),中间用大输出增长上下文,最后提问
CONVO = [
    "上线日期定在 8 月 20 日,记得提前安排测试。",
    "项目预算 3000 元,注意控制成本。",
    "以后技术讨论都用中文。",
    "决定压缩策略用两级:L1 折叠大工具输出,L2 摘要早期对话。",
    "错误日志统一放在 D:/Engineering/dummy/logs/errors.log。",
    "读取 docs/context-compression.md 的前 200 行,说说它讲了什么。",
    "读取 session_store.py 的内容,简单说明它的作用。",
    "运行 git log --oneline -20 看看最近的提交。",
    "读取 requirements.txt 的内容。",
    "运行 ls -la 看看项目根目录结构。",
    "用 3 句话总结一下刚才读的 context-compression.md 的核心思想。",
    "再运行一次 git log --oneline -15。",
    "读取 compressor.py 的前 150 行。",
    "运行 ls docs/ 看看有哪些文档。",
    "补充一下:测试环境用 staging,生产用 prod。",
    "读取 docs/phase2.2_plan.md 的前 120 行,简要说说第 6 节讲了什么。",
    "运行 git status --short 看看工作区状态。",
    "读取 prompt.py 文件。",
    "用两句话描述 agent 的 chat 流程。",
    "运行 grep -n 'def ' core.py | head -20 看看核心方法列表。",
    "再补充:日志轮转策略,超过 5MB 就归档。",
    "读取 docs/roadmap.md 的前 100 行。",
    "运行 ls scripts/ 看看有哪些脚本。",
    "读取 main.py 的前 100 行,说明它怎么装配 agent。",
    "运行 du -sh docs/ 看看文档目录大小。",
]

# 压缩后召回(答案必须在压缩后的上下文中可找到)
# 期望用列表:多段事实必须全部出现在答案里(如"哪两级"→ L1 和 L2 都要)
RECALL = [
    ("上线日期是哪天?", ["8 月 20 日"]),
    ("项目预算是多少?", ["3000"]),
    ("技术讨论用什么语言?", ["中文"]),
    ("压缩策略用哪两级?", ["L1", "L2"]),
    ("错误日志放在哪里?", ["errors.log"]),
]


def read_event_lines() -> list[dict]:
    if not os.path.exists(EVENT_LOG):
        return []
    events = []
    for line in open(EVENT_LOG, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 容忍坏行(防御),不中断报告阶段
    return events


def git_status() -> set[str]:
    """仓库工作区快照(modified/untracked 文件集),用于检测 agent 运行期的副作用。"""
    import subprocess
    out = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=REPO
    ).stdout
    return set(l.strip() for l in out.splitlines() if l.strip())


def main() -> int:
    api_key = os.environ.get("DUMMY_API") or os.environ.get("DUMMY_AGENT_API_KEY", "")
    if not api_key:
        print("缺少 API key:请设置环境变量 DUMMY_API")
        return 2
    base_url = os.environ.get("DUMMY_AGENT_BASE_URL", "").strip()
    llm = LLMClient(api_key=api_key, base_url=base_url or None, model="deepseek-chat") if base_url \
        else LLMClient(api_key=api_key, model="deepseek-chat")

    # 临时 db 放项目本地(不用 %TEMP%):避开 Windows 临时目录清理器/扫描器
    # 对运行中文件的瞬态干扰(实测出现过 unable to open database file)
    tmp = os.path.join(REPO, ".smoke-tmp-" + uuid.uuid4().hex[:8])
    os.makedirs(tmp, exist_ok=True)
    db_path = os.path.join(tmp, "smoke.db")

    events_before = len(read_event_lines())
    conv_before = set(f for f in os.listdir(LOG_DIR) if f.startswith("conversation_")) if os.path.isdir(LOG_DIR) else set()
    status_before = git_status()

    def cleanup() -> None:
        # 清理:临时 db、事件日志、本次对话快照
        agent.session_store = None
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.isdir(tmp):
            print(f"  ⚠️ 临时目录清理失败(可能被占用): {tmp}")
        # agent 可能通过 write_file 等工具改仓库文件(实测改过 .env.example):
        # 检测并告警,由操作者决定是否还原
        new_dirty = git_status() - status_before
        if new_dirty:
            print(f"  ⚠️ 冒烟期间仓库出现新的文件改动(agent 工具副作用),请检查: {sorted(new_dirty)}")
        if os.path.exists(EVENT_LOG):
            lines = open(EVENT_LOG, encoding="utf-8").read().splitlines()
            with open(EVENT_LOG, "w", encoding="utf-8") as f:
                f.write("\n".join(lines[:events_before]) + ("\n" if lines[:events_before] else ""))
        for f in os.listdir(LOG_DIR):
            if f.startswith("conversation_") and f not in conv_before:
                os.remove(os.path.join(LOG_DIR, f))

    def make_agent() -> DummyAgent:
        agent = DummyAgent(llm_client=llm, tool_registry=create_default_registry())
        agent.session_store = SessionStore(db_path)
        agent.compressor = ContextCompressor(llm, CompressionConfig(window_tokens=8000))
        return agent

    agent = make_agent()

    def chat_with_retry(prompt: str, retries: int = 2) -> str:
        """瞬态 db 错误(锁/扫描器)重试;真实 LLM/工具错误不重试。"""
        for attempt in range(retries + 1):
            try:
                return agent.chat(prompt)
            except Exception as e:
                if "unable to open database file" not in str(e) or attempt == retries:
                    raise
                print(f"    (db 瞬态错误,重试 {attempt + 1}/{retries})")
                time.sleep(2)
        raise RuntimeError("unreachable")

    try:
        print(f"=== 冒烟开始: {len(CONVO)} 轮 + {len(RECALL)} 个召回问题,窗口 8000(阈值 5600) ===")
        t_start = time.time()
        errors = []
        for i, prompt in enumerate(CONVO):
            t0 = time.time()
            try:
                reply = chat_with_retry(prompt)
                print(f"[{i + 1:02d}/{len(CONVO)}] ok {time.time() - t0:.1f}s | 历史 {len(agent.history)} 条 | tokens={llm.last_usage.prompt_tokens if llm.last_usage else '?'}")
            except Exception as e:
                errors.append((i, type(e).__name__, str(e)))
                if "database" in str(e).lower():
                    print(f"[诊断] db={db_path} 目录存在={os.path.isdir(tmp)} 文件存在={os.path.isfile(db_path)}")
                print(f"[{i + 1:02d}/{len(CONVO)}] ❌ {type(e).__name__}: {str(e)[:100]}")
        duration_total = time.time() - t_start

        events = read_event_lines()[events_before:]
        print(f"\n=== 压缩事件: {len(events)} 次 ===")
        for ev in events:
            ratio = ev["chars_after"] / ev["chars_before"] if ev.get("chars_before") else 0
            print(f"  {ev['ts']} | strategy={ev['strategy']:<5} folded={ev['folded']} covers={ev['covers']} "
                  f"chars {ev['chars_before']}→{ev['chars_after']} ({ratio:.0%}) | {ev['duration_ms']}ms | success={ev['success']} err={ev['error_type']}")

        print(f"\n=== 压缩后事实召回 ===")
        recall_ok = 0
        for q, expects in RECALL:
            try:
                ans = chat_with_retry(q)
                ok = all(e.lower() in ans.lower() for e in expects)  # 全部期望子串必须出现
                recall_ok += ok
                missing = [e for e in expects if e.lower() not in ans.lower()]
                print(f"  {'✅' if ok else '❌'} Q: {q}  →  {ans[:60].strip()}" + (f"  [缺: {missing}]" if missing else ""))
            except Exception as e:
                print(f"  ❌ Q: {q} → 异常 {type(e).__name__}")
        recall_total = len(RECALL)

        print(f"\n=== 汇总 ===")
        print(f"轮数: {len(CONVO)} | 总耗时: {duration_total:.0f}s | 调用错误: {len(errors)}")
        if events:
            ratios = [ev["chars_after"] / ev["chars_before"] for ev in events if ev.get("chars_before")]
            lat = [ev["duration_ms"] for ev in events]
            print(f"压缩率(压缩后/前): {min(ratios):.0%} ~ {max(ratios):.0%} | 摘要延迟: {sum(lat) / len(lat):.0f}ms 平均")
        print(f"召回: {recall_ok}/{recall_total} | 压缩触发: {'✅ ' + str(len(events)) + ' 次' if events else '❌ 未触发'}")

        ok = bool(events) and recall_ok >= recall_total * 0.9 and not errors
        print(f"\n冒烟结论: {'PASS' if ok else 'FAIL'}(触发={len(events)}, 召回={recall_ok}/{recall_total}, 错误={len(errors)})")
        return 0 if ok else 1
    finally:
        agent.session_store = None
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
