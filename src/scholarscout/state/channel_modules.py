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
