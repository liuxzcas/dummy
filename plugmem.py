"""PlugMem 版记忆(分支 plugmem-v2)。

按论文 PlugMem(arXiv:2603.03296)三组件实现,简化版(无 embedding
依赖,概念匹配用字符串匹配;multi-hop 检索收敛为一跳"概念→单元"):

- structuring(3.1): 对话 → 知识单元(semantic 命题 / procedural 规范)
  + 概念标签(高层路由信号)+ update_unit_id(同概念同主题更新,图演化)
- retrieval(3.2): query → LLM 生成概念集(抽象查询)→ 概念匹配激活单元
  (高层概念仅作路由信号,只有单元是候选——与论文一致)
- reasoning(3.3): query + 单元 → LLM 蒸馏成"最终信息"(Final Information)
  注入上下文——信息密度核心,不注入单元原文

对比旧 2.4(平铺事实 + 关键词检索 + 原文注入),对症 T5 三个失败模式:
- 同义词鸿沟 → 概念层(query 概念与单元概念语义对齐)
- 注入噪声 → 推理蒸馏(只给任务导向指导)
- post_hoc 覆盖链 → update_unit_id(新证据覆盖旧,不并存)
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

STRUCTURE_PROMPT = """你是记忆结构化器(PlugMem structuring)。从对话中提取知识单元。

两类知识单元:
1. semantic(命题事实):可验证的事实陈述,如"用户偏好中文交流"、"项目预算是 3000 元"
2. procedural(规范知识):可复用的做事流程,如"发布流程:先跑测试再打 tag"

每条单元必须附带 concepts(高层概念标签,3-8 个,作检索路由信号;
要抽象概括,如"测试工具"而不是"pytest 这个工具")。

已有单元列表(用于更新演化):
{existing}

输出 JSON 数组,不要输出其他内容:
[{{"type": "semantic|procedural", "text": "...", "concepts": ["概念1", "概念2"], "update_unit_id": 数字或 null}}]

规则:
- 新知识若与某已有单元语义重复(同主题同概念,如"预算 8000"是"预算 5000"的更新),
  update_unit_id 填那条单元 id,用新内容更新它——不要并存
- 只输出值得长期记住的知识,忽略临时信息(问候、单次任务细节)"""

CONCEPT_PROMPT = """你是记忆检索器(PlugMem retrieval)。根据用户提问生成 2-5 个
高层概念作为检索路由信号(抽象名词短语,1-8 字)。只输出 JSON 数组,
不要其他内容。示例:["技术栈", "部署配置", "测试工具"]。
用户提问:{query}"""

DISTILL_PROMPT = """你是记忆推理器(PlugMem reasoning)。根据用户提问,把检索到的
知识单元蒸馏成 1-3 条"最终信息"(Final Information):任务导向、可直接使用的
简短事实或指导。只输出蒸馏结果,不要解释,不要逐字复述单元原文。

用户提问: {query}

检索到的知识单元:
{units}"""


def parse_json_array(text: str) -> list:
    """解析 JSON 数组(容错:标准 JSON → 围栏 → 双引号正则兜底)。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        items = re.findall(r'"([^"]{1,200})"', t)
        return items


class PlugMemMemory:
    """PlugMem 三组件:结构化 / 检索 / 推理。"""

    def __init__(self, llm, store, log_path: str | None = None):
        self.llm = llm
        self.store = store
        if log_path is None:
            base = os.path.dirname(os.path.abspath(__file__))
            log_path = os.path.join(base, "logs", "memory.jsonl")
        self.log_path = log_path

    # ---- 3.1 Structuring ----
    def structure_from_history(
        self, history: list[dict], session_id: str
    ) -> tuple[int, int]:
        """对话 → 知识单元(命题/规范 + 概念)+ 更新演化。

        返回 (新增数, 更新数)。任何失败记事件日志并降级返回 (0,0),
        绝不影响对话主流程。
        """
        existing = self.store.list_units()
        existing_text = "\n".join(
            f"[{u['id']}] ({u['type']}) {u['text']}  concepts={u['concepts']}"
            for u in existing
        ) or "(无)"
        conv = "\n".join(
            f"{m['role']}: {str(m.get('content'))[:500]}"
            for m in history
            if m.get("content") and m["role"] in ("user", "assistant")
        )
        try:
            resp = self.llm.chat(
                messages=[
                    {"role": "system",
                     "content": STRUCTURE_PROMPT.format(existing=existing_text)},
                    {"role": "user", "content": conv},
                ],
                temperature=0.1,
            )
            units = parse_json_array(getattr(resp, "content", ""))
        except Exception as e:
            self._log_event(
                {"event": "extract_failed", "session": session_id,
                 "error_type": type(e).__name__}
            )
            return 0, 0
        if not units or not isinstance(units, list):
            self._log_event(
                {"event": "extract_failed", "session": session_id,
                 "error_type": "empty_or_invalid"}
            )
            return 0, 0

        existing_ids = {u["id"] for u in existing}
        added = updated = 0
        for u in units:
            if not isinstance(u, dict) or not u.get("text"):
                continue
            utype = u.get("type") if u.get("type") in ("semantic", "procedural") else "semantic"
            concepts = [str(c).strip()[:30] for c in (u.get("concepts") or []) if str(c).strip()]
            rid = u.get("update_unit_id") if u.get("update_unit_id") in existing_ids else None
            self.store.add_unit(
                session_id, utype, str(u["text"]).strip()[:500],
                concepts, update_unit_id=rid,
            )
            if rid is not None:
                updated += 1
            else:
                added += 1
        self._log_event(
            {"event": "extract", "session": session_id,
             "units": added + updated, "updated": updated}
        )
        return added + updated, updated

    # ---- 3.2 Retrieval(概念路由) ----
    def retrieve_concepts(self, query: str) -> list[str]:
        """query → 高层概念集(抽象查询,路由信号)。失败回退 [query]。"""
        try:
            resp = self.llm.chat(
                messages=[
                    {"role": "system", "content": CONCEPT_PROMPT.format(query=query)},
                    {"role": "user", "content": query},
                ],
                temperature=0.0,
            )
            concepts = [str(c).strip() for c in parse_json_array(getattr(resp, "content", ""))]
        except Exception:
            return [query]
        return concepts or [query]

    def retrieve_units(self, concepts: list[str], limit: int = 4) -> list[dict]:
        """概念集 → 激活单元(概念是信号,单元是候选;多概念并集去重)。"""
        return self.store.search_units_by_concepts(concepts, limit=limit)

    # ---- 3.3 Reasoning(蒸馏注入) ----
    def distill(self, query: str, units: list[dict]) -> str:
        """单元 → 最终信息(Final Information)。失败回退原文拼接(保底)。"""
        units_text = "\n".join(
            f"- ({u['type']}) {u['text']}" for u in units
        )
        try:
            resp = self.llm.chat(
                messages=[
                    {"role": "system",
                     "content": DISTILL_PROMPT.format(query=query, units=units_text)},
                    {"role": "user", "content": query},
                ],
                temperature=0.2,
            )
            out = (getattr(resp, "content", "") or "").strip()
            return out if out else units_text
        except Exception:
            return units_text

    def _log_event(self, event: dict) -> None:
        event["time"] = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
