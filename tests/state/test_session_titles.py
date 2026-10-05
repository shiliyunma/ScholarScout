"""会话标题：自动命名（"总结"式）与手动改名。

背景：桌面端新建会话时**硬编码** `title="新调研"`，而后端的自动命名条件是
`title IS NULL OR title = ''` —— 于是自动命名永远不触发，所有会话都叫"新调研"。
这里的用例把三件事钉死：

1. 占位标题（"新调研"等）**也算未命名**，首条消息要能把老会话救回来；
2. 用户手动改过的名字**不能被覆盖**；
3. 只有第一条用户消息能命名，之后的对话不会把标题改来改去。
"""

from __future__ import annotations

import pytest

from scholarscout.state.db import Database
from scholarscout.state.sessions import (
    PLACEHOLDER_TITLES,
    append_message,
    auto_title,
    create_session,
    get_session,
    rename_session,
    retitle_if_unnamed,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("帮我调研一下知识追踪方向该从哪里入门", "调研一下知识追踪方向该从哪里入门"),
        ("请给我一份教育智能体的入门资料", "一份教育智能体的入门资料"),
        ("我想做一个知识追踪的实验", "做一个知识追踪的实验"),
        ("  空白   被压平  ", "空白 被压平"),
        ("，。知识追踪？！", "知识追踪"),
        ("", "新会话"),
        ("   ", "新会话"),
        ("帮我", "新会话"),
    ],
)
def test_auto_title_summarizes_instead_of_truncating(raw: str, expected: str) -> None:
    assert auto_title(raw) == expected


def test_auto_title_truncates_long_input() -> None:
    title = auto_title("知识追踪" * 40)
    assert len(title) == 32


def test_retitle_if_unnamed_fills_null_title(db: Database) -> None:
    session_id = create_session(db, source="cli")
    assert retitle_if_unnamed(db, session_id, "帮我画一张知识追踪的主题地图") == (
        "画一张知识追踪的主题地图"
    )
    assert get_session(db, session_id)["title"] == "画一张知识追踪的主题地图"


@pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_TITLES))
def test_placeholder_titles_count_as_unnamed(db: Database, placeholder: str) -> None:
    """**关键回归**：前端传过 title="新调研"，那时自动命名永远轮不到。"""
    session_id = create_session(db, title=placeholder, source="desktop")
    assert retitle_if_unnamed(db, session_id, "知识追踪该怎么入门") == "知识追踪该怎么入门"
    assert get_session(db, session_id)["title"] == "知识追踪该怎么入门"


def test_user_title_is_never_overwritten(db: Database) -> None:
    session_id = create_session(db, title="我自己起的名字", source="desktop")
    assert retitle_if_unnamed(db, session_id, "随便说点什么") is None
    assert get_session(db, session_id)["title"] == "我自己起的名字"


def test_only_first_user_message_sets_the_title(db: Database) -> None:
    session_id = create_session(db, source="cli")
    append_message(db, session_id=session_id, role="user", content="第一条：聊聊知识追踪")
    append_message(db, session_id=session_id, role="assistant", content="好的")
    append_message(db, session_id=session_id, role="user", content="第二条：那 DKT 呢")
    assert get_session(db, session_id)["title"] == "第一条：聊聊知识追踪"


def test_assistant_message_alone_does_not_title(db: Database) -> None:
    session_id = create_session(db, source="cli")
    append_message(db, session_id=session_id, role="assistant", content="我先说")
    assert get_session(db, session_id)["title"] is None


def test_rename_session_returns_stored_title(db: Database) -> None:
    session_id = create_session(db, source="cli")
    assert rename_session(db, session_id, "  新  名字  ") == "新 名字"
    assert get_session(db, session_id)["title"] == "新 名字"
    # 改过名之后，自动命名不许再覆盖
    assert retitle_if_unnamed(db, session_id, "又一条消息") is None


def test_rename_unknown_session_returns_none(db: Database) -> None:
    assert rename_session(db, "ses_nope", "x") is None


def test_rename_empty_title_is_rejected(db: Database) -> None:
    session_id = create_session(db, source="cli")
    with pytest.raises(ValueError, match="标题不能为空"):
        rename_session(db, session_id, "   ")
