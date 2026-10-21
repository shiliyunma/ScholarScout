"""``research.search`` —— 开放世界检索（docs/01 §5.1）。

行为约定：
- 只调用**已实现**的来源；配置里写了但没实现的记为 ``not_implemented``，不假装成功
- 单个来源失败**不阻塞**其它来源，失败原因进 ``provider_status``
- 命中的来源**必须先入库**再返回 source_id（去重靠 url_canonical）
"""

from __future__ import annotations

import contextlib
from typing import Any

from scholarscout.providers.arxiv import ArxivProvider
from scholarscout.providers.base import dedupe_cards
from scholarscout.providers.bing import BingProvider
from scholarscout.providers.brave import BraveProvider
from scholarscout.providers.duckduckgo import DuckDuckGoProvider
from scholarscout.providers.github import GithubProvider
from scholarscout.providers.google_cse import GoogleCseProvider
from scholarscout.providers.openalex import OpenAlexProvider
from scholarscout.providers.searxng import SearxngProvider
from scholarscout.providers.semantic_scholar import SemanticScholarProvider
from scholarscout.providers.serper import SerperProvider
from scholarscout.providers.settings import (
    AUTO,
    COMMERCIAL_KEYS,
    KNOWN,
    api_key_for,
    optional_token,
)
from scholarscout.providers.tavily import TavilyProvider
from scholarscout.runtime import search_intent
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.subtasks import fetch_many
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.ingest import upsert_source
from scholarscout.tools.envelope import ToolResult, fail, ok

DESCRIPTION = (
    "在多个来源里检索资料并入库，返回带 source_id 的结果，"
    "并在 provider_status 里报告每个来源的成败。用于回答“这个方向有哪些资料/该怎么入门”。"
    "来源：arxiv（预印本）；openalex（期刊/会议论文与综述，带引用数）；"
    "github（仓库/官方实现/教程，星星多的排前）；searxng（自建通用检索）；"
    "bing / duckduckgo（抓结果页，默认关）；"
    "tavily / serper / brave / google_cse / semantic_scholar（需在 .env 配 key，"
    "未配置时如实报告 missing_api_key）。"
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
        "openalex": OpenAlexProvider(http, mailto=settings.openalex_mailto or None),
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
    s2_key = api_key_for("semantic_scholar")
    if s2_key:
        providers["semantic_scholar"] = SemanticScholarProvider(http, api_key=s2_key)
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


def _choice_event(ctx: ToolContext, *, query: str, intent: search_intent.Intent | None) -> None:
    """把"需要用户选一次"推给界面。

    用的是既有的事件通道（``ctx.emit`` → ``rpc/server.py`` 的 JSON-RPC 通知），只是**新增了一个
    事件名** ``chat.needs_choice``（已登记进 ``NOTIFICATION_METHODS``，否则会被加 ``chat.``
    前缀而永远到不了界面）。为什么不复用 ``chat.tool_result``：界面要拿 ``options`` 直接渲染
    选择卡，混在工具结果通知里要前端自己从 digest 里反解，反而更脆。
    """
    emit = ctx.emit
    if emit is None:
        return
    # 界面事件失败不能连累检索（与 research.delegate 的口径一致）
    with contextlib.suppress(Exception):
        emit(
            "chat.needs_choice",
            {
                "turn_id": ctx.turn_id,
                "session_id": ctx.session_id,
                "query": query,
                "options": search_intent.options(),
                "intent": intent.as_dict() if intent is not None else None,
            },
        )


def _skipped_result(query: str) -> ToolResult:
    """用户选择不检索：**不搜**，但要让模型明确知道"该直接回答"。

    ``ok=False`` 是刻意的：这一轮确实没拿到任何证据。主循环也只对"真的搜过且没新来源"计
    ``empty_rounds`` / 记判重台账 —— 不搜不能被算成"搜了没结果"，否则会误触发"停搜"提示，
    还会把这个查询钉进判重表（下一轮真想搜时被当成重复）。
    """
    note = (
        "用户已选择本轮不检索。请不要调用检索类工具，直接用已有上下文/通用知识回答，"
        "并在回答里说明哪些部分没有资料支撑。"
    )
    # ok:false 就必须带非空 error（docs/01 §5.0）：模型读 error 决定下一步，界面读 data.note 展示。
    # 顶层 skipped 是已知例外（前端在读，见 tools/envelope.py 顶注），位置不动。
    return fail(
        "search_skipped",
        "本轮按用户要求没有检索（skipped）：直接用已有上下文/通用知识回答，"
        "并说明哪些部分没有资料支撑。",
        data={"items": [], "skipped": True, "query": query},
        note=note,
        refs={"source_ids": []},
        skipped=True,
    )


def _choice_result(query: str, intent: search_intent.Intent) -> ToolResult:
    """分类置信度低且用户没表态：**不猜**，把选项交出去（模型转述给用户）。"""
    options = search_intent.options()
    note = (
        "这次没说清要哪类资料，先别搜。请把下面这些选项转成一个简短的问题问用户，"
        "等他/她选完再检索（不要自己替用户选，也不要换个说法重搜）。"
    )
    # options / needs_choice 留在信封顶层（已知例外，前端直接渲染选择卡）；error 写"先问用户"，
    # 模型不必从 data.note 里猜下一步该怎么处理。
    return fail(
        "search_needs_choice",
        "没说清要哪类资料（needs_choice）：先按 options 问用户选一类，选完再检索。",
        data={
            "needs_choice": True,
            "items": [],  # 与其它返回路径同形（预算投影/模型都按 data.items 读结果）
            "query": query,
            "note": note,
            "intent": intent.as_dict(),
        },
        refs={"source_ids": []},
        needs_choice=True,
        options=options,
    )


def research_search(
    ctx: ToolContext,
    query: str,
    sources: list[str] | None = None,
    limit: int | None = None,
    year_from: int | None = None,
    sort: str = "relevance",
) -> ToolResult:
    # ---- 方案 3：先核**每轮检索偏好位**再决定搜不搜/搜哪一层。
    # 偏好位存在研究台账里（不是系统提示）：工具签名与提示词一字未改，改的只是"这一轮查哪几个来源"。
    preference, intent = search_intent.preference_for(ctx)
    gate_enabled = bool(getattr(ctx.config.runtime, "classify_search_intent", True))

    if preference == "none":
        # 用户明确说"别搜了/直接回答"——不消耗任何出网与 token
        return _skipped_result(query)

    if (
        gate_enabled
        and preference is None
        and intent is not None
        and not intent.failed
        and intent.confidence < search_intent.CONFIDENCE_THRESHOLD
    ):
        # 没偏好 + 本轮分类置信度低 → 不搜，给结构化选项并通知界面（选择卡是下一步）
        _choice_event(ctx, query=query, intent=intent)
        return _choice_result(query, intent)
    # 分类器报错（intent.failed）走这里 = **失败开放**：照常检索，绝不因为分类失败而卡住

    # ---- A3：判重闸门。同一会话里"换词重搜同一件事"要被拦下，并告诉它该往哪走。
    # 真机证据：3 个多轮会话里 2 个出现重复查询（其中一轮一个新来源都没发现）。
    # 拦下来比让它搜完更省 —— 一次检索的钱是花了的，重复的结果对结论毫无贡献。
    from scholarscout.runtime import research_state

    # 只**读**台账做判重，**不写** —— 写者只有一个（runtime/loop.py）。
    # 两个写者会丢更新：工具写进去的查询，会被循环在轮末用自己那份内存状态整表覆盖
    # （真机抓到：第 2 轮照样重复搜了两条，因为第 1 轮的查询记录被覆盖没了）。
    state = research_state.load_state(ctx.db, ctx.session_id) if ctx.session_id else None
    if state is not None:
        repeated = research_state.find_repeat(state, query)
        if repeated:
            gaps = (
                research_state.compute_gaps(ctx.db, ctx.session_id, state) if ctx.session_id else []
            )
            hint = (
                gaps[0]["suggest"]
                if gaps
                else "换关键词/换来源/换年份；如果确实没有新信息，就直接如实说明并停止"
            )
            raise ValueError(
                f"查询被拒绝：它与本会话已搜过的「{repeated}」高度重复（判重阈值 "
                f"{research_state.DEFAULT_SIMILARITY}）。同一件事换词重搜不会带来新证据。"
                f"请按缺口清单换方向：{hint}"
            )

    settings = ctx.config.search_cfg
    requested = [str(name) for name in sources] if sources else list(settings.providers)
    wanted = resolve_wanted(settings, requested)
    providers = available_providers(ctx)
    missing = unavailable_providers(ctx)
    per_provider = max(1, min(int(limit or settings.per_provider_limit), 50))

    # ---- 方案 3：有偏好就只查这一族（模型点名了别的族也照偏好收窄 —— 用户的约束优先）。
    family_note: str | None = None
    if search_intent.providers_for(preference) is not None:
        picked = search_intent.filter_providers(wanted, providers, preference)
        if not picked:
            label = search_intent.label_of(preference)
            # ok:false 必须带非空 error（docs/01 §5.0）：这一层没接通是**结论**，不能让模型猜
            return fail(
                "search_family_unavailable",
                f"用户只要{label}，但这一层现在没有可用的来源（缺 key 或没配实例）。",
                data={
                    "items": [],
                    "provider_status": [],
                    "query": query,
                },
                note=(
                    f"用户只要{label}，但这一层现在没有可用的来源（缺 key 或没配实例）。"
                    "请如实告诉用户这一层没接通，不要改用别的类型检索。"
                ),
                refs={"source_ids": []},
            )
        if picked != wanted:
            dropped = [name for name in wanted if name not in picked]
            family_note = (
                f"已按「{search_intent.label_of(preference)}」偏好只查 {', '.join(picked)}"
                f"（跳过 {', '.join(dropped)}）"
            )
        wanted = picked

    # 子 Agent P0（docs/16 §3.3）：并行 provider 扇出 —— 只并行出网，不并发调模型。
    # 逐条返回结果，单来源失败不阻塞其它；status 的键集与旧的顺序循环逐字一致。
    outcomes = fetch_many(
        ctx,
        providers,
        wanted=wanted,
        query=query,
        per_provider=per_provider,
        year_from=year_from,
        sort=sort,
        missing=missing,
    )
    status: list[dict[str, Any]] = [outcome.as_status() for outcome in outcomes]
    collected: list[Any] = [card for outcome in outcomes for card in outcome.cards]

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
        return fail(
            "search_all_failed",
            failure,
            data={"items": [], "provider_status": status, "query": query},
            note=family_note,
            refs={"source_ids": []},
        )

    return ok(
        {
            "items": items,
            "provider_status": status,
            "query": query,
        },
        note=family_note,
        refs={"source_ids": source_ids},
    )


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
