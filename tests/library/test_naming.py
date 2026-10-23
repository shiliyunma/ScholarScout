"""命名元数据：类型 / 发表年月 / 收录年月 / 真标题 / 会话目录名的推导口径（docs/19 §1/§2/§5）。"""

from __future__ import annotations

from scholarscout.library import naming
from scholarscout.state.db import Database
from scholarscout.state.sessions import create_session


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


def test_collected_month_reads_fetched_at() -> None:
    """**收录年月**取 ``documents.fetched_at``（"我什么时候采到它"）。"""
    assert naming.collected_month("2026-10-05T10:00:00+08:00") == "2026-10"
    assert naming.collected_month("2024-03-09 00:00:00") == "2024-03"


def test_collected_month_unknown_when_unusable() -> None:
    """取不到就 ``unknown``，绝不拿别的日期冒充（与发表年月同口径）。"""
    assert naming.collected_month(None) == "unknown"
    assert naming.collected_month("") == "unknown"
    assert naming.collected_month("不是时间") == "unknown"
    assert naming.collected_month("2024-13-01") == "unknown"  # 月份非法


def test_session_short_id_is_last_four_alnum() -> None:
    assert naming.session_short_id("ses_s2d2ytta") == "ytta"
    assert naming.session_short_id("ses_ab12") == "ab12"
    # 太短的 id 原样用（不补位数，也不编）
    assert naming.session_short_id("x7") == "x7"


def test_session_dir_name_uses_slug_and_short_id_on_collision() -> None:
    assert naming.session_dir_name("调研 一 教育", "ses_ab12") == "调研-一-教育"
    assert (
        naming.session_dir_name("调研 一 教育", "ses_ab12", colliding=True) == "调研-一-教育--ab12"
    )
    # 标题取不到 → unknown，不编一个看着像会话名的字符串
    assert naming.session_dir_name(None, "ses_ab12") == "unknown"


def test_session_dir_names_give_colliding_sessions_short_ids() -> None:
    """同名会话**都**带短 id（顺序无关，迁移/重建才幂等）。"""
    got = naming.session_dir_names(
        [("ses_ab12", "教育智能体"), ("ses_cd34", "教育智能体"), ("ses_ef56", "别的")]
    )
    assert got == {
        "ses_ab12": "教育智能体--ab12",
        "ses_cd34": "教育智能体--cd34",
        "ses_ef56": "别的",
    }


def test_session_dir_names_order_independent_for_collisions() -> None:
    forward = naming.session_dir_names([("ses_ab12", "同名"), ("ses_cd34", "同名")])
    reverse = naming.session_dir_names([("ses_cd34", "同名"), ("ses_ab12", "同名")])
    assert forward == reverse


def test_session_dir_map_reads_sessions_from_the_db(db: Database) -> None:
    """搬走 SQL 之后接线仍然通：目录名由库里**真实会话**算，同名都带短 id，未命名落 unknown。"""
    first = create_session(db, title="教育智能体", source="cli")
    second = create_session(db, title="教育智能体", source="desktop")
    unnamed = create_session(db, source="cli")

    got = naming.session_dir_map(db)

    assert got == {
        first: f"教育智能体--{naming.session_short_id(first)}",
        second: f"教育智能体--{naming.session_short_id(second)}",
        unnamed: "unknown",
    }
