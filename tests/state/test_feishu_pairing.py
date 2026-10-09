"""飞书配对码与白名单的契约（docs/18 §③）。

覆盖：码生成（形态/只存哈希）、错码拒绝、过期码拒绝、未 claim 拒绝、
正确码 → 写白名单 + 收回临时放行、锁定、上限、大小写/空格宽容、撤销。
"""

from __future__ import annotations

from scholarscout.state import pairing

T0 = 1_700_000_000.0


def test_create_code_shape_and_hash_only(db) -> None:
    code = pairing.create_code(db)
    assert code is not None
    assert len(code) == pairing.CODE_LENGTH == 8
    assert all(ch in pairing.CODE_ALPHABET for ch in code)
    # 字母表剔掉 0/O/1/I（防混淆，照 Hermes pairing.py:29）
    assert not ({"0", "O", "1", "I"} & set(pairing.CODE_ALPHABET))
    # 库里**只有哈希**，没有明文码
    row = db.query_one("SELECT entry_id, code_hash, salt, user_id FROM feishu_pairing_codes")
    assert row is not None
    dumped = " ".join(str(value) for value in row)
    assert code not in dumped
    assert len(str(row["salt"])) == 32  # 16 字节 hex
    assert pairing.temp_open(db) is True
    assert pairing.pending_count(db) == 1


def test_wrong_code_rejected(db) -> None:
    pairing.create_code(db, now=T0)
    result = pairing.verify_code(db, "ZZZZZZZZ", now=T0)
    assert result.ok is False
    assert result.reason == "not_found"
    assert pairing.approved_users(db) == []


def test_expired_code_rejected(db) -> None:
    code = pairing.create_code(db, now=T0)
    assert code is not None
    result = pairing.verify_code(db, code, now=T0 + pairing.CODE_TTL_SECONDS + 1)
    assert result.ok is False
    assert result.reason == "expired"
    assert pairing.pending_count(db, now=T0 + pairing.CODE_TTL_SECONDS + 1) == 0


def test_correct_code_writes_allowlist_and_revokes_temp_open(db) -> None:
    code = pairing.create_code(db, now=T0)
    assert code is not None
    assert pairing.temp_open(db) is True
    assert pairing.claim_code(db, code, "ou_owner_1", now=T0) is True
    result = pairing.verify_code(db, code, now=T0)
    assert result.ok is True
    assert result.user_id == "ou_owner_1"
    assert pairing.is_approved(db, "ou_owner_1") is True
    assert pairing.temp_open(db) is False
    assert pairing.pending_count(db, now=T0) == 0


def test_verify_without_claim_is_unclaimed(db) -> None:
    code = pairing.create_code(db, now=T0)
    result = pairing.verify_code(db, code, now=T0)
    assert result.ok is False
    assert result.reason == "unclaimed"
    assert pairing.temp_open(db) is True  # 码还在，临时放行不收
    assert pairing.approved_users(db) == []


def test_claim_does_not_count_as_failed_attempt(db) -> None:
    """机器人收到的普通聊天不是"猜码"，不该把平台聊到锁定。"""
    pairing.create_code(db, now=T0)
    assert pairing.claim_code(db, "NOPE1234", "ou_x", now=T0) is False
    assert pairing.failure_count(db) == 0


def test_lockout_after_too_many_wrong_codes(db) -> None:
    for _ in range(pairing.MAX_FAILED_ATTEMPTS):
        assert pairing.verify_code(db, "AAAAAAAA", now=T0).reason == "not_found"
    assert pairing.locked_out(db, now=T0) is True
    assert pairing.create_code(db, now=T0) is None
    assert pairing.verify_code(db, "AAAAAAAA", now=T0).reason == "locked_out"


def test_max_pending_cap(db) -> None:
    for _ in range(pairing.MAX_PENDING):
        assert pairing.create_code(db, now=T0) is not None
    assert pairing.create_code(db, now=T0) is None


def test_code_matching_ignores_case_and_spaces(db) -> None:
    code = pairing.create_code(db, now=T0)
    assert code is not None
    pairing.claim_code(db, code, "ou_x", now=T0)
    assert pairing.verify_code(db, f"  {code.lower()} ", now=T0).ok is True


def test_list_pending_never_exposes_the_code(db) -> None:
    code = pairing.create_code(db, now=T0, user_name="大浩")
    assert code is not None
    items = pairing.list_pending(db, now=T0)
    assert len(items) == 1
    assert items[0]["claimed"] is False
    assert code not in str(items)


def test_manual_approve_and_revoke(db) -> None:
    pairing.approve_user(db, "ou_y", user_name="张三", now=T0)
    assert pairing.is_approved(db, "ou_y") is True
    assert [row["user_id"] for row in pairing.approved_users(db)] == ["ou_y"]
    assert pairing.revoke_user(db, "ou_y") is True
    assert pairing.is_approved(db, "ou_y") is False
    assert pairing.revoke_user(db, "ou_y") is False
