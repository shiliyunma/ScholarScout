"""评测运行器的裁剪开关（`--only` / `--limit`）。

为什么值得测：调提示词时只想跑一题，但**部分跑必须留痕** ——
一份只跑了 e3-01 的报告如果看不出"这不是全套"，很容易被当成完整结论引用（尤其写进论文时）。
另外题号写错时不能悄悄跑全套：那会让人以为改的就是那一题。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from run_eval import filter_rows

ROWS = [{"id": "e3-01"}, {"id": "e3-02"}, {"id": "e3-03"}]


def test_only_picks_exact_ids() -> None:
    assert [r["id"] for r in filter_rows(ROWS, only="e3-01,e3-03")] == ["e3-01", "e3-03"]


def test_only_with_unknown_id_yields_nothing() -> None:
    """写错题号就该空手而归，不能静默跑全套。"""
    assert filter_rows(ROWS, only="e3-99") == []


def test_limit_keeps_order() -> None:
    assert [r["id"] for r in filter_rows(ROWS, limit=2)] == ["e3-01", "e3-02"]


def test_limit_zero_or_negative_means_no_limit() -> None:
    assert len(filter_rows(ROWS, limit=0)) == 3
    assert len(filter_rows(ROWS, limit=-5)) == 3


def test_no_flags_keeps_everything() -> None:
    assert filter_rows(ROWS) == ROWS


def test_only_and_limit_combine() -> None:
    assert [r["id"] for r in filter_rows(ROWS, only="e3-02,e3-03", limit=1)] == ["e3-02"]


def test_partial_runs_are_recorded_in_the_report() -> None:
    """`main()` 里部分跑要写进 results['selection'] —— 报告不能假装自己是全套。"""
    source = (Path(__file__).resolve().parents[1] / "eval" / "run_eval.py").read_text(
        encoding="utf-8"
    )
    assert 'results["selection"]' in source
    assert "if args.only or args.limit" in source
