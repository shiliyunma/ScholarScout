"""``research.search`` —— 开放世界检索（docs/01 §5.1）。

行为约定：
- 只调用**已实现**的来源；配置里写了但没实现的记为 ``not_implemented``，不假装成功
- 单个来源失败**不阻塞**其它来源，失败原因进 ``provider_status``
- 命中的来源**必须先入库**再返回 source_id（去重靠 url_canonical）
"""

from __future__ import annotations

from typing import Any

from scholarscout.fetch.http import FetchError
from scholarscout.providers.arxiv import ArxivProvider
from scholarscout.providers.base import Query, dedupe_cards
from scholarscout.providers.settings import KNOWN
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.ingest import upsert_source

DESCRIPTION = (
    "在外部学术与公开网络上检索资料。返回带 source_id 的候选清单（已入库，可直接引用），"
    "并在 provider_status 里报告每个来源的成败。用于回答“这个方向有哪些资料/该怎么入门”。"
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索词，越具体越好（自然语言短语）"},
        "sources": {
            "type": "array",
            "items": {"type": "string", "enum": list(KNOWN)},
            "description": "限定来源；默认用配置里的来源列表",
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "返回条数上限"},
        "year_from": {
            "type": "integer",
            "minimum": 1900,
            "description": "只保留这个年份之后的资料",
        },
        "sort": {"type": "string", "enum": ["relevance", "date"], "description": "排序依据"},
    },
    "required": ["query"],
    "additionalProperties": False,
}


def available_providers(ctx: ToolContext) -> dict[str, Any]:
    """本版已实现的来源（M3 只有 arXiv，其余在 M3 末补齐）。"""
    http = ctx.require_http()
    return {"arxiv": ArxivProvider(http)}


def _card_payload(card: Any, source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "title": card.title,
        "url": card.url,
        "domain": card.domain,
        "authors": list(card.authors),
        "year": card.year,
        "venue": card.venue,
        "source_type": card.source_type,
        "tier": None,  # 判权在 M4（source.grade）
        "tier_label": None,
        "score": None,
        "snippet": card.snippet,
        "has_full_text": bool(card.pdf_url),
        "external_id": card.external_id,
    }


def research_search(
    ctx: ToolContext,
    query: str,
    sources: list[str] | None = None,
    limit: int | None = None,
    year_from: int | None = None,
    sort: str = "relevance",
) -> dict[str, Any]:
    settings = ctx.config.search_cfg
    wanted = [str(name) for name in sources] if sources else list(settings.providers)
    providers = available_providers(ctx)
    per_provider = max(1, min(int(limit or settings.per_provider_limit), 50))

    status: list[dict[str, Any]] = []
    collected: list[Any] = []
    for name in wanted:
        provider = providers.get(name)
        if provider is None:
            status.append({"provider": name, "ok": False, "n": 0, "error": "not_implemented"})
            continue
        interval = settings.interval_for(name, provider.min_interval_s)
        try:
            found = provider.search(
                Query(text=query, limit=per_provider, year_from=year_from, sort=sort)
            )
        except FetchError as exc:
            status.append({"provider": name, "ok": False, "n": 0, "error": f"{exc.kind}: {exc}"})
            continue
        except Exception as exc:
            status.append(
                {"provider": name, "ok": False, "n": 0, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        status.append({"provider": name, "ok": True, "n": len(found), "interval_s": interval})
        collected.extend(found)

    cards = dedupe_cards(collected)
    if year_from is not None:
        cards = [card for card in cards if card.year is None or card.year >= int(year_from)]
    total = int(limit or settings.default_limit)
    cards = cards[: max(1, total)]

    items: list[dict[str, Any]] = []
    source_ids: list[str] = []
    for card in cards:
        source_id, _created = upsert_source(ctx.db, card.to_row())
        source_ids.append(source_id)
        items.append(_card_payload(card, source_id))

    return {
        "ok": True,
        "data": {"items": items, "provider_status": status, "query": query},
        "refs": {"source_ids": source_ids},
    }


SPEC = ToolSpec(
    name="research.search",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=research_search,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)
