"""来源分层：**规则表驱动**，不由模型自由心证（docs/02 §3、决议）。

- 规则存在 ``source_tier_rules`` 表里（种子在迁移 0002），可查、可改、可解释
- ``authority`` = 该 tier 的基准分 × 规则权重，卡到 0–5
- 相关性**刻意不使用 tier**（照 AIHOT 的做法：分级只用来调门槛，不让"来源名气"污染打分）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.tier_rules import enabled_rules

#: tier 语义（写进论文第 3 章）
TIER_LABELS: dict[int, str] = {
    1: "一手权威",
    2: "学术索引与同行评议",
    3: "社区与教程",
    4: "未知或低质",
}
#: 各 tier 的权威性基准分
TIER_BASE_AUTHORITY: dict[int, float] = {1: 5.0, 2: 4.0, 3: 2.5, 4: 1.0}
#: 匹配优先级：越靠前越优先（url_regex 最具体）
MATCH_PRIORITY: tuple[str, ...] = ("url_regex", "domain", "domain_suffix", "source_type")

MATCH_TYPES = MATCH_PRIORITY


@dataclass(frozen=True)
class TierRule:
    id: int
    match_type: str
    pattern: str
    tier: int
    weight: float = 1.0
    note: str | None = None

    def matches(self, *, url: str, domain: str, source_type: str) -> bool:
        pattern = self.pattern
        if self.match_type == "domain":
            # 域名规则**同时覆盖子域**：规则写 `zhihu.com` 要能命中 `zhuanlan.zhihu.com`、
            # 写 `nature.com` 要能命中 `www.nature.com` —— 否则绝大多数真实链接会漏判成 tier 4
            # （E1 评测集跑出来的第一条真问题，见 docs/05 §3.1）。
            lowered = pattern.lower()
            return domain == lowered or domain.endswith("." + lowered)
        if self.match_type == "domain_suffix":
            return domain.endswith(pattern.lower())
        if self.match_type == "source_type":
            return source_type == pattern.lower()
        if self.match_type == "url_regex":
            try:
                return re.search(pattern, url, re.IGNORECASE) is not None
            except re.error:
                return False
        return False


@dataclass(frozen=True)
class TierVerdict:
    tier: int
    tier_label: str
    authority: float
    matched_rule_id: int | None = None
    matched_pattern: str | None = None
    signals: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "tier_label": self.tier_label,
            "authority": self.authority,
            "matched_rule_id": self.matched_rule_id,
            "matched_pattern": self.matched_pattern,
            "signals": self.signals,
        }


def load_rules(db: Database) -> list[TierRule]:
    """按匹配优先级读规则；同类型内按 tier 升序（更权威的先命中）。

    SQL 在 ``state/tier_rules.py::enabled_rules``（只回原始行，``WHERE enabled = 1`` /
    ``ORDER BY id`` 照旧）；**命中优先级的排序与行 → 规则的字段转换留在这里**：那是判权口径，
    不是存储顺序。
    """
    rules = [
        TierRule(
            id=int(row["id"]),
            match_type=str(row["match_type"]),
            pattern=str(row["pattern"]),
            tier=int(row["tier"]),
            weight=float(row["weight"] or 1.0),
            note=str(row["note"]) if row["note"] is not None else None,
        )
        for row in enabled_rules(db)
    ]
    order = {name: index for index, name in enumerate(MATCH_PRIORITY)}
    return sorted(rules, key=lambda rule: (order.get(rule.match_type, 99), rule.tier, rule.id))


def authority_for(tier: int, weight: float = 1.0) -> float:
    base = TIER_BASE_AUTHORITY.get(tier, 1.0)
    return round(min(5.0, max(0.0, base * weight)), 2)


def classify(
    *,
    url: str,
    domain: str,
    source_type: str,
    rules: list[TierRule],
) -> TierVerdict:
    """找出第一条命中的规则；一条都不中则落到 tier 4（未知或低质）。"""
    for rule in rules:
        if rule.matches(url=url, domain=domain.lower(), source_type=source_type.lower()):
            return TierVerdict(
                tier=rule.tier,
                tier_label=TIER_LABELS.get(rule.tier, TIER_LABELS[4]),
                authority=authority_for(rule.tier, rule.weight),
                matched_rule_id=rule.id,
                matched_pattern=f"{rule.match_type}:{rule.pattern}",
                signals=[
                    {
                        "name": "matched_rule",
                        "value": f"{rule.match_type}:{rule.pattern}",
                        "note": rule.note or "",
                    },
                    {
                        "name": "tier_base_authority",
                        "value": TIER_BASE_AUTHORITY.get(rule.tier, 1.0),
                    },
                    {"name": "rule_weight", "value": rule.weight},
                ],
            )
    return TierVerdict(
        tier=4,
        tier_label=TIER_LABELS[4],
        authority=authority_for(4),
        signals=[{"name": "matched_rule", "value": None, "note": "没有规则命中，按未知处理"}],
    )


def classify_source(
    db: Database, source: dict[str, Any], rules: list[TierRule] | None = None
) -> TierVerdict:
    return classify(
        url=str(source.get("url") or ""),
        domain=str(source.get("domain") or ""),
        source_type=str(source.get("source_type") or "web"),
        rules=rules if rules is not None else load_rules(db),
    )
