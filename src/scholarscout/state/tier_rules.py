"""分层规则表 `source_tier_rules` 的读侧（只读，不在此处判权）。

这条查询原本以 SQL 原文长在 ``grading/tiers.py::load_rules`` 里（白名单
``grading/tiers.py: 1``），这一轮只是**位置**搬进 state：列清单、``WHERE enabled = 1``、
``ORDER BY id`` 一个字没动，返回**原始行** ``list[dict]``。

**业务边界仍留在 grading 层**，这里刻意都不做：

- 命中优先级（``MATCH_PRIORITY``：``url_regex`` > ``domain`` > ``domain_suffix`` >
  ``source_type``，同类型内 tier 升序）是判权口径，不是存储顺序 —— 排序留在
  ``grading/tiers.py::load_rules``；
- 行 → ``TierRule`` 的字段转换（``weight`` 空值按 1.0、``note`` 空值按 ``None``）也在那一层，
  换形状时不牵动仓储；
- ``enabled`` 之外的过滤（比如某次只想读某个 ``match_type``）不在这里加：要什么过滤由调用方
  想清楚，别在 state 里长默认值。
"""

from __future__ import annotations

from typing import Any

from scholarscout.state.db import Database


def enabled_rules(db: Database) -> list[dict[str, Any]]:
    """全部启用的分层规则，按 ``id`` 升序回原始行。

    列清单（``id`` / ``match_type`` / ``pattern`` / ``tier`` / ``weight`` / ``note``）是
    ``TierRule`` 的输入契约：少一列 ``load_rules`` 会 ``KeyError``，改列名等于改判权。
    """
    rows = db.query(
        """
        SELECT id, match_type, pattern, tier, weight, note
        FROM source_tier_rules
        WHERE enabled = 1
        ORDER BY id
        """
    )
    return [dict(row) for row in rows]
