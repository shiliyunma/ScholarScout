"""抓正文的按 host 限速口径：host → ``search.min_interval_s`` 那张表。

为什么单测这一层：``min_interval_s`` 曾经**只作用于检索**，抓正文时 ``source.fetch`` 根本没把它
传给 ``http.fetch``（出网层默认 0）—— 于是"承诺按来源限速"在抓正文这条路上是空的。
这里钉住三件事：arXiv 的 3s 抓正文同样生效、认子域、表里没有的 host 回 1s。
"""

from __future__ import annotations

import pytest

from scholarscout.fetch.throttle import (
    DEFAULT_BODY_MIN_INTERVAL_S,
    interval_for_host,
    interval_for_url,
    provider_for_host,
)
from scholarscout.providers.settings import DEFAULT_MIN_INTERVAL


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # arXiv：检索 3s 的口径直接用在抓正文上（含 export./www. 子域）
        ("https://export.arxiv.org/pdf/2501.00001", 3.0),
        ("https://arxiv.org/abs/2501.00001", 3.0),
        ("https://www.arxiv.org/abs/2501.00001", 3.0),
        # 其它在表里的来源
        ("https://api.openalex.org/works/W1", 0.5),
        ("https://api.semanticscholar.org/graph/v1/paper/1", 1.5),
        # 表里没有的 host → 小默认值（不苛刻，但不放任连发）
        ("https://docs.example.com/guide", DEFAULT_BODY_MIN_INTERVAL_S),
        ("https://export.arxiv.org.evil.example/x", DEFAULT_BODY_MIN_INTERVAL_S),
        # 空主机（相对地址/解析失败）也不许抛
        ("/local/path.pdf", DEFAULT_BODY_MIN_INTERVAL_S),
    ],
)
def test_interval_for_url_uses_the_search_table(url: str, expected: float) -> None:
    assert interval_for_url(url, DEFAULT_MIN_INTERVAL) == expected


def test_github_has_no_default_interval_in_the_table() -> None:
    """github 的间隔取决于有没有 token（6s/2s），默认表故意不写 → 抓正文回 1s 默认。

    只在用户**显式**配了 ``min_interval_s.github`` 时才按表走 —— 这跟
    ``providers/README.md`` 里"别用默认表覆盖 github 自己算的额度"是同一条纪律。
    """
    assert "github" not in DEFAULT_MIN_INTERVAL
    assert interval_for_url("https://github.com/a/b", DEFAULT_MIN_INTERVAL) == 1.0
    assert interval_for_url("https://github.com/a/b", {"github": 6.0}) == 6.0


def test_provider_for_host_matches_subdomains_only() -> None:
    assert provider_for_host("export.arxiv.org") == "arxiv"
    assert provider_for_host("arxiv.org") == "arxiv"
    assert provider_for_host("notarxiv.org") is None
    assert provider_for_host("") is None


def test_interval_for_host_falls_back_for_non_positive_entries() -> None:
    """表里写了 0/负数就当没写：0 等于"不限速"，不能靠一个笔误把限速关掉。"""
    assert interval_for_host("arxiv.org", {"arxiv": 0}) == DEFAULT_BODY_MIN_INTERVAL_S
    assert interval_for_host("arxiv.org", {"arxiv": -3}) == DEFAULT_BODY_MIN_INTERVAL_S
    assert interval_for_host("arxiv.org", {"arxiv": 2.5}) == 2.5
