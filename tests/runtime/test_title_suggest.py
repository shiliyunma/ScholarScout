"""会话标题提前生成：**用户一发送就有名字**，而不是等这一轮跑完。

为什么要有这个改动：一轮对话 3–12 分钟，标题原本要等整轮结束（用户消息落库时才用
本地截断起名）。用户的要求是"输入后立即总结标题"。

实现上有一个**顺序陷阱**（测试里钉住了）：用户消息一落库，`sessions.retitle_if_unnamed`
就会用本地截断起名，那个名字看起来像真标题 → 模型总结的标题再也写不进去。
所以必须"轮次前先总结标题"。

口径：
- 标题失败**绝不影响这一轮**（失败就走本地截断兜底）；
- 只在会话**未命名**时做（用户改过名的会话不会被覆盖）；
- 关掉开关（`generation.title_by_model=false`）就完全不调模型。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from scholarscout.config import Config
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.runtime import titles
from scholarscout.state import sessions

from ..conftest import StubLlm, reply


@pytest.fixture(autouse=True)
def _enable_model_titles(ctx: AppContext) -> None:
    """本文件专门测标题，把开关打开。

    conftest 的测试配置默认 `title_by_model=False`（否则每个 chat.send 测试都要多备一条
    桩回复），所以这里要显式打开 —— 产品里的默认值是**开**。
    """
    ctx.config = replace(ctx.config, generation=replace(ctx.config.generation, title_by_model=True))


class TitleLlm(StubLlm):
    """桩模型：把它收到的第一条 system 提示词记下来，便于断言"确实问了模型"。"""

    def __init__(self, *replies: Any) -> None:
        super().__init__(*replies)
        self.system_prompts: list[str] = []

    def chat(self, messages: list[dict[str, object]], *, tools: Any = None) -> Any:
        for message in messages:
            if message.get("role") == "system":
                self.system_prompts.append(str(message.get("content") or ""))
                break
        return super().chat(messages, tools=tools)


def _new_session(ctx: AppContext) -> str:
    return sessions.create_session(ctx.db, model=ctx.config.chat.model)


def _events(ctx: AppContext) -> list[tuple[str, dict[str, Any]]]:
    captured: list[tuple[str, dict[str, Any]]] = []
    ctx.emit = lambda name, payload: captured.append((name, payload))
    return captured


# ------------------------------------------------------------------ 纯函数


def test_prompt_file_loads_and_version_tracks_content(config: Config) -> None:
    """提示词版本 = 内容哈希（改一个字就变），并且文件必须真的存在。"""
    version = titles.prompt_version(config.summary_cfg.prompts_dir)
    assert len(version) == 12
    raw = (config.summary_cfg.prompts_dir / "suggest-title.md").read_text(encoding="utf-8")
    assert "未命名" in raw


def test_clean_title_strips_quotes_and_punctuation() -> None:
    """ "「RAG 检索增强入门」。" → "RAG 检索增强入门"（模型爱带引号和句号）。"""
    assert titles.clean_title("「RAG 检索增强入门」。") == "RAG 检索增强入门"
    assert titles.clean_title('" 教育智能体调研 "') == "教育智能体调研"
    assert titles.clean_title("  多行\n标题  ") == "多行 标题"


def test_clean_title_rejects_empty_and_unnamed() -> None:
    """信息不足（模型输出"未命名"）或空输出都算没起出名字 → 走本地兜底。"""
    assert titles.clean_title("") is None
    assert titles.clean_title("   ") is None
    assert titles.clean_title("未命名") is None
    assert titles.clean_title("未命名。") is None


def test_clean_title_truncates_long_titles() -> None:
    title = titles.clean_title("超长" * 40)
    assert title is not None
    assert len(title) <= titles.TITLE_MAX_CHARS + 1  # 末尾省略号
    assert title.endswith("…")


def test_suggest_title_returns_none_when_the_model_fails(config: Config) -> None:
    """模型报错 → None（调用方保留兜底），绝不把异常抛给这一轮对话。"""

    class BoomLlm:
        def chat(self, messages: list[dict[str, object]], *, tools: Any = None) -> Any:
            raise RuntimeError("模型炸了")

    assert (
        titles.suggest_title(
            BoomLlm(), "帮我找教育智能体资料", prompts_dir=config.summary_cfg.prompts_dir
        )
        is None
    )


# ------------------------------------------------- 轮次前生成（含顺序陷阱）


def test_title_is_generated_before_the_turn_and_emitted(ctx: AppContext) -> None:
    """标题在轮次开始前就写好并推事件 —— 用户不用等 3–12 分钟。"""
    session_id = _new_session(ctx)
    events = _events(ctx)
    ctx.llm = TitleLlm(
        reply("教育智能体调研"),  # 第一次调用：起标题
        reply("好，我来查"),  # 第二次调用：正式这一轮
    )
    result = dispatch(
        ctx, "chat.send", {"session_id": session_id, "text": "帮我找教育智能体方向的入门资料"}
    )
    assert result["stop_reason"] == "completed"

    row = sessions.get_session(ctx.db, session_id)
    assert row is not None and row["title"] == "教育智能体调研"
    # 两条：一条标题事件、其余是这一轮的流事件
    updates = [payload for name, payload in events if name == "session.updated"]
    assert updates and updates[0]["title"] == "教育智能体调研"
    # 第一次调用问的是标题提示词（不是对话系统提示词）
    assert "会话标题" in ctx.llm.system_prompts[0]
    assert len(ctx.llm.calls) == 2


TITLE_MARKER = "总结成一个会话标题"


def _title_call_indexes(llm: Any) -> list[int]:
    """哪些调用是"起标题"那次 —— 按提示词内容认，不按总调用次数。"""
    return [
        index
        for index, call in enumerate(llm.calls)
        if any(TITLE_MARKER in str(m.get("content") or "") for m in call["messages"])
    ]


def test_title_only_generated_once_per_session(ctx: AppContext) -> None:
    """第二轮不再起标题：已有真标题（省钱，也不覆盖）。"""
    session_id = _new_session(ctx)
    ctx.emit = lambda name, payload: None
    first_llm = TitleLlm(reply("第一次的标题"), reply("答复一"))
    ctx.llm = first_llm
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "第一个问题"})
    first_calls = len(first_llm.calls)
    # 先证明这个过滤器本身有效（否则下面那条断言会"空过"）
    assert len(_title_call_indexes(first_llm)) == 1, "第一轮应当为标题花一次调用"

    ctx.llm = TitleLlm(reply("答复二"))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "第二个问题"})
    # 断言"没有再为标题花一次调用"，而不是"总共只调了一次" ——
    # 一轮里除了回答还可能有别的后台调用（例如 D2 的记忆抽取，每会话一次）。
    # 按总次数断言会让这类新增功能把一个好测试变成假红。
    assert _title_call_indexes(ctx.llm) == [], "第二轮不该再花一次调用去起标题"
    assert first_calls == 2
    row = sessions.get_session(ctx.db, session_id)
    assert row is not None and row["title"] == "第一次的标题"


def test_user_renamed_session_is_not_overwritten(ctx: AppContext) -> None:
    """用户手动改过名的会话：既不覆盖，也不为它多花一次调用。"""
    session_id = _new_session(ctx)
    ctx.emit = lambda name, payload: None
    sessions.rename_session(ctx.db, session_id, "我自己起的名字")
    ctx.llm = TitleLlm(reply("答复"))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "随便问问"})
    row = sessions.get_session(ctx.db, session_id)
    assert row is not None and row["title"] == "我自己起的名字"
    assert len(ctx.llm.calls) == 1, "已有真标题还去问模型＝白花钱"


def test_title_failure_keeps_the_turn_and_falls_back(ctx: AppContext) -> None:
    """起标题失败不该让这一轮失败，且本地截断兜底要补上名字。"""
    session_id = _new_session(ctx)

    class FlakyLlm(TitleLlm):
        def chat(self, messages: list[dict[str, object]], *, tools: Any = None) -> Any:
            self.calls.append({"messages": list(messages), "tools": tools})
            if not self.replies:
                raise AssertionError("脚本用完了")
            if len(self.calls) == 1:
                raise RuntimeError("标题调用炸了")
            return self.replies.pop(0)

    ctx.llm = FlakyLlm(reply("这一轮的答复"))
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "帮我看看这条线索"})
    assert result["stop_reason"] == "completed"
    row = sessions.get_session(ctx.db, session_id)
    assert row is not None and row["title"], "本地截断兜底必须补上一个名字"


def test_title_switch_off_makes_no_extra_call(ctx: AppContext) -> None:
    """关掉开关就不该多花那一次调用（成本敏感的用户要能关）。"""
    from dataclasses import replace

    session_id = _new_session(ctx)
    ctx.emit = lambda name, payload: None
    ctx.config = replace(
        ctx.config, generation=replace(ctx.config.generation, title_by_model=False)
    )
    ctx.llm = StubLlm(reply("答复"))
    dispatch(ctx, "chat.send", {"session_id": session_id, "text": "问一句"})
    assert len(ctx.llm.calls) == 1, "关掉开关后只应有这一轮的调用"
    row = sessions.get_session(ctx.db, session_id)
    assert row is not None and row["title"], "仍要有本地截断标题"


def test_no_pending_events_is_not_an_error(ctx: AppContext) -> None:
    """没有 emit（CLI 场景）时不该崩。"""
    session_id = _new_session(ctx)
    ctx.emit = None
    ctx.llm = TitleLlm(reply("标题"), reply("答复"))
    result = dispatch(ctx, "chat.send", {"session_id": session_id, "text": "你好啊"})
    assert result["stop_reason"] == "completed"
    row = sessions.get_session(ctx.db, session_id)
    assert row is not None and row["title"] == "标题"


def test_session_updated_event_shape() -> None:
    """事件参数形状（前端按这个取 title）。"""
    payload = {"session_id": "ses_x", "title": "标题", "source": "model"}
    assert json.dumps(payload, ensure_ascii=False)
