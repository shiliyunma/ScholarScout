"""``note.save`` —— 存笔记、总结与抽背卡（docs/01 §5.6）。

- 三类：``summary``（总结）/ ``note``（普通笔记）/ ``card``（抽背卡，正反面）
- 同时导出 Markdown 到 ``data/notes/<note_id>.md``（库里 content 也是 Markdown，两边一致）
- 卡片支持批量：``cards=[{front, back}, ...]``；也接受 ``title``=问题、``content``=答案的单卡写法
"""

from __future__ import annotations

from typing import Any

from scholarscout import paths
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.learning import save_note
from scholarscout.state.sessions import new_id
from scholarscout.tools.envelope import ToolResult, ok

DESCRIPTION = (
    "把结论固化成笔记、总结或抽背卡（正反面），并导出 Markdown 到本地。"
    "抽背卡可以一次给多张（每张 front/back），入库后可反查它引用了哪些资料。"
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["summary", "note", "card"], "description": "产物类型"},
        "title": {"type": "string", "description": "标题；kind=card 且未给 cards 时作为卡片正面"},
        "content": {"type": "string", "description": "正文；kind=card 且未给 cards 时作为卡片背面"},
        "cards": {
            "type": "array",
            "description": "抽背卡（kind=card 时可用）",
            "items": {
                "type": "object",
                "properties": {
                    "front": {"type": "string"},
                    "back": {"type": "string"},
                },
                "required": ["front", "back"],
                "additionalProperties": False,
            },
        },
        "refs": {
            "type": "object",
            "description": "引用的资料 id",
            "properties": {
                "source_ids": {"type": "array", "items": {"type": "string"}},
                "doc_ids": {"type": "array", "items": {"type": "string"}},
                "summary_ids": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "tags": {"type": "array", "items": {"type": "string"}, "description": "标签"},
    },
    "required": ["kind"],
    "additionalProperties": False,
}

QUALITY_RULES = (
    "写卡标准：一道题只考一个点；正面是可独立回答的问题，背面是答案（带数字与来源）；"
    "抽背时按「1 道原题 + 2 道延伸题」组织，不要把一堆问题一次抛出来。",
    "选择题必须把选项写进题干（A. …／B. …／C. …／D. …），答案给选项字母；填空题写「答案：<词>」；",
    "简答题直接给参考答案。**没有选项的选择题在界面上没法选**，等于废卡。",
)


def render_cards(cards: list[dict[str, str]]) -> str:
    lines: list[str] = ["# 抽背卡", ""]
    for index, card in enumerate(cards, start=1):
        lines.append(f"## Q{index}. {card['front'].strip()}")
        lines.append("")
        lines.append(f"**A.** {card['back'].strip()}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def note_save(
    ctx: ToolContext,
    kind: str,
    title: str | None = None,
    content: str | None = None,
    cards: list[dict[str, str]] | None = None,
    refs: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> ToolResult:
    if kind not in {"summary", "note", "card"}:
        raise ValueError(f"非法 kind：{kind!r}")

    normalized_cards: list[dict[str, str]] = []
    for card in cards or []:
        front = str(card.get("front") or "").strip()
        back = str(card.get("back") or "").strip()
        if not front or not back:
            raise ValueError("每张卡片都要有 front 与 back")
        normalized_cards.append({"front": front, "back": back})

    if kind == "card" and not normalized_cards:
        if not (title and content):
            raise ValueError("kind=card 需要 cards，或同时给 title（问题）与 content（答案）")
        normalized_cards.append({"front": title.strip(), "back": content.strip()})

    if kind == "card":
        body = render_cards(normalized_cards)
        note_title = title or (normalized_cards[0]["front"][:32] if normalized_cards else "抽背卡")
    else:
        body = (content or "").strip()
        if not body:
            raise ValueError(f"kind={kind} 需要 content")
        note_title = title or body[:32]

    note_id = new_id("not")
    target = paths.note_path_for(note_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# {note_title}\n\n> tags: {', '.join(tags or []) or '无'}\n\n"
        if tags
        else f"# {note_title}\n\n"
    )
    target.write_text(header + body, encoding="utf-8")

    saved_id = save_note(
        ctx.db,
        kind=kind,
        title=note_title,
        content=body,
        refs=refs or {},
        tags=tags or [],
        path=paths.rel_to_home(target),
        session_id=ctx.session_id,
        note_id=note_id,
    )

    return ok(
        {
            "note_id": saved_id,
            "kind": kind,
            "title": note_title,
            "path": paths.rel_to_home(target),
            "card_count": len(normalized_cards),
            "cards": normalized_cards,
        },
        refs={
            "note_ids": [saved_id],
            "source_ids": list((refs or {}).get("source_ids") or []),
            "doc_ids": list((refs or {}).get("doc_ids") or []),
            "summary_ids": list((refs or {}).get("summary_ids") or []),
        },
    )


SPEC = ToolSpec(
    name="note.save",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=note_save,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)
