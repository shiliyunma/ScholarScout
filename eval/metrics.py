"""指标计算：纯函数，不碰数据库、不读文件 —— 口径错会让论文里每个数字都错。

约定（与 docs/05 §3 一一对应）：
- 分母为 0 时返回 ``None`` 而不是 0.0 —— "没有样本"和"全错"是两件事，报告里要分开显示
- 分位数用**最近秩法**（nearest-rank），与文档口径一致
- 一致性同时给"一致率"与 Cohen's κ（一致率高可能只是分布偏斜）
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def agreement(pairs: Sequence[tuple[str, str]]) -> float | None:
    """一致率 = 相同的对数 / 总对数。"""
    if not pairs:
        return None
    hits = sum(1 for got, want in pairs if got == want)
    return hits / len(pairs)


def confusion(pairs: Sequence[tuple[str, str]], labels: Sequence[str]) -> dict[str, dict[str, int]]:
    """混淆矩阵：``matrix[期望][实际]``。"""
    matrix = {want: dict.fromkeys(labels, 0) for want in labels}
    for got, want in pairs:
        if want in matrix:
            matrix[want][got] = matrix[want].get(got, 0) + 1
    return matrix


def cohens_kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    """Cohen's κ = (Po - Pe) / (1 - Pe)；Pe 取双方边缘分布。"""
    if not pairs:
        return None
    total = len(pairs)
    observed = sum(1 for got, want in pairs if got == want) / total
    got_counts = Counter(got for got, _ in pairs)
    want_counts = Counter(want for _, want in pairs)
    expected = sum(
        (got_counts[label] / total) * (want_counts[label] / total)
        for label in set(got_counts) | set(want_counts)
    )
    if math.isclose(expected, 1.0):
        # 双方都只有一个类别：κ 无定义（分母为 0），如实返回 None
        return None
    return (observed - expected) / (1 - expected)


def percentile(values: Iterable[float], p: float) -> float | None:
    """最近秩分位数（nearest-rank）：``ceil(p/100 × n)`` 个。

    ``p=50`` 即中位数。空输入返回 ``None``。
    """
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def success_rate(rows: Sequence[Mapping[str, Any]], *, key: str = "ok") -> float | None:
    """按 ``key`` 判成功（1/True 记成功）。分母含失败行 —— 失败也要计入。"""
    if not rows:
        return None
    hits = sum(1 for row in rows if bool(row.get(key)))
    return hits / len(rows)


def float_rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def mean(values: Iterable[float]) -> float | None:
    items = [float(v) for v in values]
    if not items:
        return None
    return sum(items) / len(items)


def normalize(text: str) -> str:
    """比较用归一化：去空白、统一小写（域名与标题比对时都走它）。"""
    return "".join(str(text).split()).lower()
