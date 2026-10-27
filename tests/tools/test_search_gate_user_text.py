"""D3/D2：闸门判**用户原话**（不是模型改写后的参数）+ 选择卡标题不再堆英文检索式。

真机证据（`run/ui_eval/final/defects.md`）：

- **D3**：同一类泛指意图两种行为 —— 「找点资料」出卡，「帮我找点资料了解一下」不出卡、
  直接 `research.search → source.fetch ×2 → research.search` 共 4 轮。根因是
  `looks_ambiguous()` 判的是**工具参数**，模型把原话改写成一句长英文 query 就绕过了闸门；
- **D2**：卡标题把模型改写的英文检索式整条摊到中文界面（「这次要搜哪类资料？针对
  「research question decomposition keyword formulation…」」）。

| 用例 | 期望 |
|---|---|
| 原话泛指 + 参数被改写成具体英文 + 分类器高置信（preference 已写） | **仍出卡**、零出网、标题里没有英文 |
| 原话具体（「RAG 的核心步骤」） | 不拦，真的搜 |
| 原话具体（「帮我找 2024 年 RAG 综述」） | 不拦，真的搜 |
| 没有本轮判定、参数本身泛指 | 拦（第二道判据） |
| 原话是显式选择（「只看论文」） | 不拦（那是选完了，不是没说清） |
| 台账里的原话属于**上一轮** | 不拿旧话拦 |
| 开关关掉 | 完全回到引入前（消融口子） |

标题清洗（D2）另有一组纯函数用例：英文改写式 → 空串（界面只剩中文提问句）、
中文长句 → 截到 40 字、中文短句 → 原样留作小字。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import research_state as rs
from scholarscout.runtime import search_intent as si
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools import register_tools
from scholarscout.tools.research_search import research_search

from ..conftest import StubLlm, reply

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "arxiv_sample.xml"
SESSION = "ses_user_text_gate"
TURN = "trn_1"
#: 模型把「帮我找点资料了解一下」改写成的**具体英文检索式**（真机原样）
REWRITTEN = "research question decomposition keyword formulation literature search strategy systematic review"
GITHUB_JSON = {
    "total_count": 1,
    "items": [
        {
            "full_name": "pykt-team/pykt-toolkit",
            "html_url": "https://github.com/pykt-team/pykt-toolkit",
            "description": "Knowledge tracing toolkit",
            "stargazers_count": 441,
            "language": "Python",
            "pushed_at": "2024-05-01T00:00:00Z",
        }
    ],
}

#: 分类器说"高置信 other"——这一支专治"分类器猜得很自信，但用户原话其实没说清"
HIGH_CONFIDENCE_OTHER = '{"type": "other", "confidence": 0.9}'


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    register_tools()
    si.clear_cache()
    yield
    si.clear_cache()


class _Recorder:
    """记事件 + 数**真的出网**（断言"没搜"要看 URL，不看自报状态）。"""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._xml = FIXTURE.read_text(encoding="utf-8")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        host = request.url.host
        if host == "export.arxiv.org":
            return httpx.Response(
                200, text=self._xml, headers={"content-type": "application/atom+xml"}
            )
        if host == "api.github.com":
            return httpx.Response(200, json=GITHUB_JSON)
        raise AssertionError(f"测试不该请求这个主机：{host}")

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))

    def choice_payload(self) -> dict[str, Any]:
        kinds = [kind for kind, _ in self.events]
        assert kinds == ["chat.needs_choice"], f"事件不对：{kinds}"
        return self.events[0][1]


def _seed(
    ctx: AppContext,
    *,
    user_text: str | None = None,
    turn_id: str = TURN,
    preference: str | None = None,
    intent: si.Intent | None = None,
) -> None:
    """按**生产路径**布置台账：原话 + 偏好位都在 ``research_states.payload_json`` 里。"""
    state = rs.empty_state(user_text or "目标")
    if user_text is not None:
        state[si.USER_TEXT_KEY] = user_text
        state[si.USER_TEXT_TURN_KEY] = turn_id
    si.set_preference(state, preference=preference, turn_id=turn_id, intent=intent)
    rs.save_state(ctx.db, SESSION, state)


def _tool_ctx(
    ctx: AppContext,
    recorder: _Recorder,
    *,
    enabled: bool = True,
    turn_id: str = TURN,
) -> ToolContext:
    config = replace(
        ctx.config,
        runtime=replace(ctx.config.runtime, classify_search_intent=enabled),
        search_cfg=replace(ctx.config.search_cfg, providers=("arxiv", "github")),
    )
    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(recorder.handler))
    return ToolContext(
        db=ctx.db,
        config=config,
        http=http,
        llm=ctx.llm,
        session_id=SESSION,
        turn_id=turn_id,
        emit=recorder.emit,
    )


def _search(tool_ctx: ToolContext, query: str) -> dict[str, Any]:
    return research_search(tool_ctx, query, sources=["arxiv", "github"], limit=2)


# ------------------------------------------------------- 第一道：判用户原话（D3）


def test_generic_user_text_wins_over_a_specific_rewritten_query(ctx: AppContext) -> None:
    """D3 主场景：原话「帮我找点资料了解一下」+ 参数被改写成具体英文 → **仍然出卡**。

    这里刻意让分类器给高置信（``preference`` 已写成 ``other``）：分类器猜得自信，
    也改变不了一件事 —— 用户原话没说清要哪类资料。闸门先看原话，就不再有"两种行为"。
    """
    recorder = _Recorder()
    _seed(
        ctx,
        user_text="帮我找点资料了解一下",
        preference="other",
        intent=si.Intent("other", 0.9, "model"),
    )

    result = _search(_tool_ctx(ctx, recorder), REWRITTEN)

    assert recorder.urls == [], f"泛指请求不该出网（实际：{recorder.urls}）"
    assert result["ok"] is False and result["needs_choice"] is True
    assert len(result["options"]) == 7
    payload = recorder.choice_payload()
    assert payload["intent"]["source"] == "user_text"
    # D2：英文改写式一个字符都不进标题
    assert payload["title"] == si.CHOICE_QUESTION
    assert payload["query"] == "" and payload["subtitle"] == ""
    assert payload["query_raw"] == REWRITTEN  # 原检索式只留在事件里给排查用


def test_resolve_turn_records_the_user_text_for_the_gate(ctx: AppContext) -> None:
    """原话由轮次开头那条**已有**路径记进台账（不用调用方额外传参）。"""
    state = rs.empty_state("帮我找点资料了解一下")
    llm = StubLlm(reply(HIGH_CONFIDENCE_OTHER))
    si.resolve_turn(llm, "帮我找点资料了解一下", state=state, turn_id=TURN)

    assert state[si.USER_TEXT_KEY] == "帮我找点资料了解一下"
    assert state[si.USER_TEXT_TURN_KEY] == TURN


@pytest.mark.parametrize(
    ("user_text", "query"),
    [
        ("RAG 的核心步骤", "RAG core steps"),
        ("帮我找 2024 年 RAG 综述", "RAG survey 2024"),
    ],
)
def test_specific_request_is_not_blocked(ctx: AppContext, user_text: str, query: str) -> None:
    """有具体对象（点名了 RAG / 说了"综述"）→ 放行，真的去搜（不误伤）。"""
    recorder = _Recorder()
    _seed(ctx, user_text=user_text)

    result = _search(_tool_ctx(ctx, recorder), query)

    assert recorder.events == [], "具体请求不许被拦成选择卡"
    assert result["ok"] is True
    assert recorder.urls, "具体请求应该真的出网检索"


def test_explicit_choice_is_not_blocked(ctx: AppContext) -> None:
    """原话是「只看论文」= 用户已经选过了，不属于"没说清"，不许再问一遍。"""
    recorder = _Recorder()
    _seed(
        ctx,
        user_text="只看论文",
        preference="paper",
        intent=si.Intent("paper", 1.0, "explicit"),
    )

    result = _search(_tool_ctx(ctx, recorder), "education agent")

    assert recorder.events == []
    assert result["ok"] is True
    assert {httpx.URL(url).host for url in recorder.urls} == {"export.arxiv.org"}


def test_previous_turn_user_text_does_not_gate_this_turn(ctx: AppContext) -> None:
    """台账里那条原话属于**上一轮**（turn_id 不匹配）→ 不能拿来拦这一轮。"""
    recorder = _Recorder()
    _seed(ctx, user_text="帮我找点资料了解一下", turn_id="trn_old")

    result = _search(_tool_ctx(ctx, recorder), "education agent")

    assert recorder.events == []
    assert result["ok"] is True and recorder.urls


# ------------------------------------------------------- 第二道：判工具参数（兜底）


def test_generic_tool_query_is_blocked_without_any_verdict(ctx: AppContext) -> None:
    """本轮没有判定（分类器没跑/没留下 intent）→ 至少看住参数本身：泛指也拦。"""
    recorder = _Recorder()
    _seed(ctx)  # 台账里既没有原话也没有 intent

    result = _search(_tool_ctx(ctx, recorder), "帮我找点资料")

    assert recorder.urls == []
    assert result["needs_choice"] is True
    payload = recorder.choice_payload()
    assert payload["intent"]["source"] == "query"
    # 中文短句作为小字保留（用户自己的说法），标题仍是那句中文提问
    assert payload["title"] == si.CHOICE_QUESTION
    assert payload["subtitle"] == "帮我找点资料"


def test_switch_off_restores_the_old_behaviour(ctx: AppContext) -> None:
    """关掉开关 → 两道判据都不生效（消融/回滚的口子）。"""
    recorder = _Recorder()
    _seed(ctx, user_text="帮我找点资料了解一下")

    result = _search(_tool_ctx(ctx, recorder, enabled=False), REWRITTEN)

    assert recorder.events == []
    assert result["ok"] is True and recorder.urls


# ------------------------------------------------------- 标题清洗（D2，纯函数）


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (REWRITTEN, ""),  # 整句英文 → 兜底（界面只剩中文提问句）
        ("", ""),
        ("再找点资料", "再找点资料"),
        ("帮我找点资料。顺便看看教程", "帮我找点资料"),  # 截到第一个句末标点
        ("帮" + "我" * 60, "帮" + "我" * 39),  # 中文长句截到 40 字
        ("RAG 检索策略" * 12, ("RAG 检索策略" * 12)[:40]),
    ],
)
def test_choice_subject_never_dumps_the_raw_rewrite(query: str, expected: str) -> None:
    """卡片小字：英文改写式一律清空，中文单句 ≤40 字。"""
    subject = si.choice_subject(query)
    assert subject == expected
    assert len(subject) <= si.CHOICE_SUBJECT_MAX_CHARS


def test_choice_card_text_keeps_only_the_chinese_question() -> None:
    """``(标题, 副标题)``：标题永远只有那句中文提问，检索式降级为小字。"""
    assert si.choice_card_text(REWRITTEN) == (si.CHOICE_QUESTION, "")
    assert si.choice_card_text("再找点资料") == (si.CHOICE_QUESTION, "再找点资料")


# ------------------------------------------------------- 规则层正反例


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("帮我找点资料了解一下", True),
        ("找点资料", True),
        ("帮我看看", True),
        ("了解一下", True),
        ("RAG 的核心步骤", False),
        ("帮我找 2024 年 RAG 综述", False),
        ("只看论文", False),  # 已经选过了
        ("", False),
    ],
)
def test_requests_unclear_material_positive_and_negative(text: str, expected: bool) -> None:
    assert si.requests_unclear_material(text) is expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("帮我找点资料", True),
        ("education agent", False),
        ("x", False),  # 太短但无泛指词 → 归成本闸门管，不归这道
        ("帮我找点资料 about RAG", False),  # 点了名（拉丁词）就算说清了
        ("只看论文", False),
    ],
)
def test_looks_generic_query_positive_and_negative(query: str, expected: bool) -> None:
    assert si.looks_generic_query(query) is expected


def test_choice_event_payload_is_json_serialisable(ctx: AppContext) -> None:
    """事件走 JSON-RPC：清洗后的字段必须都能序列化（别塞进去奇怪的对象）。"""
    recorder = _Recorder()
    _seed(ctx, user_text="找点资料")

    _search(_tool_ctx(ctx, recorder), REWRITTEN)

    payload = recorder.choice_payload()
    json.dumps(payload, ensure_ascii=False)
    assert set(payload) >= {"turn_id", "session_id", "query", "query_raw", "title", "options"}
