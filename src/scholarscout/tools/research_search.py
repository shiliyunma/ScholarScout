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
from scholarscout.providers.bing import BingProvider
from scholarscout.providers.brave import BraveProvider
from scholarscout.providers.duckduckgo import DuckDuckGoProvider
from scholarscout.providers.github import GithubProvider
from scholarscout.providers.google_cse import GoogleCseProvider
from scholarscout.providers.searxng import SearxngProvider
from scholarscout.providers.serper import SerperProvider
from scholarscout.providers.settings import (
    AUTO,
    COMMERCIAL_KEYS,
    KNOWN,
    api_key_for,
    optional_token,
)
from scholarscout.providers.tavily import TavilyProvider
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.ingest import upsert_source

DESCRIPTION = (
    "在多个来源里检索资料并入库，返回带 source_id 的结果，"
    "并在 provider_status 里报告每个来源的成败。用于回答“这个方向有哪些资料/该怎么入门”。"
    "来源：arxiv（学术预印本）；github（仓库/官方实现/教程，星星多的排前）；"
    "searxng（自建通用检索）；bing / duckduckgo（抓结果页，默认关）；"
    "tavily / serper / brave / google_cse（商用检索，需在 .env 配 key，未配置时会如实报告 missing_api_key）。"
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
    """本版所有**可直接用**的来源。

    - 学术：``arxiv``
    - 通用（免 key）：``searxng``（配了实例地址才有）、``bing``/``duckduckgo``
      （**仅当** ``search.allow_searchpage_scraping`` 打开才装配 —— Bing robots 禁止 /search）
    - 商用（需 key）：``tavily`` / ``serper`` / ``brave`` / ``google_cse``（后者还要 ``cx``）
    """
    http = ctx.require_http()
    settings = ctx.config.search_cfg
    # github 免 key 也能用（Search API 10 次/分钟）；配了 .env 的 GITHUB_TOKEN 就提额到 30 次/分钟
    providers: dict[str, Any] = {
        "arxiv": ArxivProvider(http),
        "github": GithubProvider(http, token=optional_token("github")),
    }
    if settings.allow_searchpage_scraping:
        providers["bing"] = BingProvider(http)
        providers["duckduckgo"] = DuckDuckGoProvider(http)
    if settings.searxng_base_url.strip():
        providers["searxng"] = SearxngProvider(http, base_url=settings.searxng_base_url)
    for name, factory in (
        ("tavily", TavilyProvider),
        ("serper", SerperProvider),
        ("brave", BraveProvider),
    ):
        key = api_key_for(name)
        if key:
            providers[name] = factory(http, api_key=key)
    cse_key = api_key_for("google_cse")
    if cse_key and settings.google_cse_cx.strip():
        providers["google_cse"] = GoogleCseProvider(
            http, api_key=cse_key, cx=settings.google_cse_cx
        )
    return providers


def unavailable_providers(ctx: ToolContext) -> dict[str, str]:
    """不可用来源 → 原因。**用户必须能区分"没搜到"与"这层没配"**。"""
    settings = ctx.config.search_cfg
    reasons: dict[str, str] = {}
    if not settings.searxng_base_url.strip():
        reasons["searxng"] = "missing_config: search.searxng_base_url（自建实例地址）"
    if not settings.allow_searchpage_scraping:
        blocked = (
            "blocked_by_robots: 搜索结果页在 robots.txt 里被禁（Bing 禁 /search）；"
            "要抓就打开 search.allow_searchpage_scraping，或改用商用 API / 自建 SearxNG"
        )
        reasons["bing"] = blocked
        reasons["duckduckgo"] = blocked + "；另外该站国内需梯子"
    for name, env_name in COMMERCIAL_KEYS.items():
        if api_key_for(name) is None:
            reasons[name] = f"missing_api_key: {env_name}（写在 .env 里）"
    if api_key_for("google_cse") and not settings.google_cse_cx.strip():
        reasons["google_cse"] = (
            "missing_config: search.google_cse_cx（搜索引擎 ID，在 config.yaml）"
        )
    for name in ("openalex", "semantic_scholar"):
        reasons[name] = "not_implemented"
    return reasons


def resolve_wanted(settings: Any, requested: list[str]) -> list[str]:
    """把 ``auto``（以及"用户点名 + auto"的混写）解析成实际要查的来源，保持顺序、去重。"""
    names: list[str] = []
    for name in requested:
        for resolved in settings.auto_names() if name == AUTO else (name,):
            if resolved not in names:
                names.append(resolved)
    return names


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
    requested = [str(name) for name in sources] if sources else list(settings.providers)
    wanted = resolve_wanted(settings, requested)
    providers = available_providers(ctx)
    missing = unavailable_providers(ctx)
    per_provider = max(1, min(int(limit or settings.per_provider_limit), 50))

    status: list[dict[str, Any]] = []
    collected: list[Any] = []
    for name in wanted:
        provider = providers.get(name)
        if provider is None:
            if name in KNOWN:
                status.append(
                    {
                        "provider": name,
                        "ok": False,
                        "n": 0,
                        "error": missing.get(name, "unavailable"),
                    }
                )
            else:
                status.append({"provider": name, "ok": False, "n": 0, "error": "unknown_provider"})
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

    failure = _all_providers_failed(status) if not items else None
    if failure is not None:
        return {
            "ok": False,
            "error": failure,
            "data": {"items": [], "provider_status": status, "query": query},
            "refs": {"source_ids": []},
        }

    return {
        "ok": True,
        "data": {"items": items, "provider_status": status, "query": query},
        "refs": {"source_ids": source_ids},
    }


def _all_providers_failed(status: list[dict[str, Any]]) -> str | None:
    """所有来源都失败且一条都没拿到时，返回一句给人看的原因。

    **为什么必须区分「没结果」与「全失败」**：只回 ``items: []`` 会让模型以为"这个方向没资料"，
    于是反复重试同一个查询、直到把工具预算烧光并开始编造（2026-10-05 真机跑 E3 实测）。
    如实回失败原因，模型才会停下来告诉用户「检索通路有问题」。
    """
    if not status:
        return "没有任何可用的检索来源"
    if any(entry.get("ok") for entry in status):
        return None  # 有来源成功过：空结果是真实结论，不算失败
    reasons = "；".join(f"{entry['provider']}: {entry.get('error')}" for entry in status)
    return f"所有检索来源都失败了（{reasons}）"


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
