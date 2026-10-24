"""对话过程评分：工具调用轨迹的指标纯函数。

口径归属：本模块**不自定口径**，只实现两个外部权威来源的定义 + 一个国内项目既有约定：

1) RAGAS 的 *Agents or Tool use* 指标组（https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/agents/）
   - ``ToolCallAccuracy``：二值分，实际调用序列与 ``reference_tool_calls`` **顺序与内容完全一致** 才得 1；
     允许 ``strict_order=False``（并行调用的场景，顺序不计）。
   - ``ToolCallF1``：基于 precision / recall 的**软分**，容忍多调（over-call）与少调（under-call）。
   - ``AgentGoalAccuracy``：二值分，是否识别并达成用户目标。
   - ``TopicAdherence``：precision / recall / F1 三件套（公式照抄文档，见下 ``topic_adherence``）。

2) TRAJECT-Bench（arXiv:2510.04550）的轨迹级诊断指标
   - **Trajectory Exact-Match**：整条轨迹与 gold 完全一致。
   - **Trajectory Inclusion**：必需工具**都被调用且相对顺序正确**。
   - **Tool-Usage**：工具输入的 **schema 约束 / 格式 / 参数值** 校验。

3) τ-bench（arXiv:2406.12045）的 ``pass^k``
   - ``pass^k`` = **k 次独立试验全部成功**的概率，跨任务平均；``pass^1`` = 平均奖励。
   - 与 ``pass@k``（至少一次成功）相对：前者量**可靠性/一致性**，后者量**能力上限**。

本模块沿用的项目既有约定（``eval/README.md`` §2）：
**分母为 0 返回 ``None`` 而不是 ``0``** —— "没有样本"与"全错"是两件事。
纯函数、不碰库、不读文件（与 ``eval/metrics.py`` 同风格）。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "TRAJ_FETCH_K",
    "TRAJ_STAGE_BUDGET",
    "arg_valid",
    "goal_accuracy",
    "is_retrieval_turn",
    "normalize_call",
    "pass_hat_k",
    "tool_call_accuracy",
    "tool_call_accuracy_traj",
    "tool_call_f1",
    "tool_usage_rate",
    "topic_adherence",
    "traj_turn_excess",
    "trajectory_exact_match",
    "trajectory_inclusion",
]

# --------------------------------------------------------------------------- 轨迹口径（第二把尺子）

#: 轨迹口径的抓取上限 K。**为什么是 3**：E4 的六道题面里，一次「找资料」最多要求覆盖
#: 2–3 个不同来源（``eval/sets/e4_dialogues.jsonl`` 的 gold 与 artifact_min 都按这个量级写），
#: 3 次抓取足够「检索 → 判权 → 抓到可用全文」这条链路走完；取 5 或 10 会让口径退化成
#: 「几乎不判冗余」，取 1 又把正常的多来源比较判成冗余。**这是预先定死的常量，不按模型表现调。**
TRAJ_FETCH_K: int = 3

#: 轨迹口径的每轮阶段预算：1 次 ``research.search`` + 1 次 ``source.grade`` + 最多 ``K`` 次 ``source.fetch``。
#: 三个工具名之外的一律算「非检索工具」，仍按 gold 逐名比对（不在本口径里被放宽）。
TRAJ_STAGE_BUDGET: dict[str, int] = {
    "research.search": 1,
    "source.grade": 1,
    "source.fetch": TRAJ_FETCH_K,
}

#: 每个工具的必填参数（口径来源：``src/scholarscout/tools/*.py`` 的 ``PARAMETERS.required``）
REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "research.search": ("query",),
    "source.fetch": (),  # url 与 source_id 二选一
    "source.grade": (),
    "topic.build_map": ("topic",),
    "route.plan": ("topic",),
    "route.revise": ("route_id", "instruction"),
    "note.save": (),
    "research.delegate": ("goals",),
}

#: 每个工具的枚举型参数（值必须在集合内）—— 同样来自 ``PARAMETERS``
ENUM_ARGS: dict[str, dict[str, tuple[Any, ...]]] = {
    "route.plan": {
        "level": ("入门", "进阶", "研究"),
    },
    "note.save": {"kind": ("summary", "note", "card")},
    "research.search": {"sort": ("relevance", "date")},
}

#: 数值型参数的闭区间（``PARAMETERS`` 的 minimum / maximum）
RANGE_ARGS: dict[str, dict[str, tuple[float, float]]] = {
    "route.plan": {"weeks": (1, 24), "weekly_hours": (1, 40)},
    "topic.build_map": {"max_nodes": (3, 30)},
    "research.search": {"limit": (1, 50)},
    "source.grade": {"limit": (1, 100)},
}


def _as_mapping(raw: Any) -> dict[str, Any]:
    """把"落库后形态不定"的参数还原成 dict，**绝不因为形态奇怪就丢记录**。

    `tool_traces.args_json` 可能是 dict、JSON 字符串、键值对序列，或异常形态；
    这里统一收口，收不了的记成 ``{"_raw": ...}``（后续会被判为参数不合法，而不是静默跳过）。
    """
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("{"):
            try:
                j = json.loads(s)
                if isinstance(j, Mapping):
                    return dict(j)
            except (json.JSONDecodeError, ValueError):
                pass
        return {"_raw": s}
    if isinstance(raw, (list, tuple)):
        try:
            return dict(raw)
        except (TypeError, ValueError):
            return {"_raw": str(raw)}
    if raw is None:
        return {}
    return {"_raw": str(raw)}


def normalize_call(call: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
    """把一次工具调用归一成可比较的键 ``(name, ((k, v), ...))``。

    值统一转字符串并去空白，便于 ``{"weeks": 4}`` 与 ``{"weeks": "4"}`` 视为等价
    （口径：类型差异不算错，**值**差异才算）。
    """
    name = str(call.get("name") or call.get("tool") or "").strip()
    items = tuple(
        sorted(
            (str(k), str(v).strip())
            for k, v in _as_mapping(call.get("args") or call.get("arguments")).items()
        )
    )
    return name, items


# --------------------------------------------------------------------------- RAGAS: 工具调用准确率 / F1


def _key(call: Mapping[str, Any], match: str) -> tuple[Any, ...]:
    """按 ``match`` 口径取比较键：``both`` = 工具名 + 参数；``name`` = 只看工具名。"""
    name, items = normalize_call(call)
    return (name, items) if match == "both" else (name,)


def tool_call_accuracy(
    actual: Sequence[Mapping[str, Any]],
    reference: Sequence[Mapping[str, Any]],
    *,
    strict_order: bool = True,
    match: str = "both",
) -> float | None:
    """RAGAS ``ToolCallAccuracy``：二值 1.0 / 0.0（无参考 → None）。

    ``strict_order=True``（默认）：顺序与内容都要完全一致；
    ``strict_order=False``：只比较**调用集合**（RAGAS 的 "Flexible Order Mode"，用于并行调用）。
    ``match="both"``（默认）：工具名 + 参数都要对上；``match="name"``：**只看工具选择**、不看参数。
    """
    if not reference:
        return None
    act = [_key(c, match) for c in actual]
    ref = [_key(c, match) for c in reference]
    if strict_order:
        return 1.0 if act == ref else 0.0
    return 1.0 if sorted(act) == sorted(ref) else 0.0


def tool_call_f1(
    actual: Sequence[Mapping[str, Any]],
    reference: Sequence[Mapping[str, Any]],
    *,
    match: str = "both",
) -> float | None:
    """RAGAS ``ToolCallF1``：precision / recall 之 F1（无参考 → None）。

    ⚠️ **口径说明**：RAGAS 官方 ``ToolCallF1`` 是**由 LLM 判**"两次调用算不算同一次"；
    本实现是 **code-based 近似** —— ``match="both"`` 要求工具名与参数**逐字相等**
    （严格、可复算、但会把"同一个工具换了检索词"判为不匹配），
    ``match="name"`` 只看工具选择（更接近 F1 想表达的"离目标有多近"）。
    两者都报，读者自己选口径；**不要把它们与官方 LLM 判的数字混着比**
    （见 arXiv:2507.21504 §4.3 对 code-based 与 LLM-as-judge 的区分）。
    """
    if not reference:
        return None
    act = [_key(c, match) for c in actual]
    ref = [_key(c, match) for c in reference]
    if not act and not ref:
        return 1.0
    if not act or not ref:
        return 0.0
    # 多重集合交（同一调用重复出现要计次）
    ca, cr = Counter(act), Counter(ref)
    overlap = sum((ca & cr).values())
    precision = overlap / len(act)
    recall = overlap / len(ref)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# --------------------------------------------------------------------------- TRAJECT-Bench: Tool-Usage（参数校验）


def arg_valid(call: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """TRAJECT-Bench ``Tool-Usage`` 的单次调用版：schema 必填 + 枚举 + 区间三项检查。

    返回 ``(是否合法, 问题清单)``。未知工具名不判死（只记一条问题），
    因为本函数只负责"参数层"，工具选择对不对由 ``tool_call_*`` 负责。
    """
    name, items = normalize_call(call)
    d = dict(items)
    problems: list[str] = []

    if name not in REQUIRED_ARGS:
        return True, [f"未知工具名（不参与参数校验）：{name}"]

    for key in REQUIRED_ARGS[name]:
        if key not in d or d[key] == "":
            problems.append(f"缺必填参数 {key}")

    # source.fetch 是二选一：url 或 source_id 至少给一个
    if name == "source.fetch" and not (d.get("url") or d.get("source_id")):
        problems.append("url 与 source_id 必须给一个")

    for key, allowed in ENUM_ARGS.get(name, {}).items():
        if key in d and d[key] not in [str(x) for x in allowed]:
            problems.append(f"{key}={d[key]!r} 不在枚举 {list(allowed)} 内")

    for key, (lo, hi) in RANGE_ARGS.get(name, {}).items():
        if key in d:
            try:
                v = float(d[key])
            except (TypeError, ValueError):
                problems.append(f"{key}={d[key]!r} 不是数值")
                continue
            if not (lo <= v <= hi):
                problems.append(f"{key}={d[key]} 超出区间 [{lo}, {hi}]")

    return (not problems), problems


def tool_usage_rate(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """TRAJECT-Bench ``Tool-Usage`` 的批次版：参数合法的调用占全部调用的比例（无调用 → None）。

    每条 ``row`` 形如 ``{"name": ..., "args": {...}}``。
    """
    calls = [r for r in rows if (r.get("name") or r.get("tool"))]
    if not calls:
        return None
    good = sum(1 for r in calls if arg_valid(r)[0])
    return good / len(calls)


# --------------------------------------------------------------------------- TRAJECT-Bench: 轨迹级


def trajectory_exact_match(
    actual_seq: Sequence[Mapping[str, Any]],
    reference_seq: Sequence[Mapping[str, Any]],
) -> bool:
    """TRAJECT-Bench ``Trajectory Exact-Match``：整条轨迹（展平后）与 gold 完全一致。

    与 ``tool_call_accuracy(strict_order=True)`` 的关系：逐轮都过 → 整条才过，
    所以本函数也可由"所有轮 TCA 均为 1"推出；此处独立实现以便对拍。
    """
    act = [normalize_call(c) for c in actual_seq]
    ref = [normalize_call(c) for c in reference_seq]
    return act == ref


def _lcs_len(a: Sequence[str], b: Sequence[str]) -> int:
    """最长公共子序列长度（动态规划，O(len(a)·len(b))）。

    为什么用 LCS 而不是贪心匹配：贪心在「必需顺序与实际顺序部分相反」时**依赖扫描方向**，
    同一对输入会因实现细节给出不同结果；LCS 对同一对输入只有唯一答案，可复核。
    """
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def trajectory_inclusion(
    actual_seq: Sequence[Mapping[str, Any]],
    required_seq: Sequence[Mapping[str, Any]],
) -> float | None:
    """TRAJECT-Bench ``Trajectory Inclusion``：必需工具**都被调用且相对顺序正确**。

    实现：**按工具名**求 ``required`` 与 ``actual`` 的**最长公共子序列长度 / 必需项数**。
    - 返回 1.0 ⟺ 必需序列是实际序列的**子序列**（即"都被调用且顺序正确"，这才是 TRAJECT-Bench 的原意）；
      所以布尔形式就是 ``== 1.0``。
    - 中间值表示"顺序部分被满足"，用于诊断而不是判死。

    **只看工具名，不看参数** —— 参数正确性由 ``tool_usage_rate`` 单独报，两者不混
    （TRAJECT-Bench 原文把 Tool-Usage 与 trajectory 指标分开正是这个理由）。
    ``required_seq`` 为空 → None。
    """
    if not required_seq:
        return None
    need = [normalize_call(c)[0] for c in required_seq]
    have = [normalize_call(c)[0] for c in actual_seq]
    return _lcs_len(need, have) / len(need)


# --------------------------------------------------------------------------- τ-bench: pass^k 可靠性


def pass_hat_k(successes: int, trials: int, k: int) -> float | None:
    """τ-bench 的 ``pass^k`` 无偏估计：``C(c, k) / C(n, k)``。

    含义：**k 次独立试验全部成功**的概率。与 ``pass@k = 1 - C(n-c, k)/C(n, k)``
    （至少一次成功）相对 —— 前者量可靠性，后者量能力上限。
    ``n < k`` → None（试验次数不够，无法估计）；``k <= 0`` → None。
    """
    if k <= 0 or trials < k or successes > trials or successes < 0:
        return None
    return math.comb(successes, k) / math.comb(trials, k)


def pass_at_k(successes: int, trials: int, k: int) -> float | None:
    """τ-bench 同时给出的 ``pass@k``：至少一次成功（作为 pass^k 的对照）。"""
    if k <= 0 or trials < k or successes > trials or successes < 0:
        return None
    return 1 - math.comb(trials - successes, k) / math.comb(trials, k)


# --------------------------------------------------------------------------- RAGAS: 主题黏着 / 目标达成


def topic_adherence(
    answered_in_domain: int,
    answered_out_domain: int,
    refused_should_answer: int,
) -> dict[str, float | None]:
    """RAGAS ``TopicAdherence`` 的 precision / recall / F1（公式照抄官方文档）。

    - precision = 在域内作答 / （在域内作答 + 在域外作答）—— "答了的是不是都跟主题有关"
    - recall    = 在域内作答 / （在域内作答 + 本该答却拒答）—— "该答的是不是都答了"
    - F1        = 2PR / (P + R)
    任一分母为 0 → 该项 ``None``（沿用项目约定，不报 0）。
    """
    p = None
    if (answered_in_domain + answered_out_domain) > 0:
        p = answered_in_domain / (answered_in_domain + answered_out_domain)
    r = None
    if (answered_in_domain + refused_should_answer) > 0:
        r = answered_in_domain / (answered_in_domain + refused_should_answer)
    f1 = None
    if p is not None and r is not None and (p + r) > 0:
        f1 = 2 * p * r / (p + r)
    return {"precision": p, "recall": r, "f1": f1}


def goal_accuracy(judgements: Iterable[bool]) -> float | None:
    """RAGAS ``AgentGoalAccuracy`` 的批次版：达成目标的对话占比（空 → None）。

    单条判定是二值的（1 = 达成 / 0 = 未达成）；判定手段可以是 code-based
    （有确定性验收条件时）或 LLM-as-judge（无 gold 轨迹时），二者都要在报告里写明。
    """
    items = list(judgements)
    if not items:
        return None
    return sum(1 for x in items if x) / len(items)


# --------------------------------------------------------------------------- 第二把尺子：最小充分轨迹


def is_retrieval_turn(actual: Sequence[Mapping[str, Any]]) -> bool:
    """这一轮**实际**是否做了检索/判权/抓取（三个阶段任一被调用 → True）。

    用途：轨迹口径只接管这类轮；其余轮（gold 本来就不是检索类，如 ``note.save``/``route.plan``）
    两个口径下**都按原 gold 算**，不改写。
    """
    return any(normalize_call(c)[0] in TRAJ_STAGE_BUDGET for c in actual)


def _stage_excess(counts: Counter[str]) -> int:
    """超出阶段预算的部分（只算**多出来的**，预算内的调用不算冗余）。"""
    return sum(max(0, counts[name] - budget) for name, budget in TRAJ_STAGE_BUDGET.items())


def _non_retrieval_counts(calls: Sequence[Mapping[str, Any]]) -> Counter[str]:
    """非检索工具的调用计数（按工具名）。"""
    return Counter(
        normalize_call(c)[0] for c in calls if normalize_call(c)[0] not in TRAJ_STAGE_BUDGET
    )


def traj_turn_excess(
    actual: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
) -> int:
    """轨迹口径的**单轮冗余**：按「最小充分轨迹」为基准，而不是按 gold 的 1 次调用。

    机械规则（对所有对话完全相同，**不看模型实际行为来调参数**）：

    * 检索类轮（实际调过 search/grade/fetch 任一）：基准 = 1 次 ``research.search``
      + 1 次 ``source.grade`` + 最多 ``TRAJ_FETCH_K`` 次 ``source.fetch``；
      冗余 = 各阶段**超出预算**的调用数之和 + 非检索工具超出 gold 的部分。
      即：预算内的检索/判权/抓取**一律不算冗余**（gold 只标了每轮第一步，不该拿它当上限）。
    * 非检索类轮：与原口径一致 —— ``max(0, 实际 − gold)``（gold 在这里是完整标注）。

    为什么仍然不是 0：多出来的搜索（同一轮搜 4 次）、抓取超过 K 次，仍是真冗余；
    这条口径只免掉「设计上就该做的那几步」。
    """
    counts = Counter(normalize_call(c)[0] for c in actual)
    if not is_retrieval_turn(actual):
        return max(0, len(actual) - len(gold))
    other_actual = _non_retrieval_counts(actual)
    other_gold = _non_retrieval_counts(gold)
    other_excess = max(0, sum(other_actual.values()) - sum(other_gold.values()))
    return _stage_excess(counts) + other_excess


def tool_call_accuracy_traj(
    actual: Sequence[Mapping[str, Any]],
    gold: Sequence[Mapping[str, Any]],
) -> float | None:
    """轨迹口径的 ``ToolCallAccuracy``（**名字级**）：这一轮是否落在最小充分轨迹内。

    检索类轮判 1.0 需同时满足三条：
    ① 各阶段不超预算（search ≤ 1、grade ≤ 1、fetch ≤ ``TRAJ_FETCH_K``）；
    ② gold 点名的检索阶段都至少调了一次（gold 只标第一步，标到的必须做）；
    ③ 非检索工具与 gold **逐名对上**（一个不少、也没多）—— 工具选错不会被本口径放过。

    非检索类轮：直接沿用原口径的名字级 TCA（``tool_call_accuracy(..., match="name")``）。

    与 ``tool_call_accuracy`` 的差别只在**检索类轮的基准**：原口径要求「与 gold 逐项完全相同」，
    而 gold 只有 1 次 ``research.search``，所以只要系统真去抓了资料就必然判 0；
    本口径量的是「有没有做成一趟最小充分轨迹」，两把尺子量的不是同一件事。
    """
    if not gold:
        return None
    if not is_retrieval_turn(actual):
        return tool_call_accuracy(actual, gold, match="name")
    counts = Counter(normalize_call(c)[0] for c in actual)
    gold_counts = Counter(normalize_call(c)[0] for c in gold)
    for name, budget in TRAJ_STAGE_BUDGET.items():
        if counts[name] > budget:
            return 0.0
        if gold_counts[name] and not counts[name]:
            return 0.0
    if _non_retrieval_counts(actual) != _non_retrieval_counts(gold):
        return 0.0
    return 1.0
