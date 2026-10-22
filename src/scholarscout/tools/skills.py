"""``skill.list`` / ``skill.load`` —— 让模型按需翻本地技能库（渐进式披露的两个工具）。

为什么是**两个**工具而不是一个：清单与全文的代价差两个数量级（一页 20 条 ≈ 2k 字符，
一篇 ``SKILL.md`` 动辄 5k-20k）。合成一个"给名字就回全文、不给就回清单"的工具，
模型很容易一次要十几个名字，把整个技能库读进上下文 —— 那正是渐进式披露要避免的事。
所以：**先 list 一页，再 load 一篇**，每次都要模型自己做选择。

索引**不在系统提示词里**（``tests/tools/test_skills.py`` 有闸钉住）：提示词只说"有这两个工具、
什么时候该用"；清单每次由工具现取。

回包走 :mod:`scholarscout.tools.envelope`：成功 ``{ok, data, refs}``，失败 ``{ok:false, error, ...}``。
找不到 skill 是**正常回包**（``data.error_kind = "not_found"`` + 可改写的近似名），不是异常、更不是 500。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.skills.index import (
    ATTACHMENT_DIRS,
    get_index,
    list_attachments,
    search,
)
from scholarscout.tools.envelope import ToolResult, fail, ok

#: ``skill.list`` 默认返回条数（一页 20 条，够在界面上滚一屏，也不至于把提示词预算吃掉）
DEFAULT_LIMIT = 20

#: 上限：模型要"全量"时最多给这么多条（再多就该用 query 收窄，而不是一次读 130 条描述）
MAX_LIMIT = 200

LIST_DESCRIPTION = (
    "列出本地技能库（skills/）里的技能：只回名字 + 一句话描述 + 有没有 references/scripts/templates，"
    "**不回正文**。不给 query 就回全部索引（受 limit 限制）；给 query 做大小写不敏感的字符匹配，"
    "连续命中越长、位置越靠前越优先。要用某一篇的完整步骤时，再调 skill.load。"
)

LOAD_DESCRIPTION = (
    "读一篇技能的 SKILL.md 全文，并附上它与正文同级的 references/、scripts/、templates/ 文件清单"
    "（只有相对路径与字节数，没有文件内容）。名字要从 skill.list 里拿，不要凭印象编。"
)

LIST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "关键词（任务里的实词，如 docx / rag / latex）；留空回全部索引",
        },
        "limit": {
            "type": "integer",
            "description": f"最多回多少条（默认 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}）",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_LIMIT,
        },
    },
    "required": [],
    "additionalProperties": False,
}

LOAD_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "技能名（skill.list 回的那个 name，精确或忽略大小写）",
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}


def _clamp_limit(limit: int | None) -> int:
    """把 limit 收进 ``[1, MAX_LIMIT]``；回包里带上**生效值**，所以不是静默改参数。"""
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def skill_list(ctx: ToolContext, query: str | None = None, limit: int | None = None) -> ToolResult:
    """列技能（不读正文）。无 query 时按名字排；有 query 时按 :func:`search` 的排序。"""
    index = get_index()
    effective = _clamp_limit(limit)
    text = (query or "").strip()
    page, matched = search(index, text, effective)

    return ok(
        {
            "total_indexed": len(index.entries),
            "query": text,
            "matched": matched,
            "count": len(page),
            "limit": effective,
            "truncated": matched > len(page),
            "skills": [entry.as_summary() for entry in page],
        },
        refs={"skill_names": [entry.name for entry in page]},
    )


def _attachment_stats(groups: dict[str, list[dict[str, Any]]]) -> tuple[int, int]:
    """``(文件数, 总字节)``：模型据此判断"这篇的附件是不是要另想办法读"。"""
    files = [item for group in groups.values() for item in group]
    return len(files), sum(int(item["bytes"]) for item in files)


def skill_load(ctx: ToolContext, name: str) -> ToolResult:
    """读一篇 skill 的全文 + 附件清单；找不到回 ``ok:false``（``error_kind=not_found``）。"""
    index = get_index()
    wanted = (name or "").strip()
    if not wanted:
        return fail(
            "skill_name_required",
            "skill.load 需要 name：先用 skill.list（可带 query）拿到准确的名字。",
            data={"error_kind": "invalid_argument"},
        )

    entry = index.get(wanted)
    if entry is None:
        near = [item.name for item in search(index, wanted, 3)[0]]
        hint = f"；相近的有：{', '.join(near)}" if near else ""
        return fail(
            "skill_not_found",
            f"技能库里没有名为 {wanted!r} 的技能{hint}。先用 skill.list 查，别猜名字。",
            data={"error_kind": "not_found", "query": wanted, "suggestions": near},
        )

    try:
        content: str = Path(entry.path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return fail(
            "skill_unreadable",
            f"技能 {entry.name} 的 SKILL.md 读不出来（{exc.__class__.__name__}: {exc}）。",
            data={"error_kind": "io_error", "path": entry.display_path},
        )

    groups = list_attachments(entry)
    file_count, total_bytes = _attachment_stats(groups)
    return ok(
        {
            "name": entry.name,
            "description": entry.description,
            "category": entry.category,
            "path": entry.display_path,
            "chars": len(content),
            "content": content,
            # 只出现**真的有文件**的那几类（目录不存在 / 空的都不编）
            "attachments": groups,
            "attachment_groups": [group for group in ATTACHMENT_DIRS if group in groups],
            "attachment_count": file_count,
            "attachment_bytes": total_bytes,
        },
        refs={"skill_names": [entry.name]},
    )


SPEC_LIST = ToolSpec(
    name="skill.list",
    description=LIST_DESCRIPTION,
    parameters=LIST_PARAMETERS,
    handler=skill_list,
    toolset="research",
)

SPEC_LOAD = ToolSpec(
    name="skill.load",
    description=LOAD_DESCRIPTION,
    parameters=LOAD_PARAMETERS,
    handler=skill_load,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC_LIST, replace=True)
    registry.register(SPEC_LOAD, replace=True)
