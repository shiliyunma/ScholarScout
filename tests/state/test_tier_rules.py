"""`state/tier_rules.py::enabled_rules`：分层规则读侧的口径。

为什么单独钉：这条查询原本写在 `grading/tiers.py::load_rules` 里（白名单
`grading/tiers.py: 1`），下沉时最容易走样的不是列，而是**这里该不该替调用方做决定**：

- 列清单是 `TierRule` 的输入契约（少一列 `load_rules` 就 `KeyError`）；
- `ORDER BY id` 是**存储顺序**，不是判权顺序 —— 命中优先级（`url_regex` 先于 `domain`、
  同类型 tier 升序）留在 grading 层，state 不许抢着排；
- `enabled = 0` 的规则必须被滤掉（关掉一条规则是运维动作，滤错就等于规则没关掉）；
- `weight` / `note` 的兜底（`weight or 1.0`、`note` 空值转 `None`）也是 grading 的事，
  state 原样回库里的值。
"""

from __future__ import annotations

from scholarscout.state.db import Database
from scholarscout.state.tier_rules import enabled_rules


def _seed(db: Database, match_type: str, pattern: str, tier: int, **kw) -> None:
    db.execute(
        "INSERT INTO source_tier_rules (match_type, pattern, tier, weight, note, enabled)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            match_type,
            pattern,
            tier,
            kw.get("weight", 1.0),
            kw.get("note"),
            kw.get("enabled", 1),
        ),
    )


def _clear(db: Database) -> None:
    """种子规则（迁移 0002）先清掉，让用例只看到自己插的行。"""
    db.execute("DELETE FROM source_tier_rules")


def test_empty_table_returns_empty_list(db: Database) -> None:
    _clear(db)
    assert enabled_rules(db) == []


def test_row_shape_is_the_tier_rule_contract(db: Database) -> None:
    _clear(db)
    _seed(db, "domain", "arxiv.org", 1, note="预印本")
    (row,) = enabled_rules(db)
    assert list(row) == ["id", "match_type", "pattern", "tier", "weight", "note"]
    assert row["match_type"] == "domain"
    assert row["pattern"] == "arxiv.org"
    assert row["tier"] == 1
    assert row["note"] == "预印本"


def test_disabled_rules_are_filtered_out(db: Database) -> None:
    _clear(db)
    _seed(db, "domain", "on.example", 2)
    _seed(db, "domain", "off.example", 4, enabled=0)
    assert [row["pattern"] for row in enabled_rules(db)] == ["on.example"]


def test_rows_come_back_in_id_order_not_insert_order(db: Database) -> None:
    """按 id 升序（存储顺序），不是插入顺序 —— 先插 id 大的也能读对。"""
    _clear(db)
    _seed(db, "domain", "second.example", 3)
    _seed(db, "domain", "first.example", 1)
    rows = enabled_rules(db)
    assert [row["pattern"] for row in rows] == ["second.example", "first.example"]
    assert [row["id"] for row in rows] == sorted(row["id"] for row in rows)


def test_no_priority_sorting_here(db: Database) -> None:
    """state 不做命中优先级排序：url_regex 排后面就还是后面，交给 grading 重排。"""
    _clear(db)
    _seed(db, "source_type", "arxiv", 1)
    _seed(db, "url_regex", r"^https://docs\.", 1)
    assert [row["match_type"] for row in enabled_rules(db)] == ["source_type", "url_regex"]


def test_raw_values_are_not_patched_up(db: Database) -> None:
    """`weight` 原样回（0.0 不许被改成 1.0）、`note` 空值回 None —— 兜底在 grading。"""
    _clear(db)
    _seed(db, "domain", "zero.example", 4, weight=0.0, note=None)
    (row,) = enabled_rules(db)
    assert row["weight"] == 0.0
    assert row["note"] is None
