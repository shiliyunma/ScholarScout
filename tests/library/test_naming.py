"""命名元数据：类型 / 年-月 / 真标题的推导口径（docs/19 §1/§2/§5）。"""

from __future__ import annotations

from scholarscout.library import naming


def test_arxiv_number_gives_year_month() -> None:
    assert naming.year_month_for("https://arxiv.org/abs/2410.24164") == "2024-10"
    assert naming.year_month_from_arxiv(None, {"external_id": "2512.11315"}) == "2025-12"
    # arxiv.org/html/... 也认（这条来源的 source_type 其实是 web）
    assert naming.year_month_for("https://arxiv.org/html/2507.15015v1") == "2025-07"


def test_invalid_arxiv_month_is_not_a_match() -> None:
    # DOI/期刊号里的 ``9946.2026``、``2025.36179`` 不是 arxiv 号（月份非法），不许误判
    doi = "https://doi.org/10.1109/icccn69946.2026.11662970"
    assert naming.year_month_from_arxiv(doi, {}) is None
    assert naming.year_month_for(doi, {}) == "unknown"


def test_meta_date_used_when_no_arxiv_number() -> None:
    assert naming.year_month_for("https://github.com/a/b", {"pushed_at": "2026-10-04"}) == "2026-10"
    assert (
        naming.year_month_for("https://x.test/p", {"published": "2024-03-09T00:00:00Z"})
        == "2024-03"
    )


def test_published_preferred_over_updated() -> None:
    meta = {"updated_at": "2020-01-01", "published": "2024-10-10"}
    assert naming.year_month_from_meta(meta) == "2024-10"


def test_unknown_when_nothing_usable() -> None:
    assert naming.year_month_for("https://openclaw.ai/", {}) == "unknown"
    assert naming.year_month_for(None, None) == "unknown"


def test_pick_title_skips_urls_and_blanks() -> None:
    assert naming.pick_title("https://example.com/x", "Real Title") == "Real Title"
    assert naming.pick_title("www.example.com", "") == ""
    assert naming.pick_title(None, "  教育智能体  ") == "教育智能体"


def test_domain_of_strips_www_and_lowercases() -> None:
    assert naming.domain_of("https://www.arXiv.org/abs/x") == "arxiv.org"
    assert naming.domain_of("not a url") == ""
