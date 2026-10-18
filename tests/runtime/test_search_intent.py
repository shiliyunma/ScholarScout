"""轻量搜索意图分类 + 偏好位（方案 3 后端）：规则预筛、缓存、失败开放、偏好位落台账。

全桩：不出网、不真调模型；分类器只跟 ``StubLlm`` 打交道。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from scholarscout.providers.llm import ChatReply, LlmError
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import loop, research_state, search_intent

from ..conftest import StubLlm, reply


class _AlwaysLlm:
    """固定回复的桩（缓存容量用例要调很多次，不能靠 ``StubLlm`` 的脚本长度）。"""

    def __init__(self, content: str = '{"type":"paper","confidence":0.9}') -> None:
        self.content = content
        self.calls = 0

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        del messages, tools
        self.calls += 1
        return reply(self.content)


class _BoomLlm:
    """每次调用都抛错（模拟超时/网络失败）。"""

    def chat(self, messages: list[dict[str, Any]], *, tools: Any = None) -> ChatReply:
        del messages, tools
        raise LlmError("timeout", "模型请求超时（模拟）")


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    search_intent.clear_cache()
    yield
    search_intent.clear_cache()


# ------------------------------------------------------------------ 规则部分


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("只看论文", "paper"),
        ("只要代码就行", "code"),
        ("只想看教程", "tutorial"),
        ("只要数据集", "data"),
        ("先别搜了，直接回答", "none"),
        ("不用检索", "none"),
        ("都可以，全部来源", "other"),
        ("帮我找教育智能体方向的资料", None),
        ("education agent survey", None),
    ],
)
def test_explicit_choice_rules(text: str, expected: str | None) -> None:
    assert search_intent.explicit_preference(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("x", True),
        ("帮我找点资料", True),
        ("教育智能体怎么入门", True),
        ("教育智能体", True),
        ("education agent", False),
        ("arxiv paper on education agent", False),
        ("给我 github 上的实现", False),
        ("", False),
    ],
)
def test_rule_prefilter_only_fires_for_vague_queries(text: str, expected: bool) -> None:
    """预筛就是成本闸门：明确的查询**不该**多花一次模型调用。"""
    assert search_intent.looks_ambiguous(text) is expected


def test_explicit_choice_does_not_call_the_model() -> None:
    llm = StubLlm()  # 没有任何回复：一旦被调用就会抛 AssertionError
    state: dict[str, Any] = {}
    intent = search_intent.resolve_turn(llm, "只看论文", state=state, turn_id="trn_1")
    assert intent is not None
    assert intent.source == "explicit" and intent.confidence == 1.0
    assert state[search_intent.PREFERENCE_KEY] == "paper"
    assert llm.calls == []


def test_clear_query_does_not_call_the_model_either() -> None:
    llm = StubLlm()
    state: dict[str, Any] = {}
    assert search_intent.resolve_turn(llm, "education agent", state=state, turn_id="trn_1") is None
    assert llm.calls == []
    assert search_intent.PREFERENCE_KEY not in state


# ------------------------------------------------------------------ 分类器


def test_classifier_parses_one_line_json_and_keeps_usage() -> None:
    llm = StubLlm(reply('{"type":"code","confidence":0.82}'))
    intent = search_intent.classify(llm, "那个东西怎么跑起来")
    assert (intent.type, intent.confidence, intent.source, intent.failed) == (
        "code",
        0.82,
        "model",
        False,
    )
    assert intent.usage == {"prompt_tokens": 11, "completion_tokens": 7}
    # 短提示 + 无工具：分类调用绝不能带工具声明（否则模型会开始"调工具"）
    assert llm.calls[0]["tools"] is None
    system = llm.calls[0]["messages"][0]["content"]
    assert "type" in system and "confidence" in system


def test_classifier_tolerates_code_fence_but_not_garbage() -> None:
    fenced = '```json\n{"type":"news","confidence":0.7}\n```'
    assert search_intent.classify(StubLlm(reply(fenced)), "发布会").type == "news"
    garbage = search_intent.classify(StubLlm(reply("我觉得是论文吧")), "那个")
    assert garbage.failed is True and garbage.type == "other"


def test_unknown_type_is_a_failure_not_silent_other() -> None:
    intent = search_intent.classify(StubLlm(reply('{"type":"video","confidence":0.9}')), "那个")
    assert intent.failed is True


def test_cache_hit_does_not_spend_a_second_model_call() -> None:
    llm = StubLlm(reply('{"type":"paper","confidence":0.8}'))
    first = search_intent.classify(llm, "帮我找点资料")
    second = search_intent.classify(llm, "  帮我找点资料 ")  # 归一化后同一个键
    assert len(llm.calls) == 1
    assert first.source == "model" and second.source == "cache"
    assert (first.type, first.confidence) == (second.type, second.confidence)


def test_cache_is_bounded() -> None:
    llm = _AlwaysLlm()
    for index in range(search_intent.CACHE_SIZE + 40):
        search_intent.classify(llm, f"模糊查询 {index}")
    assert search_intent.cache_size() == search_intent.CACHE_SIZE


def test_model_error_is_open_not_blocking() -> None:
    """失败开放：分类器挂了也只是"没判出来"，绝不抛给调用方。"""
    intent = search_intent.classify(_BoomLlm(), "帮我找点资料")
    assert intent.failed is True and intent.type == "other"


def test_resolve_turn_records_low_confidence_without_a_preference() -> None:
    llm = StubLlm(reply('{"type":"other","confidence":0.2}'))
    state: dict[str, Any] = {}
    intent = search_intent.resolve_turn(llm, "帮我找点资料", state=state, turn_id="trn_1")
    assert intent is not None and intent.confidence == 0.2
    # 低置信**不写偏好位**：让工具去问用户，而不是替他猜
    assert search_intent.PREFERENCE_KEY not in state
    assert state[search_intent.INTENT_KEY]["turn_id"] == "trn_1"
    assert state[search_intent.INTENT_KEY]["confidence"] == 0.2


def test_resolve_turn_writes_preference_when_confident() -> None:
    llm = StubLlm(reply('{"type":"code","confidence":0.9}'))
    state: dict[str, Any] = {}
    search_intent.resolve_turn(llm, "那个东西怎么跑", state=state, turn_id="trn_7")
    assert state[search_intent.PREFERENCE_KEY] == "code"
    assert state[search_intent.PREFERENCE_TURN_KEY] == "trn_7"


def test_resolve_turn_is_a_noop_when_disabled() -> None:
    llm = StubLlm()
    state: dict[str, Any] = {}
    assert (
        search_intent.resolve_turn(llm, "只看论文", state=state, turn_id="trn_1", enabled=False)
        is None
    )
    assert llm.calls == []
    assert search_intent.PREFERENCE_KEY not in state


def test_filter_providers_keeps_family_and_reports_empty_honestly() -> None:
    available = {"arxiv": object(), "github": object()}
    assert search_intent.filter_providers(["arxiv", "github"], available, "paper") == ["arxiv"]
    assert search_intent.filter_providers(["arxiv", "github"], available, "code") == ["github"]
    assert search_intent.filter_providers(["arxiv", "github"], available, None) == [
        "arxiv",
        "github",
    ]
    # 这一族一个可用来源都没有 → 空表（调用方据此如实回"没来源"，不要偷偷退回全量）
    assert search_intent.filter_providers(["arxiv", "github"], available, "news") == []


# ------------------------------------------------------------------ 与主循环的接线


def test_turn_start_records_preference_but_system_prompt_stays_identical(
    ctx: AppContext,
) -> None:
    """偏好位进台账、**不进系统提示**：系统提示必须与"没点过任何选项"时逐字相同。"""
    from scholarscout.runtime.prompts import build_system_prompt
    from scholarscout.state import sessions

    ctx.config = replace(
        ctx.config,
        runtime=replace(ctx.config.runtime, classify_search_intent=True),
    )
    ctx.llm = StubLlm(reply('{"type":"paper","confidence":0.9}'), reply("好的"))
    session_id = sessions.create_session(ctx.db, model=ctx.config.chat.model)

    loop.run_turn(ctx.turn_context(), session_id=session_id, text="教育智能体怎么入门")

    state = research_state.load_state(ctx.db, session_id)
    assert state is not None and state[search_intent.PREFERENCE_KEY] == "paper"

    expected = build_system_prompt(toolsets=["research"])
    assert ctx.llm.calls[1]["messages"][0]["content"] == expected
    # 连"随轮注入"的那几条 system 消息里也不许出现偏好位（否则等于换了个提示词）
    for message in ctx.llm.calls[1]["messages"]:
        if message["role"] == "system":
            assert "search_preference" not in str(message["content"])
    assert "search_preference" not in research_state.render_note(state, [])

    purposes = [
        row["purpose"]
        for row in ctx.db.query("SELECT purpose FROM usage_records ORDER BY created_at")
    ]
    assert purposes == ["intent", "chat"]  # 这次小调用的钱也记了账
