"""抓正文的按 host 限速口径 —— 与检索共用 ``search.min_interval_s`` 那一张表。

**为什么需要这一层**：``min_interval_s`` 原来只作用于 provider 检索（``providers/*.py`` 把它传给
``http.fetch``），而 ``source.fetch`` 抓正文时**没传** → 出网层默认 0 → 对同一 host 可以连发，
只有 ``max_parallel_fetches`` 拦得住并发。抓正文恰恰最容易同站连发：一轮里抓 6 条来源很可能
落在同一个站（arXiv、GitHub、同一篇博客），而服务方看到的只是"同一个客户端连打 6 次"。

表是按**来源名**（``arxiv`` / ``openalex`` …）写的，抓正文只知道 **host**，所以这里做一层
host → 来源名的映射（含子域：``export.arxiv.org`` 也算 ``arxiv``）。表里没有的 host 用
``DEFAULT_BODY_MIN_INTERVAL_S``（1s）：不苛刻，但不放任连发。

**为什么 github 不写默认值**：``providers/settings.py`` 的默认表故意不含 ``github`` —— 它的
间隔取决于有没有配 token（未认证 6s / 认证 2s），由 ``providers/github.py`` 自己算。这里同理：
映射只让"用户显式配了 ``min_interval_s.github``"生效，没配就走 1s 默认，不去猜额度。
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

#: 抓正文时，``search.min_interval_s`` 里没有的 host 用这个间隔（秒）。
DEFAULT_BODY_MIN_INTERVAL_S = 1.0

#: host 后缀 → ``search.min_interval_s`` 里的来源名。顺序无关（后缀互不包含）。
HOST_PROVIDER_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("arxiv.org", "arxiv"),
    ("openalex.org", "openalex"),
    ("github.com", "github"),
    ("semanticscholar.org", "semantic_scholar"),
    ("bing.com", "bing"),
    ("duckduckgo.com", "duckduckgo"),
    ("tavily.com", "tavily"),
    ("serper.dev", "serper"),
    ("brave.com", "brave"),
    ("googleapis.com", "google_cse"),
)


def provider_for_host(host: str) -> str | None:
    """host → 检索来源名（认子域：``export.arxiv.org`` → ``arxiv``）；认不出回 ``None``。"""
    lowered = (host or "").strip().lower().split(":", 1)[0]
    if not lowered:
        return None
    for suffix, provider in HOST_PROVIDER_SUFFIXES:
        if lowered == suffix or lowered.endswith("." + suffix):
            return provider
    return None


def interval_for_host(
    host: str,
    intervals: Mapping[str, float],
    default: float = DEFAULT_BODY_MIN_INTERVAL_S,
) -> float:
    """某个 host 两次抓取之间至少要隔多久（秒）。

    ``intervals`` 就是 ``SearchSettings.min_interval_s``（含用户的覆盖）；认不出的 host 与
    "映射到了但表里没这个来源"（如未配 token 的 github）都回 ``default``。
    """
    provider = provider_for_host(host)
    if provider is not None:
        value = intervals.get(provider)
        # 0 / 负数当"没写"：0 等于不限速，别让一个笔误把抓正文的限速整个关掉
        if isinstance(value, int | float) and not isinstance(value, bool) and float(value) > 0:
            return float(value)
    return float(default)


def interval_for_url(
    url: str,
    intervals: Mapping[str, float],
    default: float = DEFAULT_BODY_MIN_INTERVAL_S,
) -> float:
    """``interval_for_host`` 的 URL 版（抓正文时手里只有 URL）。"""
    return interval_for_host(urlparse(url).hostname or "", intervals, default)


__all__ = [
    "DEFAULT_BODY_MIN_INTERVAL_S",
    "HOST_PROVIDER_SUFFIXES",
    "interval_for_host",
    "interval_for_url",
    "provider_for_host",
]
