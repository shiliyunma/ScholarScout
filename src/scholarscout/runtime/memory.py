"""跨会话记忆：抽取、判重、注入、以及**能删能改**的出口。

## 它解决什么

会话内已有研究台账（目标/子问题/查询），但那是**任务**状态；关于**用户**的稳定事实
（专业方向、技术水平、偏好与禁忌、这学期的目标）没有落点，于是每开一个新会话，
助手都从零开始了解他。

## 三条约束（缺一条就会变污染源）

1. **只抽稳定事实**：一次性任务细节、能随时重读的百科信息、任何推测，一律不要
   （提示词里明确列了；抽取后在入库前还会按相似度判重）。
2. **必须有出口**：每条都能查看/修改/删除（`memory.remove` / `memory.update` / 界面列表）。
   自动记忆没有出口就是污染 —— 一次误抽会污染之后所有轮次，用户还找不到地方改。
3. **冲突以用户当前说法为准**：注入时明写这一句。记忆是"背景"，不是"命令"；
   用户这轮说"改成 Python 吧"，不能因为记忆里写着"他用 TypeScript"就顶回去。

## 判重为什么复用 `research_state.similarity`

两处各写一份相似度迟早给出两套数字。这里的阈值（0.75）比查询判重（0.6）**更严**：
查询重搜只是浪费一次检索，记忆重复会长期占着注入预算。
"""

from __future__ import annotations

import json
import re
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso
from scholarscout.state.sessions import new_id

#: 与已有记忆的相似度阈值（比查询判重更严：重复的记忆会长期占注入预算）
DEDUPE_THRESHOLD = 0.75
#: 注入的字符上限（超了就只发最新的几条，老的留在库里不删）
NOTE_BUDGET_CHARS = 600
#: 一次最多抽几条
MAX_EXTRACTED = 5
#: 允许的类别
KINDS = ("preference", "fact", "goal")


def list_facts(db: Database) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.query(
            "SELECT id, text, kind, source_session, created_at, updated_at FROM user_facts"
            " ORDER BY created_at DESC"
        )
    ]


def _find_similar(db: Database, text: str) -> dict[str, Any] | None:
    """本地判重：只兜**近似文本**（几乎同一句话），语义重复交给模型。

    为什么不追求"本地判出所有同义改写"：
      · 字符集重合度能抓乱序改写，但会把"用户偏好中文"和"用户**不喜欢**中文"判成同一条
        —— 这是相反的语义，误合并比漏合并危险得多；
      · 二字符组相似度对短中文句太严（实测"在写本科毕设，方向是AI+教育" vs
        "在写AI+教育方向的本科毕设"只有 0.5）。
    所以分工是：**本地只兜近似文本，语义判重由模型做** —— 抽取时把已有记忆一起给它
    （提示词里写着"与已有记忆重复就别再写"），模型擅长这个。
    """
    from scholarscout.runtime import research_state

    normalized = research_state.normalize_query(text)
    for row in list_facts(db):
        existing = str(row["text"])
        other = research_state.normalize_query(existing)
        # 包含关系：一句是另一句的扩写（"偏好中文" ⊂ "偏好中文回答"）→ 算重复。
        # 短的至少要 6 个字，免得"中文"这种词把两条不相干的记忆并起来。
        shorter, longer = sorted((normalized, other), key=len)
        if len(shorter) >= 6 and shorter in longer:
            return row
        if research_state.similarity(existing, text) >= DEDUPE_THRESHOLD:
            return row
    return None


def add_fact(
    db: Database,
    text: str,
    *,
    kind: str = "fact",
    source_session: str | None = None,
) -> dict[str, Any]:
    """加一条记忆。**同一件事不重复记**（相似度高的返回已有那条）。"""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        raise ValueError("记忆内容不能为空")
    if kind not in KINDS:
        raise ValueError(f"非法类别：{kind!r}（允许：{', '.join(KINDS)}）")
    existing = _find_similar(db, cleaned)
    if existing is not None:
        return {"created": False, "fact": existing, "reason": "已有相似记忆"}
    fact_id = new_id("mem")
    db.execute(
        "INSERT INTO user_facts (id, text, kind, source_session, created_at) VALUES (?,?,?,?,?)",
        (fact_id, cleaned[:400], kind, source_session, now_iso()),
    )
    return {
        "created": True,
        "fact": {
            "id": fact_id,
            "text": cleaned[:400],
            "kind": kind,
            "source_session": source_session,
            "created_at": now_iso(),
            "updated_at": None,
        },
    }


def update_fact(
    db: Database, fact_id: str, text: str, *, kind: str | None = None
) -> dict[str, Any]:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        raise ValueError("记忆内容不能为空")
    row = db.query_one("SELECT id FROM user_facts WHERE id = ?", (fact_id,))
    if row is None:
        raise ValueError(f"记忆不存在：{fact_id}")
    if kind is not None:
        if kind not in KINDS:
            raise ValueError(f"非法类别：{kind!r}")
        db.execute(
            "UPDATE user_facts SET text = ?, kind = ?, updated_at = ? WHERE id = ?",
            (cleaned[:400], kind, now_iso(), fact_id),
        )
    else:
        db.execute(
            "UPDATE user_facts SET text = ?, updated_at = ? WHERE id = ?",
            (cleaned[:400], now_iso(), fact_id),
        )
    return {"ok": True, "id": fact_id}


def remove_fact(db: Database, fact_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT id FROM user_facts WHERE id = ?", (fact_id,))
    if row is None:
        raise ValueError(f"记忆不存在：{fact_id}")
    db.execute("DELETE FROM user_facts WHERE id = ?", (fact_id,))
    return {"ok": True, "id": fact_id}


def render_facts_note(db: Database) -> str:
    """注入用的几行。超预算就只发最新的（老的留在库里，不删）。"""
    rows = list_facts(db)
    if not rows:
        return ""
    picked: list[str] = []
    used = 0
    for row in rows:
        line = f"- [{row['kind']}] {row['text']}"
        if used + len(line) > NOTE_BUDGET_CHARS and picked:
            break
        picked.append(line)
        used += len(line)
    lines = [
        "【关于这个用户的长期记忆】（可用 memory.remove 更正；与用户当前说法冲突时**以当前说法为准**）"
    ]
    lines.extend(picked)
    return "\n".join(lines)


def parse_facts(content: str) -> list[dict[str, str]]:
    """从模型回复里取事实表；取不到回空表（**不猜**）。"""
    text = str(content or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return []
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fact_text = " ".join(str(item.get("text") or "").split())
        kind = str(item.get("kind") or "fact")
        if not fact_text:
            continue
        out.append({"text": fact_text[:400], "kind": kind if kind in KINDS else "fact"})
    return out[:MAX_EXTRACTED]


def build_transcript(db: Database, session_id: str, *, max_chars: int = 4000) -> str:
    """给抽取用的素材：**用户说过的话** + 这次会话产出了什么。

    只取用户消息（不取助手的长篇回答）—— 关于用户的事实几乎都在用户自己说的话里，
    而助手的回答又长又贵，塞进去只会稀释信号。
    """
    rows = db.query(
        "SELECT role, content FROM messages WHERE session_id = ? AND role = 'user'"
        " ORDER BY created_at",
        (session_id,),
    )
    parts = [str(row["content"])[:600] for row in rows if str(row["content"]).strip()]
    artifacts = db.query(
        "SELECT topic FROM learning_routes WHERE session_id = ?"
        " UNION SELECT topic FROM topic_maps WHERE session_id = ?",
        (session_id, session_id),
    )
    if artifacts:
        topics = "、".join(str(row["topic"]) for row in artifacts)
        parts.append(f"（这次会话产出的主题：{topics}）")
    text = "\n".join(parts)
    return text[-max_chars:] if len(text) > max_chars else text


def extract_facts(
    llm: Any,
    db: Database,
    session_id: str,
    *,
    prompts_dir: Any,
) -> dict[str, Any]:
    """抽一次记忆（整个会话只做一次）。失败回空表 —— 绝不阻塞这一轮。"""
    from scholarscout.summary.prompts import PromptPack

    transcript = build_transcript(db, session_id)
    if not transcript.strip():
        return {"added": [], "skipped": [], "reason": "这次会话没有可用的用户消息"}
    existing = "\n".join(f"- {row['text']}" for row in list_facts(db)) or "（还没有）"
    try:
        prompt = PromptPack(prompts_dir).compose(
            "memory-extract", existing=existing[:1500], transcript=transcript
        )
        reply = llm.chat([{"role": "user", "content": prompt}])
    except Exception as exc:
        return {"added": [], "skipped": [], "reason": f"{type(exc).__name__}: {exc}", "usage": {}}
    usage = {
        key: int(value)
        for key, value in (getattr(reply, "usage", {}) or {}).items()
        if isinstance(value, int)
    }
    parsed = parse_facts(str(getattr(reply, "content", "")))
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in parsed:
        result = add_fact(db, item["text"], kind=item["kind"], source_session=session_id)
        (added if result["created"] else skipped).append(result["fact"])
    return {"added": added, "skipped": skipped, "reason": None, "usage": usage}


def should_extract(db: Database, session_id: str) -> bool:
    """该不该为这个会话抽一次？—— 每会话最多一次，且要有"值得记"的迹象。

    迹象：用户说过至少两轮（一句话的会话没什么可记的），或这次会话产出了产物。
    """
    already = db.query_one(
        "SELECT count(*) AS n FROM user_facts WHERE source_session = ?", (session_id,)
    )
    if already is not None and int(already["n"]) > 0:
        return False
    turns = db.query_one(
        "SELECT count(*) AS n FROM messages WHERE session_id = ? AND role = 'user'", (session_id,)
    )
    produced = db.query_one(
        "SELECT (SELECT count(*) FROM learning_routes WHERE session_id = ?)"
        " + (SELECT count(*) FROM topic_maps WHERE session_id = ?)"
        " + (SELECT count(*) FROM notes WHERE session_id = ?) AS n",
        (session_id, session_id, session_id),
    )
    user_turns = int(turns["n"]) if turns is not None else 0
    produced_n = int(produced["n"]) if produced is not None else 0
    return user_turns >= 2 or produced_n > 0
