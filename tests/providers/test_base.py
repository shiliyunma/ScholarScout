"""检索来源公共类型的契约：URL 规范化、去重、年份提取。"""

from __future__ import annotations

from scholarscout.providers.base import SourceCard, canonical_url, dedupe_cards, year_from_iso


def test_canonical_url_strips_tracking_and_fragment() -> None:
    url = "https://Example.COM/Path/?utm_source=x&b=2&a=1&fbclid=z#section"
    assert canonical_url(url) == "https://example.com/Path?a=1&b=2"


def test_canonical_url_drops_default_port_and_trailing_slash() -> None:
    assert canonical_url("http://example.com:80/a/") == "http://example.com/a"
    assert canonical_url("https://example.com:443/a/") == "https://example.com/a"
    assert canonical_url("https://example.com:8443/a/") == "https://example.com:8443/a"


def test_canonical_url_keeps_meaningful_query_and_sorts_it() -> None:
    assert (
        canonical_url("https://arxiv.org/abs/2501.1?x=2&y=1")
        == "https://arxiv.org/abs/2501.1?x=2&y=1"
    )
    assert canonical_url("https://e.com/p?y=1&x=2") == "https://e.com/p?x=2&y=1"


def test_canonical_url_defaults_scheme_and_root_path() -> None:
    assert canonical_url("example.com") == "https://example.com/"


def test_card_domain_and_canonical() -> None:
    card = SourceCard(
        url="https://arXiv.org/abs/1?v=2&utm_source=t", title="t", source_type="arxiv"
    )
    assert card.domain == "arxiv.org"
    assert card.canonical == "https://arxiv.org/abs/1?v=2"


def test_to_row_shape() -> None:
    card = SourceCard(
        url="https://e.com/a",
        title="标题",
        source_type="web",
        authors=("A", "B"),
        year=2025,
        external_id="abc",
    )
    row = card.to_row()
    assert row["url_canonical"] == "https://e.com/a"
    assert row["authors_json"] == ["A", "B"]
    assert row["meta_json"] == {"external_id": "abc", "pdf_url": None}


def test_dedupe_keeps_first_occurrence() -> None:
    a = SourceCard(url="https://e.com/x?utm_source=1", title="first", source_type="web")
    b = SourceCard(url="https://e.com/x", title="second", source_type="web")
    c = SourceCard(url="https://e.com/y", title="third", source_type="web")
    assert [card.title for card in dedupe_cards([a, b, c])] == ["first", "third"]


def test_year_from_iso() -> None:
    assert year_from_iso("2025-01-01T00:00:00Z") == 2025
    assert year_from_iso("") is None
    assert year_from_iso(None) is None
    assert year_from_iso("xxxx-01-01") is None
