"""契约闸 3/4：**8 个模型可见工具的回包是同一个信封**（docs/01 §5.0）。

为什么这道闸必须有：模型是照着 `refs` / `ok` / `error` 读工具结果的。
任何一个工具改回扁平 dict，模型就看到「有内容但没有 ok」——它要么把失败当成功，
要么把成功当失败重试，两种都会烧钱且给出错结论（`route.revise` 现在就是这种）。

信封规则（docs/01 §5.0）：

- 顶层只许 `ok` / `data` / `error` / `refs` 四个键（`refs` 可省，`error` 成功时可省）；
- `ok` 必须是布尔，`data` 必须是对象 —— 成功/失败**同形**，调用方不必分支；
- 失败：`ok is False` 且 `error` 是非空字符串（不许只回 `data.items=[]` 让模型猜）；
- 成功：`ok is True` 且不带非空 `error`（两个都在 = 前端当成功吞掉错误）；
- `note` **只在 `data` 里**出现（信封顶层的 `note` 会被当成 `refs` 之外的多余字段），
  且只有 `research.search` 会在检索来源被收窄/跳过时带它 —— 其余工具出现 `note` 说明它在临时加字段。

本闸**不真出网、不真调模型**：出网用 `_DeadHttp`（必抛 `FetchError`）、
检索扇出用空结果、模型要么不给（`use_model=False`）要么用 `StubLlm` 脚本。
所有调用都是真实 handler + 真实 `ToolSpec`，只把边界依赖换成桩 —— 打桩的是 I/O，不是被测的形状。

现状：8 个全绿。`route.revise` 曾经是唯一不同形的（扁平 dict + `raise ValueError`），
已于 2026-10-19 收敛成 `{ok, data, refs}`（失败 `ok:false` + `error`，不抛）。
新增工具时**先加到下面的 `SAMPLES`**，这道闸才会真的管到它。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scholarscout.fetch.http import FetchError
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools import delegate as delegate_tool
from scholarscout.tools import register_tools
from scholarscout.tools import research_search as search_tool
from scholarscout.tools import source_fetch as fetch_tool
from scholarscout.tools.note_save import note_save
from scholarscout.tools.route_plan import route_plan
from scholarscout.tools.route_revise import route_revise
from scholarscout.tools.source_grade import source_grade
from scholarscout.tools.topic_map import topic_build_map

from ..conftest import StubLlm, insert_source, redirect_home, reply, tool_ctx

#: 信封顶层允许出现的键（docs/01 §5.0）。`refs` 可省，`error` 成功时可省。
ENVELOPE_KEYS = {"ok", "data", "error", "refs"}

#: 唯一会在 `data.note` 里带「这一轮为什么这么搜」的工具
NOTE_ALLOWED = {"research.search"}

#: 本版注册的工具（`tools/__init__.register_tools()` 的实际注册集，8 个）
TOOL_NAMES = (
    "note.save",
    "research.delegate",
    "research.search",
    "route.plan",
    "route.revise",
    "source.fetch",
    "source.grade",
    "topic.build_map",
)


class _DeadHttp:
    """出网桩：任何抓取都当作 404 失败 —— 用来确定性地走 `source.fetch` 的失败分支。"""

    def fetch(self, url: str, *, cache: bool = True) -> Any:
        raise FetchError("not_found", f"HTTP 404：{url}", url=url, status=404)


@pytest.fixture
def tc(ctx: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ToolContext:
    """真实装配 + 一条真来源；落盘重定向到 tmp，注册表按真实路径注册 8 个工具。"""
    redirect_home(monkeypatch, tmp_path)
    insert_source(ctx.db)
    register_tools()
    return tool_ctx(ctx)


def _assert_envelope(name: str, envelope: Any) -> None:
    """信封同形的唯一判定处（所有断言都从这里出，避免各处口径漂移）。"""
    assert isinstance(envelope, dict), f"{name} 回的不是 dict：{type(envelope).__name__}"
    extra = set(envelope) - ENVELOPE_KEYS
    assert extra == set(), f"{name} 回包出现信封之外的顶层键：{sorted(extra)}"
    assert "ok" in envelope, f"{name} 回包缺 ok（模型无法判断成败）"
    assert isinstance(envelope["ok"], bool), f"{name} 的 ok 不是布尔：{envelope['ok']!r}"
    assert "data" in envelope, f"{name} 回包缺 data"
    assert isinstance(envelope["data"], dict), f"{name} 的 data 不是对象"
    assert "note" not in envelope, f"{name} 把 note 放到了信封顶层（约定放 data.note）"

    if envelope["ok"]:
        assert not envelope.get("error"), f"{name} 成功却带了 error：{envelope.get('error')!r}"
    else:
        error = envelope.get("error")
        assert isinstance(error, str) and error.strip(), (
            f"{name} 失败必须给非空 error（不许只回空 data 让模型猜）"
        )
        assert error.strip() != "None", f"{name} 的 error 是字符串化的 None"

    data = envelope["data"]
    if "note" in data:
        assert name in NOTE_ALLOWED, f"{name} 的 data 里出现 note，但约定只有检索工具带它"
        note = data["note"]
        assert note is None or (isinstance(note, str) and note.strip()), (
            f"{name} 的 data.note 只能是 None 或非空字符串"
        )

    refs = envelope.get("refs")
    if refs is not None:
        assert isinstance(refs, dict), f"{name} 的 refs 不是对象"
        for key, values in refs.items():
            assert isinstance(values, list), f"{name} 的 refs.{key} 不是数组"
            for value in values:
                assert isinstance(value, str) and value, f"{name} 的 refs.{key} 里有空 id"


def _search(tc: ToolContext, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """检索：扇出桩成空表 → 走「没有任何可用来源」的失败分支（不真出网）。"""
    monkeypatch.setattr(search_tool, "fetch_many", lambda *args, **kwargs: [])
    return search_tool.research_search(replace(tc, http=_DeadHttp()), "教育智能体 检索增强")


def _fetch(tc: ToolContext) -> dict[str, Any]:
    """抓取：出网桩恒 404 → 工具内就地兜成 `ok:false`（这正是不许冒泡的那条铁律）。"""
    return fetch_tool.source_fetch(
        replace(tc, http=_DeadHttp()), url="https://example.invalid/a.pdf"
    )


def _grade(tc: ToolContext) -> dict[str, Any]:
    return source_grade(tc, source_ids=["src_test0001"], use_model=False)


def _topic_map(tc: ToolContext) -> dict[str, Any]:
    return topic_build_map(tc, "教育智能体", use_model=False)


def _route_plan(tc: ToolContext) -> dict[str, Any]:
    return route_plan(tc, "教育智能体", weeks=2, use_model=False)


def _note_save(tc: ToolContext) -> dict[str, Any]:
    return note_save(tc, kind="note", title="信封闸", content="工具回包必须同形。")


def _delegate(tc: ToolContext) -> dict[str, Any]:
    return delegate_tool.research_delegate(tc, ["教育智能体的检索增强有哪些代表工作"])


#: 现在就是绿的 7 个工具：名字 → 桩调用（每个都真实进 handler）
SAMPLES = {
    "research.search": _search,
    "source.fetch": _fetch,
    "source.grade": _grade,
    "topic.build_map": _topic_map,
    "route.plan": _route_plan,
    "note.save": _note_save,
    "research.delegate": _delegate,
}


def test_registry_matches_the_documented_tool_set(tc: ToolContext) -> None:
    """注册集就是这 8 个：不多不少、不重名（名字是 `docs/01 §5` 的唯一索引）。"""
    names = register_tools()
    assert names == sorted(TOOL_NAMES), f"注册集与契约不一致：{names}"
    assert len(names) == len(set(names)), f"注册集里有重名：{names}"


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_tool_returns_the_shared_envelope(
    name: str, tc: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7 个工具的回包同形（成败两条分支由各自的桩决定，形状只有一种）。"""
    envelope = SAMPLES[name](tc, monkeypatch) if name == "research.search" else SAMPLES[name](tc)
    _assert_envelope(name, envelope)


def test_failure_samples_really_are_failures(
    tc: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败样本必须**真的是** `ok:false`：否则「失败分支同形」这条闸是空转。"""
    for name, sample, expected in (
        ("research.search", _search, "没有任何可用的检索来源"),
        ("source.fetch", _fetch, "404"),
        ("research.delegate", _delegate, "子任务总开关未打开"),
    ):
        envelope = sample(tc, monkeypatch) if name == "research.search" else sample(tc)
        assert envelope["ok"] is False, f"{name} 的失败样本实际回了 ok:true"
        assert expected in str(envelope["error"]), (
            f"{name} 的 error 没说到点子上：{envelope['error']!r}（期望含 {expected!r}）"
        )


def test_success_samples_carry_no_error_and_real_refs(tc: ToolContext) -> None:
    """成功样本：`error` 缺席/空，且 `refs` 里的 id 都是非空字符串（可追溯性）。"""
    for name, sample in (
        ("source.grade", _grade),
        ("route.plan", _route_plan),
        ("note.save", _note_save),
    ):
        envelope = sample(tc)
        assert envelope["ok"] is True, f"{name} 的成功样本没成功：{envelope.get('error')}"
        assert not envelope.get("error")
        refs = envelope.get("refs") or {}
        assert refs, f"{name} 成功却没回 refs（回答里的 id 就无从追溯）"


def test_route_revise_returns_the_shared_envelope(tc: ToolContext) -> None:
    """`route.revise` 也必须回信封（与另外 7 个同形）。"""
    created = _route_plan(tc)
    route_id = str(created["data"]["route_id"])
    tc.llm = StubLlm(
        reply(
            '{"summary": "每周时长改成 8 小时",'
            ' "ops": [{"op": "set_plan_field", "field": "weekly_hours", "value": 8}]}'
        )
    )
    envelope = route_revise(tc, route_id, "每周时长改成 8 小时")
    _assert_envelope("route.revise", envelope)
    assert envelope["ok"] is True
    assert envelope["data"]["version"] == 2
    assert envelope["refs"]["route_ids"] == [route_id]


def test_route_revise_failure_is_an_envelope_not_an_exception(tc: ToolContext) -> None:
    """补丁不合法时**不抛**，回 `ok:false` + 非空 `error`（模型据此换一种改法重试）。

    这是这条 xfail 的另一半：原来 `raise ValueError` 会打断整轮对话，模型连"错在哪"都看不到。
    """
    created = _route_plan(tc)
    route_id = str(created["data"]["route_id"])
    tc.llm = StubLlm(reply('{"summary": "乱改", "ops": [{"op": "set_week_field"}]}'))

    envelope = route_revise(tc, route_id, "把第 1 周改成别的")
    _assert_envelope("route.revise", envelope)
    assert envelope["ok"] is False
    assert "被拒绝" in envelope["error"]
    assert envelope["data"]["changes"] == []
