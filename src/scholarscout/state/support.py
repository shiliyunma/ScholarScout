"""引用核查结果的落库（`support_checks` 表）。

一条判定 = 一个 (artifact, item)，同一对只保留最新一次判定（``ON CONFLICT`` 覆盖
verdict/reason/证据等级/时间），这样复核同一批产物**不重复计费**。
判定怎么产生（模型调用、回复解析）在 ``grading/support.py``，不属于状态层。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from scholarscout.state.db import Database


def upsert_check(
    db: Database,
    *,
    check_id: str,
    artifact_kind: str,
    artifact_id: str,
    session_id: str | None,
    item_id: str,
    claim: str,
    source_ids: Sequence[str],
    evidence_level: str,
    verdict: str,
    reason: str,
    model: str,
    prompt_version: str,
    created_at: str,
) -> None:
    """写入/覆盖一条核查判定（``(artifact_id, item_id)`` 唯一）。"""
    db.execute(
        """
        INSERT INTO support_checks (id, artifact_kind, artifact_id, session_id, item_id, claim,
                                    source_ids_json, evidence_level, verdict, reason, model,
                                    prompt_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artifact_id, item_id) DO UPDATE SET
            verdict = excluded.verdict, reason = excluded.reason,
            evidence_level = excluded.evidence_level, created_at = excluded.created_at
        """,
        (
            check_id,
            artifact_kind,
            artifact_id,
            session_id,
            item_id,
            claim,
            json.dumps(list(source_ids), ensure_ascii=False),
            evidence_level,
            verdict,
            reason,
            model,
            prompt_version,
            created_at,
        ),
    )
