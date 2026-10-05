"""把一次会话导出成可复制的 Markdown（左侧栏「分享」）。

为什么是 Markdown：用户要的是"能直接复制粘贴给别人"的东西 —— 纯文本 md 最通用，
不需要对方装任何东西，也不会把界面样式一起带走。

导出内容（按顺序）：

1. 元信息（标题、会话号、模型、时间）；
2. 对话正文（只带 `active = 1` 的消息，与界面看到的一致）；
3. 每一轮的**工具轨迹**（调了什么、成不成功）—— 论文与复盘都要靠它；
4. 出处清单（这次会话引用过的来源）；
5. 学习产物与笔记（有才写）。

**只读**：本模块不改任何数据；不导出密钥、不导出系统提示词、不导出原始 JSON 大字段。
"""

from __future__ import annotations

import re
from typing import Any

from scholarscout import paths
from scholarscout.state import learning, sessions, traces
from scholarscout.state.db import Database

#: 消息正文里可能出现的内网/本机绝对路径，导出前打码（分享给别人才不会泄漏目录结构）
_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\)\]]+|/(?:home|Users)/[^\s\)\]]+")

ROLE_LABELS = {"user": "我", "assistant": "ScholarScout", "system": "系统"}


def _scrub(text: Any) -> str:
    """把机器相关的绝对路径换成 `<path>`（导出件是给别人看的）。"""
    return _PATH_RE.sub("<path>", str(text or ""))


def _stamp(value: Any) -> str:
    text = str(value or "")
    return text.replace("T", " ")[:19]


def build_markdown(db: Database, session_id: str, *, exports_dir: Any = None) -> str:
    """生成 Markdown 文本。会话不存在抛 `KeyError`。"""
    session = sessions.get_session(db, session_id)
    if session is None:
        raise KeyError(session_id)

    lines: list[str] = []
    title = _scrub(session.get("title") or "未命名会话")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(
        "> 会话 `{sid}`｜模型 `{model}`｜创建 {created}｜更新 {updated}".format(
            sid=session.get("id") or session_id,
            model=_scrub(session.get("model") or "未知"),
            created=_stamp(session.get("created_at")),
            updated=_stamp(session.get("updated_at")),
        )
    )
    lines.append("")

    messages, _has_more = sessions.list_messages(db, session_id, limit=500)
    # 只导出"对话"：`tool` 消息是内部管道（一条就是几千字符的原始 JSON），
    # `system` 是提示词。它们对分享对象毫无价值，只会把文档撑成不可读；
    # 工具调用另有下面「工具轨迹」一节做摘要。
    dialogue = [m for m in messages if str(m.get("role")) in {"user", "assistant"}]
    if dialogue:
        lines.append("## 对话")
        lines.append("")
        for message in dialogue:
            role = ROLE_LABELS.get(str(message.get("role")), str(message.get("role")))
            lines.append(f"**{role}**（{_stamp(message.get('created_at'))}）")
            lines.append("")
            lines.append(_scrub(message.get("content")))
            lines.append("")

    tool_rows = traces.list_traces(db, session_id=session_id, limit=500)
    if tool_rows:
        lines.append("## 工具轨迹")
        lines.append("")
        for row in tool_rows:
            mark = "✅" if row.get("ok") else "❌"
            detail = row.get("summary") or row.get("result_digest") or ""
            suffix = f" —— {_scrub(detail)}" if detail else ""
            lines.append(
                f"- {mark} `{row.get('tool_name')}`（{_stamp(row.get('created_at'))}）{suffix}"
            )
        lines.append("")

    mentions = db.query(
        "SELECT * FROM source_mentions WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    )
    if mentions:
        lines.append("## 出处")
        lines.append("")
        seen: set[str] = set()
        for mention in mentions:
            item = dict(mention)
            key = str(item.get("source_id") or item.get("url") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            label = item.get("title") or item.get("source_id") or ""
            url = item.get("url") or ""
            lines.append(f"- {_scrub(label)}" + (f" — {_scrub(url)}" if url else ""))
        lines.append("")

    maps = [m for m in learning.list_topic_maps(db, limit=200) if m.get("session_id") == session_id]
    routes = [r for r in learning.list_routes(db, limit=200) if r.get("session_id") == session_id]
    notes = [n for n in learning.list_notes(db, limit=200) if n.get("session_id") == session_id]
    if maps or routes or notes:
        lines.append("## 学习产物")
        lines.append("")
        for item in maps:
            lines.append(f"- 知识地图：{_scrub(item.get('topic') or item.get('title'))}")
        for item in routes:
            lines.append(f"- 学习路线：{_scrub(item.get('topic') or item.get('title'))}")
        for item in notes:
            kind = item.get("kind") or "note"
            body = _scrub(item.get("title") or item.get("body") or "")
            lines.append(f"- 笔记（{kind}）：{body}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("由 ScholarScout 导出（本地存储，无云端上传）。")
    lines.append("")
    return "\n".join(lines)


def export_to_file(db: Database, session_id: str, *, out_dir: Any = None) -> dict[str, str]:
    """落一份文件到 `data/exports/`，返回 ``{path, text}``（path 是相对项目根的路径）。"""
    text = build_markdown(db, session_id)
    target_dir = paths.data_dir(out_dir) / "exports" if out_dir else paths.data_dir() / "exports"
    target_dir.mkdir(parents=True, exist_ok=True)
    session = sessions.get_session(db, session_id) or {}
    stamp = _stamp(session.get("created_at")).replace(" ", "-").replace(":", "")
    name = f"{session_id}-{stamp or 'export'}.md"
    path = target_dir / name
    path.write_text(text, encoding="utf-8")
    try:
        relative = str(path.relative_to(paths.home()))
    except ValueError:  # 自定义目录（测试里常见）——照实回绝对路径
        relative = str(path)
    return {"path": relative, "text": text}
