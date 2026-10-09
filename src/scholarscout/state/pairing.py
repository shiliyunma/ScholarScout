"""飞书配对码与白名单（**入口层状态**，docs/18 §③）。

## 规格（照 Hermes `gateway/pairing.py`，逐条带锚点）

- 字母表 `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`（**剔掉 0/O/1/I 防混淆**，`pairing.py:29`）；
- 长度 `8`（`:30`）—— 实测终端里的码正是 8 位大写字母数字（如 `KMHSZQXF`）；
- 有效期 `3600` 秒（`:32`）；同平台待批上限 `3`（`:35`）；失败 `5` 次锁 `3600` 秒（`:34/:36`）；
- **只存 `sha256(salt + code)`**（`:412-414`），条目键是随机 `entry_id`（`:444`），码本身**不落盘**；
- 批准 = 写白名单（`_approve_user`，`:384-393`）。

## 与 Hermes 的方向差异（docs/18 §3.3，本项目有意为之）

Hermes 是**机器人给未知用户发码**，码在生成时**已带 `user_id`**（`run_inbound.py:88`）；
本项目是向导**先生成不带身份的码** → 用户把码发给机器人 → `claim_code` 把 openid 绑上去
→ `verify_code` 批准。这样 openid **自动落库**，用户不必手抄 openid。

## 临时放行

「临时放行」是"还没批准任何身份时，渠道层先放行"的开关，借 `state_meta` 存（不另开表）。
不变量：**只要有未批的码就开，批准/清空后关** —— 由 `create_code` / `verify_code` 自动维护。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scholarscout.state.db import Database

#: 无歧义字母表（无 0/O/1/I），照 Hermes `pairing.py:29`
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
#: 码长度（`pairing.py:30`）
CODE_LENGTH = 8
#: 有效期（`pairing.py:32`，1 小时）
CODE_TTL_SECONDS = 3600
#: 同平台最多同时待批几枚码（`pairing.py:35`）
MAX_PENDING = 3
#: 连续错码多少次触发锁定（`pairing.py:36`）
MAX_FAILED_ATTEMPTS = 5
#: 锁定时长（`pairing.py:34`）
LOCKOUT_SECONDS = 3600

#: `state_meta` 里的键
TEMP_OPEN_KEY = "feishu_temp_open"
_FAILURES_KEY = "feishu_pairing_failures"
_LOCKOUT_KEY = "feishu_pairing_lockout_until"


@dataclass(frozen=True)
class VerifyResult:
    """`verify_code` 的结果。

    `reason` ∈ `"ok"` / `"not_found"` / `"expired"` / `"unclaimed"` / `"locked_out"` /
    `"too_many_pending"`。刻意把 `unclaimed`（还没收到 openid）与 `not_found`（码不对）
    分开 —— 界面上要能说清"你是不是还没把码发给机器人"。
    """

    ok: bool
    user_id: str = ""
    user_name: str = ""
    reason: str = "ok"


# --------------------------------------------------------------------- 内部工具


def _epoch(now: float | datetime | None) -> float:
    """把注入的时间规整成秒级 epoch。**不读系统钟**（`now is None` 时才取当前时间）。"""
    if now is None:
        return time.time()
    if isinstance(now, bool):  # bool 是 int 的子类，挡掉
        raise TypeError("now 不能是 bool")
    if isinstance(now, int | float):
        return float(now)
    if isinstance(now, datetime):
        return now.timestamp()
    raise TypeError(f"不支持的 now 类型：{type(now).__name__}")


def _stamp(epoch: float) -> str:
    """epoch → ISO-8601（带本地时区），与项目统一时间格式一致。"""
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone().isoformat(timespec="microseconds")


def _parse_stamp(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def _hash_code(code: str, salt: bytes) -> str:
    """`sha256(salt + code)`，照 Hermes `pairing.py:413-414`。"""
    return hashlib.sha256(salt + code.encode("utf-8")).hexdigest()


def _normalize(code: str) -> str:
    return str(code or "").strip().upper()


def _find_entry(db: Database, code: str) -> tuple[str, Any] | None:
    """按定时安全比较找条目（**含已过期的**，好让调用方区分"过期"与"不存在"）。"""
    normalized = _normalize(code)
    if not normalized:
        return None
    for row in db.query(
        "SELECT entry_id, code_hash, salt, user_id, user_name, created_at FROM feishu_pairing_codes"
    ):
        try:
            salt = bytes.fromhex(str(row["salt"]))
        except ValueError:
            continue
        if hmac.compare_digest(_hash_code(normalized, salt), str(row["code_hash"])):
            return str(row["entry_id"]), row
    return None


def _get_meta(db: Database, key: str) -> str:
    value = db.scalar_str("SELECT value FROM state_meta WHERE key = ?", (key,))
    return value or ""


def _set_meta(db: Database, key: str, value: str, epoch: float) -> None:
    db.execute(
        """
        INSERT INTO state_meta (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, _stamp(epoch)),
    )


# --------------------------------------------------------------------- 临时放行


def set_temp_open(db: Database, enabled: bool, *, now: float | datetime | None = None) -> None:
    """开关「临时放行」（存 `state_meta`）。"""
    _set_meta(db, TEMP_OPEN_KEY, "1" if enabled else "0", _epoch(now))


def temp_open(db: Database) -> bool:
    return _get_meta(db, TEMP_OPEN_KEY) == "1"


# --------------------------------------------------------------------- 锁定 / 失败


def locked_out(db: Database, *, now: float | datetime | None = None) -> bool:
    until = _get_meta(db, _LOCKOUT_KEY)
    if not until:
        return False
    try:
        return _epoch(now) < float(until)
    except ValueError:
        return False


def failure_count(db: Database) -> int:
    raw = _get_meta(db, _FAILURES_KEY)
    return int(raw) if raw.isdigit() else 0


def _record_failed(db: Database, epoch: float) -> None:
    """记一次错码；连续到阈值即锁定（照 `pairing.py:562-573`）。"""
    fails = failure_count(db) + 1
    if fails >= MAX_FAILED_ATTEMPTS:
        _set_meta(db, _LOCKOUT_KEY, str(epoch + LOCKOUT_SECONDS), epoch)
        _set_meta(db, _FAILURES_KEY, "0", epoch)
    else:
        _set_meta(db, _FAILURES_KEY, str(fails), epoch)


def _reset_failures(db: Database) -> None:
    if failure_count(db):
        _set_meta(db, _FAILURES_KEY, "0", _epoch(None))


# --------------------------------------------------------------------- 待批码


def clear_expired(db: Database, *, now: float | datetime | None = None) -> int:
    """删掉过期码，返回删除数（照 `pairing.py:352-362`）。"""
    epoch = _epoch(now)
    rows = db.query("SELECT entry_id, created_at FROM feishu_pairing_codes")
    expired = [
        str(r["entry_id"])
        for r in rows
        if epoch - _parse_stamp(str(r["created_at"])) > CODE_TTL_SECONDS
    ]
    for entry_id in expired:
        db.execute("DELETE FROM feishu_pairing_codes WHERE entry_id = ?", (entry_id,))
    return len(expired)


def pending_count(db: Database, *, now: float | datetime | None = None) -> int:
    clear_expired(db, now=now)
    return db.scalar_int("SELECT count(*) FROM feishu_pairing_codes")


def list_pending(db: Database, *, now: float | datetime | None = None) -> list[dict[str, Any]]:
    """待批列表（**绝不回传码本身**；只给 entry_id 与绑定的身份，照 `pairing.py:507-525`）。"""
    epoch = _epoch(now)
    clear_expired(db, now=epoch)
    return [
        {
            "entry_id": str(r["entry_id"]),
            "user_id": str(r["user_id"] or ""),
            "user_name": str(r["user_name"] or ""),
            "claimed": bool(r["user_id"]),
            "age_seconds": max(0, int(epoch - _parse_stamp(str(r["created_at"])))),
        }
        for r in db.query(
            "SELECT entry_id, user_id, user_name, created_at FROM feishu_pairing_codes ORDER BY created_at"
        )
    ]


def create_code(
    db: Database,
    *,
    user_name: str = "",
    now: float | datetime | None = None,
) -> str | None:
    """生成一枚配对码并**把临时放行打开**；待批已满返回 None。

    返回的是**明文码**，也是它唯一一次可见的地方 —— 库里只有哈希。
    """
    epoch = _epoch(now)
    clear_expired(db, now=epoch)
    if locked_out(db, now=epoch) or pending_count(db, now=epoch) >= MAX_PENDING:
        return None
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    salt = os.urandom(16)
    db.execute(
        """
        INSERT INTO feishu_pairing_codes (entry_id, code_hash, salt, user_id, user_name, created_at)
        VALUES (?, ?, ?, '', ?, ?)
        """,
        (
            secrets.token_hex(8),
            _hash_code(code, salt),
            salt.hex(),
            str(user_name or ""),
            _stamp(epoch),
        ),
    )
    set_temp_open(db, True, now=epoch)
    return code


def claim_code(
    db: Database,
    code: str,
    open_id: str,
    *,
    user_name: str = "",
    now: float | datetime | None = None,
) -> bool:
    """把 openid 绑到码上（渠道层收到"把码发给机器人"的那条私信时调）。

    **不计失败次数**：机器人收到的普通聊天不是"猜码"，若计入，随便聊两句就能把平台锁死。
    返回 True 表示绑定成功；码不存在/已过期返回 False。
    """
    epoch = _epoch(now)
    open_id = str(open_id or "").strip()
    if not open_id:
        return False
    clear_expired(db, now=epoch)
    found = _find_entry(db, code)
    if found is None:
        return False
    entry_id, _row = found
    db.execute(
        "UPDATE feishu_pairing_codes SET user_id = ?, user_name = ? WHERE entry_id = ?",
        (open_id, str(user_name or ""), entry_id),
    )
    return True


def verify_code(db: Database, code: str, *, now: float | datetime | None = None) -> VerifyResult:
    """校验并批准：成功则**写白名单 + 收回临时放行**（docs/18 §③）。"""
    epoch = _epoch(now)
    if locked_out(db, now=epoch):
        return VerifyResult(False, reason="locked_out")
    found = _find_entry(db, code)
    if found is None:
        _record_failed(db, epoch)
        return VerifyResult(False, reason="not_found")
    entry_id, row = found
    if epoch - _parse_stamp(str(row["created_at"])) > CODE_TTL_SECONDS:
        db.execute("DELETE FROM feishu_pairing_codes WHERE entry_id = ?", (entry_id,))
        _record_failed(db, epoch)
        return VerifyResult(False, reason="expired")
    user_id = str(row["user_id"] or "")
    if not user_id:
        return VerifyResult(False, reason="unclaimed")
    user_name = str(row["user_name"] or "")
    with db.transaction():
        db.execute("DELETE FROM feishu_pairing_codes WHERE entry_id = ?", (entry_id,))
        db.execute(
            """
            INSERT INTO feishu_paired_users (user_id, user_name, approved_at, source)
            VALUES (?, ?, ?, 'pairing')
            ON CONFLICT(user_id) DO UPDATE SET user_name = excluded.user_name, approved_at = excluded.approved_at
            """,
            (user_id, user_name, _stamp(epoch)),
        )
    _reset_failures(db)
    # 收回临时放行：还有别的未批码就继续放行，否则关掉
    set_temp_open(db, db.scalar_int("SELECT count(*) FROM feishu_pairing_codes") > 0, now=epoch)
    return VerifyResult(True, user_id=user_id, user_name=user_name)


# --------------------------------------------------------------------- 白名单


def approve_user(
    db: Database,
    user_id: str,
    *,
    user_name: str = "",
    source: str = "manual",
    now: float | datetime | None = None,
) -> None:
    """直接写白名单（手工配置那条路，照 Hermes 选 3 时写 `FEISHU_ALLOWED_USERS`）。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("user_id 不能为空")
    db.execute(
        """
        INSERT INTO feishu_paired_users (user_id, user_name, approved_at, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET user_name = excluded.user_name, approved_at = excluded.approved_at
        """,
        (user_id, str(user_name or ""), _stamp(_epoch(now)), source),
    )


def is_approved(db: Database, user_id: str) -> bool:
    user_id = str(user_id or "").strip()
    if not user_id:
        return False
    return (
        db.scalar_int("SELECT count(*) FROM feishu_paired_users WHERE user_id = ?", (user_id,)) > 0
    )


def approved_users(db: Database) -> list[dict[str, Any]]:
    return [
        {
            "user_id": str(r["user_id"]),
            "user_name": str(r["user_name"] or ""),
            "approved_at": str(r["approved_at"]),
            "source": str(r["source"] or ""),
        }
        for r in db.query(
            "SELECT user_id, user_name, approved_at, source FROM feishu_paired_users ORDER BY approved_at"
        )
    ]


def revoke_user(db: Database, user_id: str) -> bool:
    """从白名单移除（**必须有出口**：误批准要能撤）。"""
    user_id = str(user_id or "").strip()
    if not user_id:
        return False
    existed = is_approved(db, user_id)
    if existed:
        db.execute("DELETE FROM feishu_paired_users WHERE user_id = ?", (user_id,))
    return existed
