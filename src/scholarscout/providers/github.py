"""GitHub 仓库检索（**免 key 也可用**，官方 Search API）。

为什么这个源对"学习辅助"特别重要：真机跑 E3 时，路线里"无出处"的资料类条目
几乎全是**代码与教程**（`公开 DKT 实现（含数据加载与训练脚本）`、`框架官方入门教程`），
而 arXiv 只给论文 —— 官方文档、可运行实现、示例代码得靠 GitHub 补。

实测（2026-10-05 本机、国内网络、未认证）：`api.github.com` 直连 **0.9s** 返回真结果
（`pykt-team/pykt-toolkit` ⭐441、`chrispiech/DeepKnowledgeTracing` ⭐313）。
`api.github.com/robots.txt` 返回 **404** → 按 robots 标准即"无限制"，所以**不需要**进豁免名单。

限速（官方口径，代码里按这个设最小间隔）：
- 未认证：Search API **10 次/分钟** → 最小间隔 6s
- 认证（`.env` 里配 `GITHUB_TOKEN`）：**30 次/分钟** → 最小间隔 2s
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.base import Query, SourceCard
from scholarscout.providers.websearch import parse_year

NAME = "github"
ENDPOINT = "https://api.github.com/search/repositories"
API_KEY_ENV = "GITHUB_TOKEN"
#: 官方推荐显式声明 API 版本，避免哪天默认版本变了悄悄改语义
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"
MIN_INTERVAL_S = 6.0
MIN_INTERVAL_WITH_TOKEN_S = 2.0


def card_from_repo(item: dict[str, Any]) -> SourceCard | None:
    """把一个仓库对象压成 SourceCard。"""
    full_name = str(item.get("full_name") or "").strip()
    url = str(item.get("html_url") or "").strip()
    if not full_name or not url:
        return None
    description = str(item.get("description") or "").strip()
    language = str(item.get("language") or "").strip()
    stars = item.get("stargazers_count")
    pushed_at = str(item.get("pushed_at") or item.get("updated_at") or "")
    archived = bool(item.get("archived"))
    extra = {
        "provider": NAME,
        "stars": str(stars if stars is not None else ""),
        "language": language,
        "archived": "true" if archived else "false",
        "pushed_at": pushed_at[:10],
    }
    # 归档仓库照样收，但把状态写进 extra —— 让判权与路线安排能看见"这仓库不维护了"
    return SourceCard(
        url=url,
        title=full_name,
        source_type="github",
        abstract=description or None,
        snippet=description or None,
        # 年份取"最近推送"：判"这份资料现在还能不能用"比建仓时间有用
        year=parse_year(pushed_at),
        venue=language or None,
        external_id=full_name,
        extra=extra,
    )


def parse_results(payload: dict[str, Any], *, limit: int) -> list[SourceCard]:
    cards: list[SourceCard] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        card = card_from_repo(item)
        if card is not None:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


def search_query_url(*, endpoint: str, text: str, limit: int) -> str:
    """按 star 降序取最活跃的高星仓库（学习路线里"先能跑起来"的那批）。"""
    params = {
        "q": text,
        "sort": "stars",
        "order": "desc",
        "per_page": str(max(1, min(limit, 30))),
    }
    return f"{endpoint}?{urlencode(params)}"


class GithubProvider:
    """GitHub 仓库检索。没配 token 也能用，只是间隔拉长到 6s（官方 10 次/分钟）。"""

    name = NAME

    def __init__(
        self,
        http: HttpClient,
        *,
        token: str | None = None,
        endpoint: str = ENDPOINT,
        min_interval_s: float | None = None,
    ) -> None:
        self.http = http
        self.token = (token or "").strip()
        self.endpoint = endpoint
        self.min_interval_s = (
            min_interval_s
            if min_interval_s is not None
            else (MIN_INTERVAL_WITH_TOKEN_S if self.token else MIN_INTERVAL_S)
        )

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def search(self, query: Query) -> list[SourceCard]:
        headers = {"X-GitHub-Api-Version": API_VERSION}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self.http.fetch(
            search_query_url(endpoint=self.endpoint, text=query.text, limit=query.limit),
            accept=ACCEPT,
            headers=headers,
            min_interval_s=self.min_interval_s,
            cache=False,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            return []
        return parse_results(payload, limit=query.limit)
