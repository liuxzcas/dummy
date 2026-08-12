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

EXTRACT_PROMPT = """你是记忆抽取器。从用户提供的对话中提取值得长期记住的事实
(用户偏好、项目决策、关键配置、重要事实),输出 JSON 数组,不要输出任何其他内容。

已有记忆列表(用于冲突判断):
{existing}

输出格式,每条一个对象:
{{"fact": "事实文本", "category": "偏好|项目|技术|其他", "confidence": 0.0~1.0, "replace_id": 数字或 null}}

规则:
- 如果新事实与某条已有记忆语义重复,replace_id 填那条记忆的 id(新事实通常更新);
- 否则 replace_id 为 null。
- 只抽取值得长期记住的事实,不抽取临时信息(问候、单次任务细节)。
- confidence 表示你对该事实可信度的把握。"""


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
