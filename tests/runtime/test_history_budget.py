"""历史重发的裁剪：**保头保尾**，中间丢掉并显式说明。

为什么要裁：历史每一轮都重发，40 条消息的长会话会让 token 随轮次线性膨胀。
为什么是"保头保尾"而不是简单砍最早几条：用户最初那句话定义整个任务（"我想入门 XX"），
丢了它模型就不知道在回答什么；最近几轮才是当前上下文。
为什么丢中间要留一句说明：不说明的话模型会以为对话就这么短，可能重复问已经说过的信息。

实测：当前真实会话（12 条消息 ≈ 1,000 tokens）根本碰不到预算 —— 这条是**防失控**，不是
日常省钱手段。别把它当成主要优化项（探针见 run/cost_probe2.py 那一轮量测）。
"""

from __future__ import annotations

from scholarscout.runtime.loop import _HISTORY_CHAR_BUDGET, _fit_history


def msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_short_history_is_untouched() -> None:
    history = [msg("user", "你好"), msg("assistant", "在的")]
    assert _fit_history(history, 12000) == history


def test_budget_zero_or_negative_means_no_trimming() -> None:
    history = [msg("user", "x" * 5000)]
    assert _fit_history(history, 0) == history
    assert _fit_history(history, -1) == history


def test_long_history_keeps_head_and_tail() -> None:
    first = msg("user", "我想入门教育智能体")
    middle = [msg("assistant", "说明" * 2000) for _ in range(3)]
    last = msg("user", "接着说")
    trimmed = _fit_history([first, *middle, last], 3000)

    assert trimmed[0] is first, "最初的需求必须留下"
    assert trimmed[-1] is last, "最近的对话必须留下"
    assert len(trimmed) < len([first, *middle, last])


def test_trimming_states_how_many_were_dropped() -> None:
    history = [msg("user", "需求"), *[msg("assistant", "y" * 3000) for _ in range(4)]]
    trimmed = _fit_history(history, 2000)
    notes = [item for item in trimmed if item["role"] == "system"]
    assert notes, "丢掉中间就要留一句说明，否则模型以为对话就这么短"
    assert "已省略" in notes[0]["content"]


def test_first_user_message_is_not_duplicated_when_it_is_also_the_tail() -> None:
    """只有一条消息时不能出现两份。"""
    history = [msg("user", "z" * 10000)]
    trimmed = _fit_history(history, 100)
    assert sum(1 for item in trimmed if item is history[0]) == 1


def test_default_budget_is_sane() -> None:
    """默认预算要能在"记住上下文"与"别烧钱"之间站得住（≈4,000 tokens）。"""
    assert 2000 <= _HISTORY_CHAR_BUDGET // 3 <= 8000


def test_fitted_history_respects_budget_when_possible() -> None:
    history = [msg("user", "a" * 1000) for _ in range(20)]
    trimmed = _fit_history(history, 12000)
    total = sum(len(item["content"]) for item in trimmed)
    # 预算管的是"保留的尾部"；最初那条需求（保头）与一句省略说明是**额外**的，
    # 所以总长允许 = 预算 + 头部一条 + 说明。这不是放纵，是保头策略的必然代价。
    head = len(history[0]["content"])
    assert total <= 12000 + head + 200
    assert total >= 12000, "既然超预算了，就该把预算用满（别保守到丢一半）"
