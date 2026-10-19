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

from scholarscout.state import facts as state_facts
from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso
from scholarscout.state.sessions import new_id, session_meta, set_session_meta

#: 与已有记忆的相似度阈值（比查询判重更严：重复的记忆会长期占注入预算）
DEDUPE_THRESHOLD = 0.75
#: 注入的字符上限（**两本账合占**，超了就只发最新的几条，老的留在库里不删）
NOTE_BUDGET_CHARS = 600
#: 一次最多抽几条
MAX_EXTRACTED = 5
#: 允许的类别
KINDS = ("preference", "fact", "goal")
#: 记忆的两本账（照 Hermes 的 MEMORY.md / USER.md；迁移 0016 起有 target 列）：
#:   user   —— 关于**用户**（谁在用）：身份/背景、长期偏好、持续目标、硬约束
#:   memory —— agent **自己的工作笔记**：环境/约定/工具坑/教训
#: 取值靠**代码层校验**（SQLite 的 ADD COLUMN 不支持 CHECK）。
TARGETS = ("user", "memory")
#: nudge 用的基准键：上一次抽取发生在"用户第几轮"（存 `sessions.meta_json`，见 mark_extracted）
_EXTRACT_TURN_KEY = "memory_extract_turn"

#: 两段注入的小标题（与用户当前说法冲突时以当前说法为准 —— 记忆是背景不是命令）
SECTION_USER = (
    "【关于这个用户的长期记忆】（可用 memory.remove 更正；与用户当前说法冲突时**以当前说法为准**）"
)
SECTION_MEMORY = "【我自己的工作笔记】（环境/约定/工具坑；可用 memory.remove 更正）"
#: 注入超预算时追加的一行合并引导（≤40 字，指向设置面板 / memory.remove）
MERGE_HINT = "（记忆较多，只发最新的；在设置里整理或用 memory.remove 删旧项）"


def list_facts(db: Database) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.query(
            "SELECT id, text, kind, target, source_session, created_at, updated_at FROM user_facts"
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
    target: str = "user",
    source_session: str | None = None,
) -> dict[str, Any]:
    """加一条记忆。**同一件事不重复记**（相似度高的返回已有那条）。

    `target` 决定落哪本账：`user`（关于用户）/ `memory`（agent 自己的工作笔记）。
    """
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        raise ValueError("记忆内容不能为空")
    if kind not in KINDS:
        raise ValueError(f"非法类别：{kind!r}（允许：{', '.join(KINDS)}）")
    if target not in TARGETS:
        raise ValueError(f"非法账本：{target!r}（允许：{', '.join(TARGETS)}）")
    existing = _find_similar(db, cleaned)
    if existing is not None:
        return {"created": False, "fact": existing, "reason": "已有相似记忆"}
    fact_id = new_id("mem")
    state_facts.insert_fact(
        db,
        fact_id=fact_id,
        text=cleaned[:400],
        kind=kind,
        target=target,
        source_session=source_session,
        created_at=now_iso(),
    )
    return {
        "created": True,
        "fact": {
            "id": fact_id,
            "text": cleaned[:400],
            "kind": kind,
            "target": target,
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
        state_facts.update_fact(db, fact_id, text=cleaned[:400], kind=kind, updated_at=now_iso())
    else:
        state_facts.update_fact(db, fact_id, text=cleaned[:400], kind=None, updated_at=now_iso())
    return {"ok": True, "id": fact_id}


def remove_fact(db: Database, fact_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT id FROM user_facts WHERE id = ?", (fact_id,))
    if row is None:
        raise ValueError(f"记忆不存在：{fact_id}")
    state_facts.delete_fact(db, fact_id)
    return {"ok": True, "id": fact_id}


def _note_line(row: dict[str, Any]) -> str:
    return f"- [{row['kind']}] {row['text']}"


def _pick_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """按 NOTE_BUDGET_CHARS 从新到旧挑要注入的行；返回（挑中的行, 是否截断）。

    **纯函数**（不读钟、不写库），所以渲染与"入库会不会顶破预算"共用同一把尺子。
    """
    picked: list[dict[str, Any]] = []
    used = 0
    for row in rows:
        line_len = len(_note_line(row))
        if used + line_len > NOTE_BUDGET_CHARS and picked:
            return picked, True
        picked.append(row)
        used += line_len
    return picked, False


def render_facts_note(db: Database) -> str:
    """注入用的几行。**两本账分两段**（user / memory），两段**合占 600 字预算**。

    超预算就只发最新的几条（老的留在库里，不删），并追一行合并引导 ——
    不静默丢弃、也不让用户/模型不知道该怎么收拾。
    """
    rows = list_facts(db)
    if not rows:
        return ""
    picked, truncated = _pick_rows(rows)
    user_lines = [_note_line(row) for row in picked if row.get("target") != "memory"]
    memory_lines = [_note_line(row) for row in picked if row.get("target") == "memory"]
    lines: list[str] = []
    if user_lines:
        lines.append(SECTION_USER)
        lines.extend(user_lines)
    if memory_lines:
        if lines:
            lines.append("")
        lines.append(SECTION_MEMORY)
        lines.extend(memory_lines)
    if truncated:
        lines.append(MERGE_HINT)
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
        target = str(item.get("target") or "user")
        if not fact_text:
            continue
        out.append(
            {
                "text": fact_text[:400],
                "kind": kind if kind in KINDS else "fact",
                # 账本写错 → 落 user（与 kind 同样"不丢这条"）
                "target": target if target in TARGETS else "user",
            }
        )
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
    overflow = False
    # 当前注入会占多少字（两本账合占 600 字预算）—— 与 render_facts_note 共用 _pick_rows
    picked, _ = _pick_rows(list_facts(db))
    used = sum(len(_note_line(row)) for row in picked)
    for item in parsed:
        line_len = len(_note_line({"kind": item["kind"], "text": item["text"]}))
        if used + line_len > NOTE_BUDGET_CHARS and (picked or added):
            # 注入口袋满了：**不静默丢旧、也不硬塞** —— 停手并把 current_entries 回给上游，
            # 让它提示模型/用户"合并或删除后再记"（照 Hermes 满载拒绝 add 的做法）。
            overflow = True
            break
        result = add_fact(
            db,
            item["text"],
            kind=item["kind"],
            target=item["target"],
            source_session=session_id,
        )
        if result["created"]:
            used += line_len
        (added if result["created"] else skipped).append(result["fact"])
    outcome: dict[str, Any] = {
        "added": added,
        "skipped": skipped,
        "reason": None,
        "usage": usage,
    }
    if overflow:
        outcome["reason"] = (
            f"记忆注入预算已满（{NOTE_BUDGET_CHARS} 字），未再写入新条目；"
            "请合并或删除已有记忆后重发（current_entries 里是现有全部）"
        )
        outcome["current_entries"] = list_facts(db)
    return outcome


def should_extract(db: Database, session_id: str, *, nudge_turns: int = 0) -> bool:
    """该不该为这个会话抽一次记忆？

    - `nudge_turns <= 0`（**默认**）：**每会话最多一次**，且要有"值得记"的迹象
      （用户说过≥2 轮，或这次会话产出了产物）。行为与引入 nudge 之前**逐字一致**。
    - `nudge_turns > 0`：**按轮续写式**（照 Hermes 的 `nudge_interval`，docs/16 §2 P1-1）——
      距上次抽取的用户轮数 ≥ `nudge_turns` 就再抽一次，让长会话中途出现的稳定事实
      （如"我改用 PyTorch 了"）不必等到下次会话才被记。由 `mark_extracted` 记录基准轮。
    """
    turns = db.query_one(
        "SELECT count(*) AS n FROM messages WHERE session_id = ? AND role = 'user'", (session_id,)
    )
    user_turns = int(turns["n"]) if turns is not None else 0

    if nudge_turns > 0:
        last = int(session_meta(db, session_id).get(_EXTRACT_TURN_KEY, 0) or 0)
        return user_turns - last >= nudge_turns

    already = db.query_one(
        "SELECT count(*) AS n FROM user_facts WHERE source_session = ?", (session_id,)
    )
    if already is not None and int(already["n"]) > 0:
        return False
    produced = db.query_one(
        "SELECT (SELECT count(*) FROM learning_routes WHERE session_id = ?)"
        " + (SELECT count(*) FROM topic_maps WHERE session_id = ?)"
        " + (SELECT count(*) FROM notes WHERE session_id = ?) AS n",
        (session_id, session_id, session_id),
    )
    produced_n = int(produced["n"]) if produced is not None else 0
    return user_turns >= 2 or produced_n > 0


def mark_extracted(db: Database, session_id: str) -> None:
    """记下"刚刚在这条会话的第几轮抽过记忆"（nudge 的续写式触发靠它作基准）。

    **无论抽没抽到新事实都要记**：不然抽到空表时，下一轮又会触发一次，白花钱。
    """
    turns = db.query_one(
        "SELECT count(*) AS n FROM messages WHERE session_id = ? AND role = 'user'", (session_id,)
    )
    current = int(turns["n"]) if turns is not None else 0
    set_session_meta(db, session_id, {_EXTRACT_TURN_KEY: current})
