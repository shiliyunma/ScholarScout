"""`state/sessions.py::get_session_by_key`：`chat_id → session_id` 的唯一读口（只读，不建会话）。

为什么单独一条：`rpc/channel_file.py::_session_id` 原本在这一层自己写
``SELECT id FROM sessions WHERE session_key = ?``（白名单 `rpc/channel_file.py: 2` 里的一处）。
同一句查询在 `state/` 已经有一份（`get_session_by_key`，`ensure_session` 也用它），
所以这次是**复用**而不是抄第二份 —— 这条用例钉的就是复用之后仍然成立的两件事：

- 按 `session_key` 命中就回整行、`id` 取得到（调用方只读 `["id"]`）；
- 查不到回 `None`（**不是**空字典、也不是"顺手建一个"）—— 发文件不该凭空造会话。
"""

from __future__ import annotations

from scholarscout.state import sessions
from scholarscout.state.db import Database


def test_get_session_by_key_finds_the_row(db: Database) -> None:
    session_id = sessions.create_session(db, title="飞书私聊", session_key="oc_file_chat_0001")

    row = sessions.get_session_by_key(db, "oc_file_chat_0001")

    assert row is not None
    assert str(row["id"]) == session_id
    assert row["title"] == "飞书私聊"


def test_get_session_by_key_returns_none_and_creates_nothing(db: Database) -> None:
    assert sessions.get_session_by_key(db, "oc_nope") is None
    assert sessions.list_sessions(db) == [], "查不到不许顺手建会话（发文件是只读链路）"
