"""来源状态视图：哪些搜索引擎能用、为什么不能用、怎么才能用。

这张表要同时满足两件互相牵制的事：

1. **口径必须与 `auto_names` 一致** —— 界面说"可用"的来源，检索时就必须真的被带上；
   不一致用户会觉得"我明明配好了却没用上"（这类错觉最难排查）。
   所以这里有个不变量测试：`usable` 与 `name in auto_names()` 必须逐个对上。
2. **不可用时必须给出"怎么才能用"** —— 只写"不可用"等于让用户去读源码。
"""

from __future__ import annotations

import pytest

from scholarscout.providers.settings import (
    COMMERCIAL_KEYS,
    DISPLAY_ORDER,
    KEYLESS,
    SearchSettings,
    provider_statuses,
)


def _statuses(settings: SearchSettings, keys: dict[str, str] | None = None):
    table = keys or {}
    lookup = lambda name: table.get(name) or None  # noqa: E731 - 测试里就是要短
    return {item.name: item for item in provider_statuses(settings, key_lookup=lookup)}


def test_every_implemented_provider_is_listed_in_display_order() -> None:
    """表里必须覆盖所有已实现来源（少了就等于用户永远看不见它）。"""
    table = _statuses(SearchSettings(providers=("auto",)))
    assert tuple(table) == DISPLAY_ORDER
    assert set(DISPLAY_ORDER) == set(KEYLESS) | set(COMMERCIAL_KEYS)


def test_keyless_sources_are_usable_without_any_key() -> None:
    table = _statuses(SearchSettings(providers=("auto",)))
    for name in ("arxiv", "openalex", "github"):
        assert table[name].usable, name
        assert table[name].reason == "ok"
        assert table[name].env_var is None


def test_github_token_is_optional_not_required() -> None:
    """GitHub 免 key 也能用，token 只是提额度 —— 不能把可选说成必需。"""
    table = _statuses(SearchSettings(providers=("auto",)))
    assert table["github"].usable
    assert table["github"].key_configured is False
    assert table["github"].optional_env_var == "GITHUB_TOKEN"


def test_searxng_needs_a_base_url() -> None:
    table = _statuses(SearchSettings(providers=("auto",)))
    assert table["searxng"].usable is False
    assert table["searxng"].reason == "missing_config"
    assert "searxng_up" in table["searxng"].note, "必须告诉用户怎么才能用"
    table = _statuses(SearchSettings(providers=("auto",), searxng_base_url="http://localhost:8080"))
    assert table["searxng"].usable is True


def test_commercial_sources_flip_to_usable_once_the_key_is_set() -> None:
    before = _statuses(SearchSettings(providers=("auto",)))
    for name in ("tavily", "serper", "brave"):
        assert before[name].usable is False
        assert before[name].reason == "missing_api_key"
        assert before[name].env_var == COMMERCIAL_KEYS[name]
    after = _statuses(
        SearchSettings(providers=("auto",)),
        {"tavily": "t", "serper": "s", "brave": "b"},
    )
    assert all(after[name].usable for name in ("tavily", "serper", "brave"))


def test_google_cse_needs_both_key_and_cx() -> None:
    """只填 key 不够 —— 这一点必须在界面上说清，否则用户会以为填了就该可用。"""
    only_key = _statuses(SearchSettings(providers=("auto",)), {"google_cse": "k"})
    assert only_key["google_cse"].usable is False
    assert only_key["google_cse"].reason == "missing_config"
    only_cx = _statuses(SearchSettings(providers=("auto",), google_cse_cx="cx"))
    assert only_cx["google_cse"].usable is False
    assert only_cx["google_cse"].reason == "missing_api_key"
    both = _statuses(SearchSettings(providers=("auto",), google_cse_cx="cx"), {"google_cse": "k"})
    assert both["google_cse"].usable is True


def test_searchpage_scraping_is_off_by_default_and_says_why() -> None:
    table = _statuses(SearchSettings(providers=("auto",)))
    for name in ("bing", "duckduckgo"):
        assert table[name].usable is False
        assert table[name].reason == "blocked_by_robots"
        assert "robots" in table[name].note
    on = _statuses(SearchSettings(providers=("auto",), allow_searchpage_scraping=True))
    assert on["bing"].usable and on["duckduckgo"].usable


def test_semantic_scholar_is_treated_as_key_required() -> None:
    """无 key 恒 429（实测）→ 按"需要 key"报，而不是假装它能用。"""
    table = _statuses(SearchSettings(providers=("auto",)))
    assert table["semantic_scholar"].usable is False
    assert table["semantic_scholar"].reason == "missing_api_key"


@pytest.mark.parametrize(
    "settings,keys",
    [
        (SearchSettings(providers=("auto",)), {}),
        (SearchSettings(providers=("auto",), searxng_base_url="http://x"), {}),
        (SearchSettings(providers=("auto",)), {"brave": "k", "tavily": "k"}),
        (SearchSettings(providers=("auto",), google_cse_cx="cx"), {"google_cse": "k"}),
        (SearchSettings(providers=("auto",), allow_searchpage_scraping=True), {}),
    ],
)
def test_usable_matches_what_auto_would_actually_use(settings: SearchSettings, keys: dict) -> None:
    """**不变量**：界面标"可用"的来源必须真的会被 auto 带上，反之亦然。"""
    lookup = lambda name: keys.get(name) or None  # noqa: E731
    auto = set(settings.auto_names(key_lookup=lookup))
    for item in provider_statuses(settings, key_lookup=lookup):
        if item.name in ("bing", "duckduckgo"):
            continue  # 抓页来源永远不进 auto（即使打开开关，也要显式点名）
        assert item.usable == (item.name in auto), f"{item.name}: 界面={item.usable} auto={auto}"
