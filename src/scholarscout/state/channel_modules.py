"""每群/每会话绑模块的仓储（表 `channel_modules`，迁移 0018）。

## 它是什么

一张 **入口层状态** 表：`(channel, chat_id) → module`。谁读它：

- `rpc/channel.py::channel_get_module` —— 设置页/排障要看"这个群绑了什么"；
- `rpc/channel.py::module_for_chat` —— 群消息进站时解析"这一轮用哪个模块"，**只有这一处**。

## 唯一碰 SQL 的地方

与 `state/pairing.py` 同一条分工：渠道层（`channels/`）**不读库**（分层门禁
`tests/test_layering.py::RULES["channels"]`），SQL 全部收在本模块，渠道层只经 RPC 拿结论。

## 时间口径

`updated_at` 走 `state/migrate.now_iso()`：ISO-8601、带本地时区偏移、**带微秒**
（同秒内多次换绑时 `ORDER BY` 才有确定次序）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from scholarscout.state.db import Database
from scholarscout.state.migrate import now_iso


def _clean(value: Any, *, field: str) -> str:
    """非空字符串校验：`None` / 空白 / 非字符串一律直接 `ValueError`。

    为什么不"顺手 `str()` 一下"：`str(None)` 会得到 `"None"`，那会让一行脏数据
    看起来像合法绑定（拼错的参数应该当场报错，而不是悄悄写进库）。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串（拿到 {value!r}）")
    return value.strip()


def set_module(
    db: Database,
    channel: str,
    chat_id: str,
    module: str,
    *,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """写入/覆盖一条绑定，回写入后的那一行（换绑 = 覆盖，不保留历史）。"""
    target = _clean(channel, field="channel")
    chat = _clean(chat_id, field="chat_id")
    name = _clean(module, field="module")
    stamp = updated_at or now_iso()
    db.execute(
        """
        INSERT INTO channel_modules (channel, chat_id, module, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(channel, chat_id) DO UPDATE SET
          module = excluded.module,
          updated_at = excluded.updated_at
        """,
        (target, chat, name, stamp),
    )
    return {"channel": target, "chat_id": chat, "module": name, "updated_at": stamp}


def get_module(db: Database, channel: str, chat_id: str) -> str | None:
    """取绑定；**没绑过回 `None`**（不报错 —— "没绑"是正常状态，不是错误）。"""
    target = _clean(channel, field="channel")
    chat = _clean(chat_id, field="chat_id")
    row = db.query_one(
        "SELECT module FROM channel_modules WHERE channel = ? AND chat_id = ?",
        (target, chat),
    )
    return None if row is None else str(row["module"])


def list_chats(db: Database, *, channels: Sequence[str]) -> list[dict[str, Any]]:
    """列**已知的** (channel, chat_id) —— 设置页那张表的唯一数据源。

    两个来源**取并集**，缺一不可：

    1. `channel_modules`：绑过的行（含显式 `default`）；
    2. `sessions`：渠道入站时建过的会话（`channels/feishu.py::run_turn` 拿 `chat_id` 当
       `session_key`，`source` 就是平台名）。**只有这张表能给出"从没绑过的群"** ——
       只查绑定表，设置页就永远显示不出"未绑 = 默认"，用户也就没有入口给新群选模块。

    `channels` 由调用方传（`rpc/channel.py::PLATFORMS`），本模块不猜哪些 `source` 算渠道：
    来源清单只存一份。`module=None` 表示从未绑过（与 `get_module` 同口径）。

    排在最前面的是绑过的（按 `updated_at` 倒序，最近改过的先看到），其余按会话活跃时间倒序。
    """
    bound = db.query(
        """
        SELECT b.channel AS channel, b.chat_id AS chat_id, b.module AS module,
               b.updated_at AS updated_at, s.title AS title
        FROM channel_modules AS b
        LEFT JOIN sessions AS s ON s.session_key = b.chat_id AND s.source = b.channel
        ORDER BY b.updated_at DESC, b.chat_id
        """
    )
    rows = [
        {
            "channel": str(row["channel"]),
            "chat_id": str(row["chat_id"]),
            "module": None if row["module"] is None else str(row["module"]),
            "title": None if row["title"] is None else str(row["title"]),
            "updated_at": None if row["updated_at"] is None else str(row["updated_at"]),
        }
        for row in bound
    ]
    if not channels:
        return rows
    placeholders = ", ".join("?" for _ in channels)
    unseen = db.query(
        f"""
        SELECT s.source AS channel, s.session_key AS chat_id, s.title AS title,
               s.updated_at AS updated_at
        FROM sessions AS s
        WHERE s.session_key IS NOT NULL AND s.source IN ({placeholders})
          AND NOT EXISTS (
            SELECT 1 FROM channel_modules AS m
            WHERE m.channel = s.source AND m.chat_id = s.session_key
          )
        ORDER BY s.updated_at DESC, s.session_key
        """,
        tuple(channels),
    )
    rows.extend(
        {
            "channel": str(row["channel"]),
            "chat_id": str(row["chat_id"]),
            "module": None,
            "title": None if row["title"] is None else str(row["title"]),
            "updated_at": None if row["updated_at"] is None else str(row["updated_at"]),
        }
        for row in unseen
    )
    return rows
