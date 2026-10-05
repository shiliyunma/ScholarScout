"""指标口径契约：口径错会让论文里每个数字都错，所以逐个钉死。"""

from __future__ import annotations

from eval import metrics


def test_agreement_counts_and_empty() -> None:
    assert metrics.agreement([("a", "a"), ("b", "c")]) == 0.5
    assert metrics.agreement([]) is None, "没有样本 ≠ 全错"


def test_perfect_agreement_is_one() -> None:
    assert metrics.agreement([("x", "x")] * 5) == 1.0


def test_kappa_zero_when_agreement_is_chance() -> None:
    # 双方各自 50/50，且完全对不上 → Po=0，Pe=0.5 → κ=-1
    pairs = [("a", "b"), ("a", "b"), ("b", "a"), ("b", "a")]
    assert metrics.cohens_kappa(pairs) == -1.0


def test_kappa_one_when_perfect_agreement_with_variation() -> None:
    pairs = [("a", "a"), ("a", "a"), ("b", "b"), ("b", "b")]
    assert metrics.cohens_kappa(pairs) == 1.0


def test_kappa_undefined_for_single_label() -> None:
    assert metrics.cohens_kappa([("a", "a")] * 3) is None, "只有一个类别时 κ 无定义"


def test_confusion_matrix_shape() -> None:
    matrix = metrics.confusion([("核心", "核心"), ("排除", "相关")], ["核心", "相关", "排除"])
    assert matrix["核心"]["核心"] == 1
    assert matrix["相关"]["排除"] == 1
    assert matrix["排除"]["排除"] == 0
    assert set(matrix) == {"核心", "相关", "排除"}


def test_percentile_nearest_rank() -> None:
    values = [10, 20, 30, 40]
    assert metrics.percentile(values, 50) == 20
    assert metrics.percentile(values, 100) == 40
    assert metrics.percentile(values, 1) == 10
    assert metrics.percentile([], 95) is None


def test_percentile_ignores_input_order() -> None:
    assert metrics.percentile([40, 10, 30, 20], 95) == 40


def test_success_rate_counts_failures_in_denominator() -> None:
    rows = [{"ok": 1}, {"ok": 0}, {"ok": True}, {}]
    assert metrics.success_rate(rows) == 0.5
    assert metrics.success_rate([]) is None


def test_float_rate_guards_zero_denominator() -> None:
    assert metrics.float_rate(0, 0) is None
    assert metrics.float_rate(3, 4) == 0.75


def test_mean_and_normalize() -> None:
    assert metrics.mean([1, 2, 3]) == 2.0
    assert metrics.mean([]) is None
    assert metrics.normalize("  Nature.COM \n") == "nature.com"
