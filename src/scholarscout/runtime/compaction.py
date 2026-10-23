"""模型摘要式压缩（D3）：历史太长时，用**一次模型调用**把中段压成结构化提要。

## 与已有规则式瘦身的分工

| 机制 | 做什么 | 花不花钱 |
|---|---|---|
| `context_budget.project_tool_result` / `collapse_old_tool_results` | 工具结果去重、截断、折叠 | 不花 |
| `_fit_history` | 保头保尾裁剪到字符预算 | 不花 |
| **本模块** | 被裁掉的那一段，用模型压成"目标 / 已确认结论（带来源 id）/ 未决问题 / 用户偏好" | **一次小调用** |

规则式裁剪会把中段**直接丢掉**（第 1 轮的关键证据就是这么没的）。本模块把"丢掉"
换成"压缩"，代价是每压一段一次调用 —— 用户明确要求要有，但做成**只在跨过阈值时触发一次**，
并把开销按 `purpose="compact"` 如实记账，免得它变成看不见的暗开销。

## 三条口径

1. **同一段只压一次**：提要与"已覆盖到第几条消息"一起存进研究台账；下轮只压**新增**的部分
   （把上次的提要当"续写"的起点喂进去）。否则每轮都要重压一遍，等于每轮多花一次钱。
2. **失败不阻塞**：模型调用失败就退回规则式裁剪（`_fit_history`），本轮照常进行 ——
   压缩是省钱手段，不能因为它把一轮弄挂。
3. **提要位置固定**：插在 system 提示词**之后、历史之前**。位置固定，服务端缓存才能在
   "两次压缩之间"稳定命中；每次压缩会让缓存冷一次（这是可接受的代价，且只冷一次，
   而不是每轮都冷 —— 真机实测稳定前缀时缓存命中率约 70%）。
"""

from __future__ import annotations

from typing import Any

from scholarscout.runtime import research_state
from scholarscout.state import sessions
from scholarscout.state.db import Database

#: 存在研究台账里的键
SUMMARY_KEY = "history_summary"
COVERED_KEY = "history_summary_count"
#: 至少这么多字符才值得为它花一次调用（太短的话规则式裁剪就够了）
MIN_SPAN_CHARS = 1200
#: 默认阈值：未被提要覆盖的那段超过它才触发
DEFAULT_AFTER_CHARS = 4000


def _summarized_count(state: dict[str, Any]) -> int:
    try:
        return int(state.get(COVERED_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def pending_span(
    db: Database, session_id: str, *, state: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """还没被提要覆盖的那部分消息（用户/助手，且有内容）。

    「哪些算一条要压的消息」是本模块的口径（跳过 `tool`/`system`、跳过空正文），
    读行与排序在 `state/sessions.py::message_rows`。
    """
    current = state if state is not None else (research_state.load_state(db, session_id) or {})
    rows = sessions.message_rows(db, session_id)
    usable = [
        {"id": str(row["id"]), "role": str(row["role"]), "content": str(row["content"])}
        for row in rows
        if str(row["role"]) in {"user", "assistant"} and str(row["content"]).strip()
    ]
    return usable[_summarized_count(current) :]


def span_chars(span: list[dict[str, str]]) -> int:
    return sum(len(item["content"]) for item in span)


def should_compact(
    db: Database,
    session_id: str,
    *,
    state: dict[str, Any] | None = None,
    after_chars: int = DEFAULT_AFTER_CHARS,
) -> bool:
    span = pending_span(db, session_id, state=state)
    return span_chars(span) >= max(MIN_SPAN_CHARS, int(after_chars))


def build_transcript(span: list[dict[str, str]], *, max_chars: int = 12000) -> str:
    """把片段渲染成"用户/助手"交替的文本，超出预算就**保留尾部**（越近越重要）。"""
    lines = [f"[{item['role']}] {item['content']}" for item in span]
    text = "\n\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


def compact(
    llm: Any,
    db: Database,
    session_id: str,
    *,
    prompts_dir: Any,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """压一次。返回 ``{summary, covered, usage, reason}``；失败时 ``reason`` 非空、``summary`` 为 None。"""
    from scholarscout.summary.prompts import PromptPack

    current = state if state is not None else (research_state.load_state(db, session_id) or {})
    span = pending_span(db, session_id, state=current)
    if not span:
        return {
            "summary": None,
            "covered": _summarized_count(current),
            "usage": {},
            "reason": "没有待压缩的内容",
        }
    previous = str(current.get(SUMMARY_KEY) or "（还没有，这是第一次压缩）")
    try:
        prompt = PromptPack(prompts_dir).compose(
            "compact-history", previous=previous[:2000], transcript=build_transcript(span)
        )
        reply = llm.chat([{"role": "user", "content": prompt}])
    except Exception as exc:
        return {
            "summary": None,
            "covered": _summarized_count(current),
            "usage": {},
            "reason": f"{type(exc).__name__}: {exc}",
        }
    text = str(getattr(reply, "content", "") or "").strip()
    if not text:
        return {
            "summary": None,
            "covered": _summarized_count(current),
            "usage": {},
            "reason": "模型返回空提要",
        }
    usage = {
        key: int(value)
        for key, value in (getattr(reply, "usage", {}) or {}).items()
        if isinstance(value, int)
    }
    covered = _summarized_count(current) + len(span)
    return {"summary": text, "covered": covered, "usage": usage, "reason": None}


def save_summary(db: Database, session_id: str, *, summary: str, covered: int) -> None:
    state = research_state.load_state(db, session_id) or research_state.empty_state()
    state[SUMMARY_KEY] = summary
    state[COVERED_KEY] = int(covered)
    research_state.save_state(db, session_id, state)


def render_summary_note(summary: str) -> str:
    """注入用的文本（位置固定在 system 之后）。"""
    return (
        "【较早对话的提要】（系统压缩生成，用于省 token；只信这里列出的、带来源 id 的结论）\n\n"
        + str(summary).strip()
    )
