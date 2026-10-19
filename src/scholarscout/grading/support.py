"""引用语义支持度校验：**这条资料是否真的支持这句话**。

## 为什么需要它（第 5 章的已知局限）

现有的反幻觉守卫只校验**引用 id 的真实性**（存在、属于本次允许集合、不越权），
所以"出处覆盖率 100%"只能读作**可归属率**，不能读作"资料真的支持该说法"。
本模块补上后半句，并把它做成**可复算、可累积、可人工复核**的检查。

## 三条设计约束（都直接来自踩过的坑）

1. **证据分两层，必须分开报**：抓过原文的来源有摘要与要点（强证据），没抓过的只有标题、
   年份、域名（弱证据）。混在一起算"支持率"会被**标题字面重合**污染 ——
   所以结果按 `evidence_level` 分开统计（`summary` / `title`）。
2. **证据不足就判不支持，不许猜**：提示词写死了这条，`unknown` 只留给"模型输出不可解析"，
   且**不计入分子分母**（宁可样本变少，也不把解析失败当成通过）。
3. **结果落库**（`support_checks`）：报告可复算、可累积，重复审同一批产物**不重复计费**；
   弱支持清单直接给人工复核。

## 与"出处覆盖率"的关系

覆盖率回答"有没有挂上真实来源"，本模块回答"挂上的来源是否支持这句话"。
两者的分母口径一致：**只算资料类条目**（`paper/doc/video/code`），行动类（`exercise`）不声称出处。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.support import upsert_check

#: 判定的三种结果（写进库、进报告）
VERDICTS = ("supported", "partial", "unsupported")
#: 解析失败时的兜底值：**不计入分子分母**
UNKNOWN = "unknown"

#: 只审这些条目类型（与反幻觉口径一致：行动类不声称出处）
RESOURCE_ITEM_KINDS = ("paper", "doc", "video", "code")

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "verify-support.md"


def load_prompt(path: Path | None = None) -> str:
    return (path or PROMPT_PATH).read_text(encoding="utf-8")


def prompt_version(prompt: str) -> str:
    """提示词版本＝内容哈希（与摘要流水线同一套约定：改了提示词，历史结果就不可比）。"""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class SupportPair:
    """一条待审的（断言, 证据）对。"""

    artifact_kind: str  # "topic_map" | "route"
    artifact_id: str
    item_id: str
    claim: str
    source_ids: tuple[str, ...]
    evidence_level: str  # "summary"（有摘要＝强证据）| "title"（仅标题＝弱证据）
    evidence_text: str


@dataclass(frozen=True)
class SupportVerdict:
    verdict: str
    reason: str
    model: str = ""
    prompt_version: str = ""


# --------------------------------------------------------------- 证据与配对


def _source_evidence(db: Database, source_id: str) -> tuple[str, str]:
    """取一条来源能拿到的证据，返回 ``(等级, 文本)``。"""
    row = db.query_one(
        "SELECT id, title, url, year, source_type FROM sources WHERE id = ?", (source_id,)
    )
    if row is None:
        return "title", f"（库中没有这条来源：{source_id}）"
    item = dict(row)
    head = f"标题：{item.get('title') or '（无标题）'}"
    meta = "，".join(
        str(part)
        for part in (
            f"{item.get('year')} 年" if item.get("year") else "",
            item.get("source_type") or "",
            (item.get("url") or "")[:80],
        )
        if part
    )
    summary = db.query_one(
        "SELECT title_zh, summary_short, points_json FROM summaries"
        " WHERE source_id = ? AND coalesce(summary_short,'') != ''"
        " ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    )
    if summary is None:
        return "title", f"{head}\n{meta}"
    data = dict(summary)
    points = data.get("points_json")
    try:
        point_list = json.loads(points) if points else []
    except json.JSONDecodeError:
        point_list = []
    lines = [
        head,
        meta,
        f"中文标题：{data.get('title_zh') or '（无）'}",
        f"摘要：{data.get('summary_short')}",
    ]
    if point_list:
        lines.append("要点：" + "；".join(str(point) for point in point_list))
    return "summary", "\n".join(line for line in lines if line)


def build_pairs(
    db: Database,
    artifact_kind: str,
    artifact_id: str,
    *,
    max_sources_per_item: int = 2,
) -> list[SupportPair]:
    """把一件产物拆成待审对。只审资料类条目；一个条目最多带 `max_sources_per_item` 条证据。"""
    if artifact_kind == "topic_map":
        row = db.query_one("SELECT id, graph_json FROM topic_maps WHERE id = ?", (artifact_id,))
        if row is None:
            raise KeyError(artifact_id)
        graph = json.loads(str(row["graph_json"]))
        pairs: list[SupportPair] = []
        for node in graph.get("nodes") or []:
            source_ids = [str(item) for item in (node.get("source_ids") or [])]
            if not source_ids:
                continue  # 没挂来源的节点属于"ungrounded"，由覆盖率指标管，这里不重复算
            pairs.append(
                _pair(
                    "topic_map",
                    artifact_id,
                    str(node.get("nid") or "?"),
                    str(node.get("label") or ""),
                    source_ids,
                    db,
                    max_sources_per_item,
                )
            )
        return pairs

    if artifact_kind == "route":
        row = db.query_one("SELECT id, plan_json FROM learning_routes WHERE id = ?", (artifact_id,))
        if row is None:
            raise KeyError(artifact_id)
        plan = json.loads(str(row["plan_json"]))
        pairs = []
        for week_index, week in enumerate(plan.get("weeks") or [], start=1):
            # 周号字段是 `idx`（不是 `week`）；取不到就用序号兜底 —— 编号里绝不能出现 "?"
            week_no = week.get("idx") or week_index
            for index, item in enumerate(week.get("items") or []):
                kind = str(item.get("kind") or "")
                source_id = item.get("source_id")
                if kind not in RESOURCE_ITEM_KINDS or not source_id:
                    continue  # 行动类不声称出处（与 ungrounded 口径一致）
                claim = str(item.get("title") or "")
                why = str(item.get("why") or "")
                pairs.append(
                    _pair(
                        "route",
                        artifact_id,
                        f"w{week_no}-{index}",
                        f"{claim}——{why}" if why else claim,
                        [str(source_id)],
                        db,
                        max_sources_per_item,
                    )
                )
        return pairs

    raise ValueError(f"不认识的产物类型：{artifact_kind}")


def _pair(
    artifact_kind: str,
    artifact_id: str,
    item_id: str,
    claim: str,
    source_ids: list[str],
    db: Database,
    limit: int,
) -> SupportPair:
    picked = source_ids[: max(1, limit)]
    blocks: list[str] = []
    level = "title"
    for source_id in picked:
        source_level, text = _source_evidence(db, source_id)
        if source_level == "summary":
            level = "summary"
        blocks.append(f"[{source_id}]\n{text}")
    return SupportPair(
        artifact_kind=artifact_kind,
        artifact_id=artifact_id,
        item_id=item_id,
        claim=claim,
        source_ids=tuple(picked),
        evidence_level=level,
        evidence_text="\n\n".join(blocks),
    )


# ------------------------------------------------------------------- 判定


def build_messages(pair: SupportPair, prompt: str) -> list[dict[str, Any]]:
    user = (
        f"断言：{pair.claim}\n\n"
        f"证据等级：{'强（含摘要与要点）' if pair.evidence_level == 'summary' else '弱（仅标题与元数据）'}\n\n"
        f"资料：\n{pair.evidence_text}"
    )
    return [{"role": "system", "content": prompt}, {"role": "user", "content": user}]


_JSON_RE = re.compile(r"\{.*?\}", re.S)


def parse_verdict(text: str) -> tuple[str, str]:
    """从模型输出里取 (verdict, reason)。**解析不出来就是 unknown**，绝不默认成 supported。"""
    body = str(text or "").strip()
    candidates = [body]
    candidates.extend(match.group(0) for match in _JSON_RE.finditer(body))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict in VERDICTS:
            return verdict, str(data.get("reason") or "")[:200]
    lowered = body.lower()
    # **必须从长到短匹配**：`"supported"` 是 `"unsupported"` 的子串，
    # 按声明顺序匹配会把"不支持"读成"支持" —— 那是直接让指标虚高的错法（用例钉住）。
    for verdict in sorted(VERDICTS, key=len, reverse=True):
        if verdict in lowered:
            return verdict, body[:200]
    return UNKNOWN, body[:200]


def judge(client: Any, pair: SupportPair, prompt: str, *, version: str = "") -> SupportVerdict:
    """让模型判一条。**客户端报错时抛出去**，由调用方决定是否继续（不静默当成通过）。"""
    reply = client.chat(build_messages(pair, prompt))
    verdict, reason = parse_verdict(getattr(reply, "content", "") or "")
    return SupportVerdict(
        verdict=verdict,
        reason=reason,
        model=str(getattr(reply, "model", "") or ""),
        prompt_version=version,
    )


# ------------------------------------------------------------------- 落库


def record_check(
    db: Database, pair: SupportPair, result: SupportVerdict, *, session_id: str | None = None
) -> str:
    """把判定写进 `support_checks`（可复算、可累积、不重复计费）。"""
    check_id = f"sup_{hashlib.sha256((pair.artifact_id + pair.item_id + pair.evidence_text).encode()).hexdigest()[:12]}"
    upsert_check(
        db,
        check_id=check_id,
        artifact_kind=pair.artifact_kind,
        artifact_id=pair.artifact_id,
        session_id=session_id,
        item_id=pair.item_id,
        claim=pair.claim,
        source_ids=pair.source_ids,
        evidence_level=pair.evidence_level,
        verdict=result.verdict,
        reason=result.reason,
        model=result.model,
        prompt_version=result.prompt_version,
        created_at=_now(),
    )
    return check_id


def _now() -> str:
    from scholarscout.state.migrate import now_iso

    return now_iso()


def list_checks(db: Database, artifact_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM support_checks WHERE artifact_id = ? ORDER BY item_id", (artifact_id,)
    )
    return [dict(row) for row in rows]


# ------------------------------------------------------------------- 汇总


def summarise(pairs: Iterable[tuple[SupportPair, SupportVerdict]]) -> dict[str, Any]:
    """按证据等级分层汇总。`unknown` 不计入分子分母（解析失败不能当成通过）。"""
    counts: dict[str, int] = {verdict: 0 for verdict in VERDICTS}
    unknown = 0
    by_level: dict[str, dict[str, int]] = {"summary": dict(counts), "title": dict(counts)}
    weak: list[dict[str, Any]] = []
    total = 0
    for pair, result in pairs:
        total += 1
        if result.verdict == UNKNOWN:
            unknown += 1
        else:
            counts[result.verdict] += 1
            level = "summary" if pair.evidence_level == "summary" else "title"
            by_level[level][result.verdict] += 1
        if result.verdict in {"partial", "unsupported"}:
            weak.append(
                {
                    "item_id": pair.item_id,
                    "claim": pair.claim,
                    "source_ids": list(pair.source_ids),
                    "evidence_level": pair.evidence_level,
                    "verdict": result.verdict,
                    "reason": result.reason,
                }
            )
    checked = sum(counts.values())

    def rate(level_counts: dict[str, int], *, include_partial: bool = False) -> float | None:
        denominator = sum(level_counts.values())
        if denominator <= 0:
            return None
        numerator = level_counts["supported"] + (level_counts["partial"] if include_partial else 0)
        return numerator / denominator

    return {
        "pairs": total,
        "checked": checked,
        "unknown": unknown,
        "counts": counts,
        "supported_rate": rate(counts),
        "supported_or_partial_rate": rate(counts, include_partial=True),
        "by_evidence_level": {
            level: {
                "checked": sum(level_counts.values()),
                "counts": level_counts,
                "supported_rate": rate(level_counts),
                "supported_or_partial_rate": rate(level_counts, include_partial=True),
            }
            for level, level_counts in by_level.items()
        },
        "weak_cases": weak,
    }
