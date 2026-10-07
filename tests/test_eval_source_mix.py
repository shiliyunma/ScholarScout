"""来源时效性与多样性指标（E3 报告新增的那张表）。

为什么加：原来的"不同来源数"只能说明"没有退化到只挂一个 id"，
回答不了"这批资料够不够新、够不够杂"。而调研类产物最容易被质疑的就是这两点。

口径（这三条是本文件的测试重点，因为口径错比代码错更贵）：
1. **未知年份单列，且不进"近两年占比"的分母** —— 取不到年份就不编；
2. `now_year` **必须显式传入**，不在函数里读今天（否则同一份数据不同日子跑出不同结论）；
3. 多题合并（`sum_mixes`）时桶标签必须一致，否则会静默错位 —— 宁可报错。
"""

from __future__ import annotations

import pytest
from eval.run_eval import _source_meta

from eval import metrics

NOW = 2026


def test_year_mix_buckets_and_counts_unknown_separately() -> None:
    """2026 年跑、窗口 2 年：≥2025 / 2023–2024 / ≤2022 / 未知。"""
    mix = metrics.year_mix([2026, 2025, 2024, 2023, 2020, None, None], now_year=NOW)
    labels = list(mix)
    assert mix[labels[0]] == 2  # 2026, 2025
    assert mix[labels[1]] == 2  # 2024, 2023
    assert mix[labels[2]] == 1  # 2020
    assert mix["未知年份"] == 2
    assert "2025" in labels[0], "桶标签要写出年份边界，报告里才不用猜"


def test_year_mix_labels_depend_on_now_year() -> None:
    """换一年跑，桶标签跟着变（所以 `now_year` 必须显式传）。"""
    assert list(metrics.year_mix([], now_year=2026)) != list(metrics.year_mix([], now_year=2027))


def test_fresh_share_excludes_unknown_years_from_the_denominator() -> None:
    """分母只算有年份的：3 条里 2 条新 → 66.7%，未知年份不参与。"""
    share = metrics.fresh_share([2026, 2025, 2019, None, None], now_year=NOW)
    assert share == pytest.approx(2 / 3)


def test_fresh_share_returns_none_without_any_known_year() -> None:
    """全都没年份 → None（"没有样本"和"0%"是两件事）。"""
    assert metrics.fresh_share([None, None], now_year=NOW) is None


def test_kind_mix_sorts_by_count_and_keeps_blanks() -> None:
    mix = metrics.kind_mix(["arxiv", "arxiv", "github", "", "web"])
    assert list(mix)[:2] == ["arxiv", "github"]
    assert list(mix)[-1] == "?", "空类型排在最后（与年份分布的「未知年份」一致），但不能丢"


def test_kind_mix_of_nothing_is_empty() -> None:
    assert metrics.kind_mix([]) == {}


def test_sum_mixes_combines_by_label() -> None:
    first = metrics.year_mix([2026], now_year=NOW)
    second = metrics.year_mix([2025, None], now_year=NOW)
    merged = metrics.sum_mixes([first, second])
    assert sum(merged.values()) == 3
    assert merged["未知年份"] == 1


def test_sum_mixes_refuses_mismatched_labels() -> None:
    """不同 now_year 的分布不能合并 —— 标签会错位，宁可炸。"""
    with pytest.raises(ValueError, match="桶标签不一致"):
        metrics.sum_mixes(
            [metrics.year_mix([2026], now_year=2026), metrics.year_mix([2026], now_year=2027)]
        )


def test_distinct_domains_style_source_meta_roundtrip() -> None:
    """`_source_meta` 是唯一取数入口：只认库里的元数据，缺年份就返回 None。"""

    class FakeDb:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[str, ...]]] = []

        def query(self, sql: str, params: tuple[str, ...]) -> list[dict[str, object]]:
            self.calls.append((sql, params))
            return [{"id": "src_a", "year": 2026, "source_type": "arxiv", "domain": "arxiv.org"}]

    db = FakeDb()
    rows = _source_meta(db, ["src_a", "src_b"])
    assert len(rows) == 1
    assert "IN (?,?)" in db.calls[0][0]
    assert db.calls[0][1] == ("src_a", "src_b")


def test_source_meta_is_empty_without_ids() -> None:
    """没引用任何来源时不该去查库（省一次查询，也避免空 IN 的 SQL 错误）。"""

    class BoomDb:
        def query(self, sql: str, params: tuple[str, ...]) -> list[dict[str, object]]:
            raise AssertionError("不该查库")

    assert _source_meta(BoomDb(), []) == []
