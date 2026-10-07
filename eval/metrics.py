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


def distinct_sources(graph: Mapping[str, Any] | None, plan: Mapping[str, Any] | None) -> int:
    """一件产物里用到的**不同来源**个数（地图节点引用 + 路线资料类条目的 source_id）。

    为什么必须和"出处覆盖率"一起看：覆盖率只校验"每条都挂着一个存在的 id"，
    如果模型把每条都挂同一个 id，覆盖率照样 100% 却毫无价值。真机抽样验证过
    （e3-04：14 节点 30 次引用 8 个来源，路线 7 条 7 个来源），这个数就是那种退化的照妖镜。
    """
    refs: set[str] = set()
    for node in (graph or {}).get("nodes") or []:
        refs.update(str(ref) for ref in (node.get("source_ids") or []) if ref)
    for week in (plan or {}).get("weeks") or []:
        for item in week.get("items") or []:
            source_id = item.get("source_id")
            if source_id:
                refs.add(str(source_id))
    return len(refs)


def kind_mix(kinds: Iterable[str]) -> dict[str, int]:
    """来源类型分布（按条数降序；空值归 "?"，**排在最后**，与年份分布的"未知年份"一致）。"""
    counts = Counter(str(kind or "?") for kind in kinds)
    return {
        key: count
        for key, count in sorted(counts.items(), key=lambda kv: (kv[0] == "?", -kv[1], kv[0]))
    }


def year_mix(
    years: Iterable[int | None], *, now_year: int, window_years: int = 2
) -> dict[str, int]:
    """年份分布（人看的三个桶 + 未知）。

    **未知年份必须单列**：把"取不到年份"塞进任何一桶都是编数据。
    `now_year` 显式传入（而不是在函数里读今天）——否则同一个数据集在不同日子跑出不同结论。
    """
    recent = f"近 {window_years} 年（≥{now_year - window_years + 1}）"
    middle = f"前 {window_years} 年（{now_year - 2 * window_years + 1}–{now_year - window_years}）"
    older = f"更早（≤{now_year - 2 * window_years}）"
    mix = {recent: 0, middle: 0, older: 0, "未知年份": 0}
    for year in years:
        if year is None:
            mix["未知年份"] += 1
        elif int(year) >= now_year - window_years + 1:
            mix[recent] += 1
        elif int(year) >= now_year - 2 * window_years + 1:
            mix[middle] += 1
        else:
            mix[older] += 1
    return mix


def fresh_share(
    years: Iterable[int | None], *, now_year: int, window_years: int = 2
) -> float | None:
    """近 N 年占比 = 近 N 年条数 / **有年份的条数**（未知年份不进分母，单独看）。"""
    known = [int(year) for year in years if year is not None]
    if not known:
        return None
    fresh = sum(1 for year in known if year >= now_year - window_years + 1)
    return fresh / len(known)


def sum_mixes(mixes: Sequence[Mapping[str, int]]) -> dict[str, int]:
    """按桶标签合并多份分布（各题的年份桶标签必须一致，否则合并会静默错位）。"""
    total: Counter[str] = Counter()
    labels: set[tuple[str, ...]] = set()
    for mix in mixes:
        labels.add(tuple(mix.keys()))
        total.update(mix)
    if len(labels) > 1:
        raise ValueError("桶标签不一致，合并会错位 —— 检查 `now_year` 是否同一个")
    return dict(total)


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
