"""跨 Session 记忆抽取(Phase 2.4)。

对话结束 → LLM 抽取事实(fact/category/confidence/replace_id)
→ 冲突覆盖或新增 → memories 表;事件日志 logs/memory.jsonl。

设计要点(见 docs/cross-session-memory.md):
- 抽取是旁路调用:不污染对话历史,失败绝不影响主流程
- 冲突裁决交给 LLM(输出 replace_id 指向语义重复的旧记忆),代码只执行
- 事件日志可审计:extract / extract_failed 全部落盘
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

EXPAND_PROMPT = """你是查询扩展器。把用户的提问转成 2-4 个用于检索记忆库的检索词
(名词性概念,1-8 字)。要求:提取主题名词,去掉问句语气词
(什么/是/分别/用/吗/呢/怎么样/请),不要疑问句,不要解释。
只输出 JSON 数组,例如:["前端框架", "后端框架", "技术选型"]。
用户提问:{question}"""


def parse_terms(text: str) -> list[str]:
    """解析检索词 JSON 数组(容错:标准 JSON → 围栏 → 双引号正则兜底)。

    非法输入返回空列表,调用方回退到原始问句。
    """
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        data = json.loads(t)
        if isinstance(data, list):
            return [str(x).strip()[:20] for x in data if str(x).strip()]
    except json.JSONDecodeError:
        items = re.findall(r'"([^"]{1,20})"', t)
        return items
    return []

EXTRACT_PROMPT = """你是记忆蒸馏器(Hermes 方式)。把对话中值得长期记住的信息
蒸馏成精炼的事实条目,输出 JSON 数组,不要输出任何其他内容。

已有记忆列表(用于合并与容量管理):
{existing}

输出格式,每条一个对象:
{{"fact": "精炼事实", "category": "偏好|项目|技术|其他", "confidence": 0.0~1.0, "replace_id": 数字或 null}}

规则(写入时蒸馏,重要):
- fact 要精炼、信息密集;同类信息合并成一条
  (如"预算先定 5000 后来改成 8000" → "最终预算是 8000 元")
- **精确值必须原样保留**:涉及数量、专名、名称、端口、路径、清单时,
  具体值不得省略、不得概括成"若干/多个"——如"团队在北京、上海、深圳
  三个城市都有分部"要保留完整清单与数量,不能写成"团队分部在多个城市"
- 新信息与已有条目同主题时,replace_id 填那条 id,并用合并后的精炼
  表述替换旧条目——不并存
- 只输出值得长期记住的事实,忽略临时信息(问候、单次任务细节)
- confidence 表示你对该事实可信度的把握"""


def parse_facts_response(text: str) -> list[dict[str, Any]]:
    """解析 LLM 返回的 JSON 事实列表。

    容错三级:标准 JSON → 去掉 ```json 围栏 → 正则逐对象兜底。
    非法项直接丢弃(抽取失败面最小化)。
    """
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    data: Any = None
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        objs = re.findall(r"\{[^{}]*\"fact\"[^{}]*\}", t)
        data = [json.loads(o) for o in objs] if objs else []
    if not isinstance(data, list):
        return []
    facts: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("fact"):
            continue
        facts.append(
            {
                "fact": str(item["fact"]).strip()[:500],
                "category": str(item.get("category") or "其他")[:20],
                "confidence": float(item.get("confidence") or 0.8),
                "replace_id": item.get("replace_id"),
            }
        )
    return facts


class MemoryExtractor:
    """LLM 事实抽取器:对话 → 事实列表 → memories 表(冲突覆盖)。"""

    def __init__(self, llm, store, log_path: str | None = None):
        self.llm = llm
        self.store = store
        if log_path is None:
            base = os.path.dirname(os.path.abspath(__file__))
            log_path = os.path.join(base, "logs", "memory.jsonl")
        self.log_path = log_path

    def expand_query(self, question: str) -> list[str]:
        """LLM 把问句扩展成检索词(PlugMem 意图路由,方案 A 2026-08-11)。

        T5 回归实测:完整问句做 FTS 短语查询与记忆事实无共享关键词,
        检索不命中(8/30)。改为先提炼名词性概念("技术栈?" →
        ["前端框架","后端框架","技术选型"]),命中率大增。
        任何失败(调用异常/解析失败/空)回退 [question],不中断。
        """
        try:
            resp = self.llm.chat(
                messages=[
                    {"role": "system",
                     "content": EXPAND_PROMPT.format(question=question)},
                    {"role": "user", "content": question},
                ],
                temperature=0.0,
            )
            terms = parse_terms(getattr(resp, "content", ""))
        except Exception:
            return [question]
        return terms or [question]

    def extract_from_history(
        self, history: list[dict], session_id: str
    ) -> tuple[int, int]:
        """从对话历史抽取并落库。返回 (新增+覆盖数, 覆盖数)。

        任何失败(调用异常/解析失败/空结果)都记事件日志并安全降级,
        返回 (0, 0),绝不影响对话主流程。
        """
        existing = self.store.list_memories()
        existing_text = "\n".join(
            f"{m['id']}. [{m['category']}] {m['fact']}" for m in existing
        ) or "(无)"
        conv = "\n".join(
            f"{m['role']}: {str(m.get('content'))[:500]}"
            for m in history
            if m.get("content") and m["role"] in ("user", "assistant")
        )
        try:
            resp = self.llm.chat(
                messages=[
                    {"role": "system", "content": EXTRACT_PROMPT.format(existing=existing_text)},
                    {"role": "user", "content": conv},
                ],
                temperature=0.1,
            )
            facts = parse_facts_response(getattr(resp, "content", ""))
        except Exception as e:
            self._log_event(
                {"event": "extract_failed", "session": session_id,
                 "error_type": type(e).__name__}
            )
            return 0, 0
        if not facts:
            self._log_event(
                {"event": "extract_failed", "session": session_id,
                 "error_type": "empty_or_invalid"}
            )
            return 0, 0

        existing_ids = {m["id"] for m in existing}
        conflicts = 0
        for f in facts:
            rid = f["replace_id"] if f["replace_id"] in existing_ids else None
            if rid is not None:
                conflicts += 1
            self.store.add_memory(
                session_id, f["fact"], f["category"], f["confidence"], rid
            )
        self._log_event(
            {"event": "extract", "session": session_id,
             "facts": len(facts), "conflicts": conflicts}
        )
        return len(facts), conflicts

    def _log_event(self, event: dict) -> None:
        """追加事件日志;日志失败静默(不影响主流程)。"""
        event["time"] = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
