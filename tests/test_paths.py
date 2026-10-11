"""``paths``：``slugify`` 与新的「类型/年-月」路径拼装（docs/19 §1/§2/§5）。"""

from __future__ import annotations

from pathlib import Path

from scholarscout import paths


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


def test_raw_path_uses_type_and_year_month_buckets(tmp_path: Path) -> None:
    path = paths.raw_path_for_doc(
        "arxiv", "2024-10", "A Vision Model", "ac4d3d6e" + "0" * 56, "pdf", root=tmp_path
    )
    assert path == tmp_path / "data" / "raw" / "paper" / "2024-10" / "A-Vision-Model--ac4d3d6e.pdf"


def test_text_path_matches_raw_name_differs_only_by_suffix(tmp_path: Path) -> None:
    arguments = ("github", "2026-10", "repo: guide", "deadbeef" + "0" * 56)
    raw = paths.raw_path_for_doc(*arguments, "html", root=tmp_path)
    text = paths.text_path_for_doc(*arguments, root=tmp_path)
    assert raw.stem == text.stem
    assert raw.name == "repo-guide--deadbeef.html"
    assert text.name == "repo-guide--deadbeef.txt"
    assert raw.parts[-4] == "raw"
    assert text.parts[-4] == "text"
    assert raw.parts[-3] == text.parts[-3] == "code"


def test_unknown_month_lands_in_unknown_bucket(tmp_path: Path) -> None:
    path = paths.raw_path_for_doc(
        "web", "unknown", "T", "deadbeef" + "0" * 56, "html", root=tmp_path
    )
    assert path.parent.name == "unknown"
    # 不合规的月份字符串同样落 unknown，不会拼出奇怪目录名
    again = paths.raw_path_for_doc(
        "web", "2024/10", "T", "deadbeef" + "0" * 56, "html", root=tmp_path
    )
    assert again.parent.name == "unknown"


def test_missing_title_falls_back_to_domain(tmp_path: Path) -> None:
    path = paths.raw_path_for_doc(
        "web", "unknown", "", "deadbeef" + "0" * 56, "html", domain="juejin.cn", root=tmp_path
    )
    assert path.name == "juejin.cn--deadbeef.html"


def test_same_title_different_sha_do_not_collide(tmp_path: Path) -> None:
    first = paths.raw_path_for_doc("web", "2024-10", "Same Title", "a" * 64, "html", root=tmp_path)
    second = paths.raw_path_for_doc("web", "2024-10", "Same Title", "b" * 64, "html", root=tmp_path)
    assert first != second
    assert first.name == "Same-Title--aaaaaaaa.html"
    assert second.name == "Same-Title--bbbbbbbb.html"


def test_total_path_stays_under_windows_limit(tmp_path: Path) -> None:
    long_title = "标题" * 300  # 远超上限，应被截到 60 字符
    path = paths.raw_path_for_doc(
        "openalex", "2024-10", long_title, "ab" * 32, "pdf", root=tmp_path
    )
    assert len(paths.rel_to_home(path, tmp_path)) <= 130
    assert len(str(path)) <= 260
