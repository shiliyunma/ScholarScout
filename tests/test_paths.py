"""``paths``：``slugify`` 与**按布局**（默认 ``<会话>/<格式>``）的路径拼装（docs/19 §1/§2/§5）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout import paths

SHA = "ac4d3d6e" + "0" * 56


def test_slugify_strips_windows_illegal_chars() -> None:
    assert paths.slugify('a<b>c:d"e/f\\g|h?i*j') == "a-b-c-d-e-f-g-h-i-j"


def test_slugify_collapses_whitespace_and_underscores() -> None:
    assert paths.slugify("hello   world__again") == "hello-world-again"
    assert paths.slugify("  leading and trailing  ") == "leading-and-trailing"


def test_slugify_keeps_cjk_and_latin() -> None:
    assert paths.slugify("教育智能体 Agent 设计") == "教育智能体-Agent-设计"


def test_slugify_truncates_with_tilde_at_cap() -> None:
    slug = paths.slugify("x" * 500)
    assert len(slug) == paths.SLUG_MAX
    assert slug.endswith("~")
    assert not slug.endswith("-~")  # 截断处不该留下孤零零的连字符


def test_slugify_removes_control_chars() -> None:
    assert paths.slugify("a\x00b\x1fc\x7fd") == "abcd"


def test_slugify_empty_title_falls_back_to_domain() -> None:
    assert paths.slugify("   ", fallback="arxiv.org") == "arxiv.org"
    # 全非法字符（///）也等同空标题
    assert paths.slugify("///", fallback="juejin.cn") == "juejin.cn"


def test_slugify_without_title_or_domain_is_untitled() -> None:
    assert paths.slugify("") == "untitled"


def test_doc_type_mapping() -> None:
    assert paths.doc_type_for("arxiv") == "paper"
    assert paths.doc_type_for("openalex") == "paper"
    assert paths.doc_type_for("github") == "code"
    assert paths.doc_type_for("web") == "doc"
    assert paths.doc_type_for("official") == "doc"
    assert paths.doc_type_for("something-new") == "other"
    assert paths.doc_type_for(None) == "other"


def test_year_month_bucket_rejects_bad_values() -> None:
    assert paths.year_month_bucket("2024-10") == "2024-10"
    assert paths.year_month_bucket("2024-13") == "unknown"
    assert paths.year_month_bucket("") == "unknown"
    assert paths.year_month_bucket(None) == "unknown"


# --------------------------------------------------------------- 布局（docs/19 §2）


def test_default_layout_is_session_then_format() -> None:
    assert paths.DEFAULT_LAYOUT == ("session", "format")
    assert paths.normalize_layout(None) == ("session", "format")


def test_normalize_layout_accepts_string_and_sequence() -> None:
    assert paths.normalize_layout("session/format") == ("session", "format")
    assert paths.normalize_layout(["collected", "session"]) == ("collected", "session")
    # 多余斜杠产生的空段被忽略，别拼出一个空目录名
    assert paths.normalize_layout("session//format") == ("session", "format")


def test_normalize_layout_rejects_unknown_segment() -> None:
    """未知段**直接报错**：静默回默认 = 用户以为改了、其实还写在老位置。"""
    with pytest.raises(ValueError, match="未知的布局段"):
        paths.normalize_layout("session/year")
    with pytest.raises(ValueError, match="布局参数不能为空"):
        paths.normalize_layout("")


def test_type_segment_is_alias_of_format() -> None:
    assert paths.layout_dir("type", fmt="paper") == paths.layout_dir("format", fmt="paper")
    assert paths.layout_dir("type", fmt="paper") == Path("paper")


def test_raw_path_uses_session_then_format(tmp_path: Path) -> None:
    path = paths.raw_path_for_layout(
        root=tmp_path,
        session="调研一",
        fmt="paper",
        collected="2026-10",
        published="2024-10",
        title="A Vision Model",
        sha256=SHA,
        ext="pdf",
    )
    assert path == tmp_path / "data" / "raw" / "调研一" / "paper" / "A-Vision-Model--ac4d3d6e.pdf"


def test_text_path_matches_raw_name_differs_only_by_suffix(tmp_path: Path) -> None:
    placement = {
        "session": "调研一",
        "fmt": "code",
        "collected": "2026-10",
        "published": "2024-10",
        "title": "repo: guide",
        "sha256": "deadbeef" + "0" * 56,
    }
    raw = paths.raw_path_for_layout(root=tmp_path, ext="html", **placement)
    text = paths.text_path_for_layout(root=tmp_path, **placement)
    assert raw.stem == text.stem
    assert raw.name == "repo-guide--deadbeef.html"
    assert text.name == "repo-guide--deadbeef.txt"
    assert raw.parts[-4] == "raw"
    assert text.parts[-4] == "text"
    assert raw.parts[-3] == text.parts[-3] == "调研一"
    assert raw.parts[-2] == text.parts[-2] == "code"


def test_missing_session_and_format_fall_back(tmp_path: Path) -> None:
    """没有会话/格式上下文时落 ``unknown/other``（不编）。"""
    path = paths.raw_path_for_layout(
        root=tmp_path, title="T", sha256="deadbeef" + "0" * 56, ext="html"
    )
    assert path.parent == tmp_path / "data" / "raw" / "unknown" / "other"


def test_layout_with_month_segments_puts_year_month_dirs(tmp_path: Path) -> None:
    path = paths.raw_path_for_layout(
        "session/collected/format",
        root=tmp_path,
        session="调研一",
        fmt="paper",
        collected="2026-10",
        title="T",
        sha256="deadbeef" + "0" * 56,
        ext="pdf",
    )
    assert path.parent == tmp_path / "data" / "raw" / "调研一" / "2026-10" / "paper"


def test_unknown_month_lands_in_unknown_bucket(tmp_path: Path) -> None:
    path = paths.raw_path_for_layout(
        "collected/format",
        root=tmp_path,
        fmt="doc",
        collected="2024/10",  # 不合规形态 → unknown，不拼出奇怪目录名
        title="T",
        sha256="deadbeef" + "0" * 56,
        ext="html",
    )
    assert path.parent == tmp_path / "data" / "raw" / "unknown" / "doc"


def test_layout_segments_cannot_escape_data(tmp_path: Path) -> None:
    """目录段带分隔符/``..`` 必须被抹平（规则 §3.8：不许越出 data/）。"""
    path = paths.raw_path_for_layout(
        root=tmp_path,
        session="../..",
        fmt="a/b",
        title="T",
        sha256="deadbeef" + "0" * 56,
        ext="html",
    )
    assert ".." not in path.parts
    assert path.parent == tmp_path / "data" / "raw" / "unknown" / "a-b"


def test_missing_title_falls_back_to_domain(tmp_path: Path) -> None:
    path = paths.raw_path_for_layout(
        root=tmp_path,
        session="s",
        fmt="doc",
        title="",
        sha256="deadbeef" + "0" * 56,
        ext="html",
        domain="juejin.cn",
    )
    assert path.name == "juejin.cn--deadbeef.html"


def test_same_title_different_sha_do_not_collide(tmp_path: Path) -> None:
    common = {
        "session": "s",
        "fmt": "doc",
        "collected": "2024-10",
        "published": "2024-10",
        "title": "Same Title",
        "ext": "html",
        "root": tmp_path,
    }
    first = paths.raw_path_for_layout(sha256="a" * 64, **common)
    second = paths.raw_path_for_layout(sha256="b" * 64, **common)
    assert first != second
    assert first.name == "Same-Title--aaaaaaaa.html"
    assert second.name == "Same-Title--bbbbbbbb.html"


def test_total_path_stays_under_windows_limit(tmp_path: Path) -> None:
    long_title = "标题" * 300  # 远超上限，应被截到 60 字符
    path = paths.raw_path_for_layout(
        root=tmp_path,
        session="调研一" * 5,
        fmt="paper",
        title=long_title,
        sha256="ab" * 32,
        ext="pdf",
    )
    assert len(paths.rel_to_home(path, tmp_path)) <= 130
    assert len(str(path)) <= 260
