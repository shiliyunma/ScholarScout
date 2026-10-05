"""分层规则契约：种子规则、匹配优先级、权威性计算、未知来源兜底。"""

from __future__ import annotations

from scholarscout.grading import tiers
from scholarscout.state.db import Database


def _classify(db: Database, url: str, domain: str | None = None, source_type: str = "web"):
    from urllib.parse import urlparse

    host = domain or (urlparse(url).hostname or "")
    return tiers.classify(url=url, domain=host, source_type=source_type, rules=tiers.load_rules(db))


def test_seed_rules_are_loaded(db: Database) -> None:
    rules = tiers.load_rules(db)
    assert len(rules) >= 30
    kinds = {rule.match_type for rule in rules}
    assert kinds == set(tiers.MATCH_TYPES)


def test_rules_sorted_by_match_priority(db: Database) -> None:
    order = [rule.match_type for rule in tiers.load_rules(db)]
    first_domain = order.index("domain")
    first_source_type = order.index("source_type")
    assert order[0] == "url_regex"
    assert first_domain < first_source_type, "domain 规则必须排在 source_type 之前命中"


def test_arxiv_domain_is_tier_one(db: Database) -> None:
    verdict = _classify(db, "https://arxiv.org/abs/2501.00001", source_type="arxiv")
    assert verdict.tier == 1
    assert verdict.tier_label == "一手权威"
    assert verdict.authority == 5.0
    assert verdict.matched_pattern == "domain:arxiv.org"
    assert verdict.matched_rule_id is not None


def test_docs_prefix_wins_by_url_regex(db: Database) -> None:
    """docs.github.com 同时命中 url_regex(docs.) 与 domain(github.com) —— 前者优先。"""
    verdict = _classify(db, "https://docs.github.com/en/rest", source_type="web")
    assert verdict.tier == 1
    assert verdict.matched_pattern is not None and verdict.matched_pattern.startswith("url_regex")


def test_publisher_domain_is_tier_two(db: Database) -> None:
    verdict = _classify(db, "https://dl.acm.org/doi/10.1145/1")
    assert verdict.tier == 2
    assert verdict.authority == 4.0


def test_community_domain_is_tier_three(db: Database) -> None:
    assert _classify(db, "https://github.com/a/b").tier == 3
    assert _classify(db, "https://stackoverflow.com/q/1").tier == 3


def test_low_quality_domain_is_tier_four(db: Database) -> None:
    assert _classify(db, "https://blog.csdn.net/x/article").tier == 4


def test_domain_suffix_rules(db: Database) -> None:
    assert _classify(db, "https://www.mit.edu/research").tier == 2
    assert _classify(db, "https://www.tsinghua.edu.cn/x").tier == 2


def test_org_suffix_has_reduced_weight(db: Database) -> None:
    verdict = _classify(db, "https://some-foundation.org/about")
    assert verdict.tier == 3
    assert verdict.authority == 1.25, "weight 0.5 应把 2.5 的基准分打折"


def test_unknown_domain_falls_back_to_tier_four(db: Database) -> None:
    verdict = _classify(db, "https://totally-unknown-xyz.example/page", source_type="mystery")
    assert verdict.tier == 4
    assert verdict.authority == 1.0
    assert verdict.matched_rule_id is None
    assert verdict.signals[0]["value"] is None


def test_source_type_rule_used_when_domain_unknown(db: Database) -> None:
    verdict = _classify(db, "https://mirror.example.org/abs/1", source_type="arxiv")
    # .org 后缀（tier 3）比 source_type=arxiv（tier 1）先命中 —— 域名规则优先于类型规则
    assert verdict.tier == 3
    verdict2 = _classify(db, "https://mirror.example.com/abs/1", source_type="arxiv")
    assert verdict2.tier == 1
    assert verdict2.matched_pattern == "source_type:arxiv"


def test_domain_match_is_case_insensitive(db: Database) -> None:
    assert _classify(db, "https://ARXIV.ORG/abs/1", domain="ARXIV.ORG").tier == 1


def test_disabled_rule_is_skipped(db: Database) -> None:
    db.execute("UPDATE source_tier_rules SET enabled = 0 WHERE pattern = 'arxiv.org'")
    verdict = _classify(db, "https://arxiv.org/abs/1", source_type="web")
    assert verdict.matched_pattern != "domain:arxiv.org"


def test_authority_for_is_clamped() -> None:
    assert tiers.authority_for(1, 1.0) == 5.0
    assert tiers.authority_for(1, 2.0) == 5.0, "权重放大也不许超过 5"
    assert tiers.authority_for(4, 0.1) == 0.1
    assert tiers.authority_for(99, 1.0) == 1.0, "未知 tier 落到最低基准分"


def test_invalid_url_regex_is_ignored(db: Database) -> None:
    db.execute(
        """
        INSERT INTO source_tier_rules (match_type, pattern, tier, weight, note)
        VALUES ('url_regex', '([', 1, 1.0, '故意写坏的正则')
        """
    )
    verdict = _classify(db, "https://broken.example.org/x")
    assert verdict.tier in {3, 4}  # 坏正则不命中，但不许抛异常


def test_tier_labels_cover_all_tiers() -> None:
    assert set(tiers.TIER_LABELS) == {1, 2, 3, 4}
    assert set(tiers.TIER_BASE_AUTHORITY) == {1, 2, 3, 4}


def test_verdict_as_dict_shape(db: Database) -> None:
    payload = _classify(db, "https://arxiv.org/abs/1").as_dict()
    assert set(payload) == {
        "tier",
        "tier_label",
        "authority",
        "matched_rule_id",
        "matched_pattern",
        "signals",
    }
