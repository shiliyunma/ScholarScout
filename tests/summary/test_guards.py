"""守卫契约：清洗、截断、长度规则、确定性压缩、数字可溯源。"""

from __future__ import annotations

from scholarscout.summary import guards


def test_clean_text_removes_scripts_tags_urls_and_entities() -> None:
    raw = (
        "<html><head><style>a{}</style><script>var x=1;</script></head>"
        "<body><p>Hello&nbsp;&amp; welcome</p><a href='https://x.com/a'>link</a></body></html>"
    )
    cleaned = guards.clean_text(raw)
    assert "script" not in cleaned and "https://x.com/a" not in cleaned
    assert "Hello & welcome" in cleaned
    assert cleaned.count(" ") + 1 <= len(cleaned)


def test_clean_text_collapses_blank_lines() -> None:
    assert guards.clean_text("a\n\n\n\nb") == "a\n\nb"


def test_clean_text_none_is_empty() -> None:
    assert guards.clean_text(None) == ""


def test_clamp_text_counts_codepoints() -> None:
    assert guards.clamp_text("教育智能体", 3) == "教育智…"
    assert guards.clamp_text("abc", 10) == "abc"
    assert guards.clamp_text("abc", 0) == ""


def test_count_sentences() -> None:
    assert guards.count_sentences("第一句。第二句！第三句？") == 3
    assert guards.count_sentences("没有句号") == 1


def test_length_ok_boundaries() -> None:
    long_source = "正" * 600
    assert guards.length_ok("正" * 100 + "。", source_chars=len(long_source)) is False  # 只有 1 句
    assert (
        guards.length_ok("正" * 60 + "。" + "正" * 60 + "。", source_chars=len(long_source)) is True
    )
    assert (
        guards.length_ok("正" * 200 + "。" + "正" * 10 + "。", source_chars=len(long_source))
        is False
    )


def test_length_ok_short_source_allows_shorter_summary() -> None:
    assert guards.length_ok("正" * 55 + "。", source_chars=100) is True


def test_length_ok_rejects_empty() -> None:
    assert guards.length_ok("", source_chars=1000) is False


def test_compact_summary_trims_to_limit_without_cutting_numbers() -> None:
    text = "该工作在 MATH 上把准确率从 42% 提升到 58%。" * 6
    compacted = guards.compact_summary(text, max_chars=160)
    assert len(compacted) <= 160
    assert "42%" in compacted
    assert compacted.endswith("。")


def test_compact_summary_keeps_short_text_untouched() -> None:
    assert guards.compact_summary("短文本。", max_chars=160) == "短文本。"


def test_compact_summary_falls_back_to_clause_cut() -> None:
    text = "第一分句非常非常长" * 10 + "。"
    compacted = guards.compact_summary(text, max_chars=30)
    assert len(compacted) <= 31


def test_ungrounded_numbers_detects_fabrication() -> None:
    source = "该工作在 MATH 上把准确率从 42% 提升到 58%。"
    assert guards.ungrounded_numbers("准确率从 42% 提升到 58%。", source) == []
    assert guards.ungrounded_numbers("准确率从 42% 提升到 91%。", source) == ["91%"]


def test_ungrounded_numbers_handles_versions_and_sizes() -> None:
    assert guards.ungrounded_numbers("支持 7B 与 v1.2.3。", "支持 7B。") == ["v1.2.3"]


def test_numbers_in_empty_text() -> None:
    assert guards.numbers_in("") == set()
