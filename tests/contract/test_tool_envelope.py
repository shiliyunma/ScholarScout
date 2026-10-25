"""契约闸 3/4：**11 个模型可见工具的回包是同一个信封**（docs/01 §5.0；`py.exec` 见 docs/24，
`skill.list` / `skill.load` 是渐进式披露那一对，见 `skills/index.py`）。

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

import httpx
import pytest

from scholarscout.fetch.http import FetchError, HttpClient
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import registry
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools import delegate as delegate_tool
from scholarscout.tools import file_tools as file_tools_mod
from scholarscout.tools import py_exec as py_exec_tool
from scholarscout.tools import register_tools
from scholarscout.tools import research_search as search_tool
from scholarscout.tools import skills as skills_tool
from scholarscout.tools import source_fetch as fetch_tool
from scholarscout.tools import tool_catalog as catalog_tool
from scholarscout.tools.note_save import note_save
from scholarscout.tools.route_plan import route_plan
from scholarscout.tools.route_revise import route_revise
from scholarscout.tools.source_grade import source_grade
from scholarscout.tools.topic_map import topic_build_map

from ..conftest import StubLlm, insert_source, redirect_home, reply, tool_ctx

#: 信封顶层允许出现的键（docs/01 §5.0）。`refs` 可省，`error` 成功时可省。
ENVELOPE_KEYS = {"ok", "data", "error", "refs"}

#: 允许在 `data.note` 里带一句话说明的工具。
#:
#: `research.search`：带「这一轮为什么这么搜」（检索意图/回退路径），是给人看的解释。
#:
#: `tool.search` / `tool.describe`：**登记为例外（2026-10-25 决议）**。这两个是元工具，
#: 它们的 note 是**给模型的自救提示**，不是解释：
#: - `tool.search`：「目录只有名字与一句话，要参数就 describe（一次最多 3 个）」——
#:   模型看到目录但不知道下一步该干什么，就会拿着名字瞎猜参数；这句话就是那一步的唯一指路。
#: - `tool.describe`：「这些工具已加入本会话可用集，现在可以直接调用；新会话不继承」——
#:   不写，模型会怀疑自己的调用会不会被闸拦（或在下一轮重复 describe，白烧调用）。
#: 两者都是**产品行为**（渐进披露的核心可用性保证），不是调试残留，所以登记在允许名单里，
#: 不删产品里的 note；信封顶层形状（`ok`/`data`/`error`/`refs`）对它们仍一模一样，
#: 下面 `note is None or 非空字符串` 的约束照旧生效。
NOTE_ALLOWED = {"research.search", "tool.search", "tool.describe"}

#: **历史值**：2026-10-25 之前的产品默认集（11 个）。留着当对照面：`file.*` 是默认开**加**上来的、
#: `tool.*` 是披露开着才有的 —— 旧的 11 个必须仍在新集里（下面有子集断言）。
TOOL_NAMES_V11 = (
    "note.save",
    "py.exec",
    "research.delegate",
    "research.search",
    "route.plan",
    "route.revise",
    "skill.list",
    "skill.load",
    "source.fetch",
    "source.grade",
    "topic.build_map",
)

#: 本版**默认**注册的工具（`tools/__init__.register_tools()` 不带参数时的实际注册集，13 个）：
#: 冻结的 11 个 + 默认开的 `file.list` / `file.read`（docs/01 §3.16）。
#: `tool.search` / `tool.describe` **不在里面** —— 渐进披露默认关（A/B 实测开着会把整轮调用数
#: 19 → 25、冗余 7 → 15 而没有包含率收益），见 `TOOL_NAMES_WITH_DISCLOSURE`。
TOOL_NAMES = (
    "file.list",
    "file.read",
    "note.save",
    "py.exec",
    "research.delegate",
    "research.search",
    "route.plan",
    "route.revise",
    "skill.list",
    "skill.load",
    "source.fetch",
    "source.grade",
    "topic.build_map",
)

#: 披露开着时的注册集（15 个）：默认的 13 个 + 两个元工具（docs/01 §3.17）。
#: 两个集都要钉：只钉默认集会漏掉「披露一开元工具注册了没有」，只钉披露集会漏掉默认值回退。
TOOL_NAMES_WITH_DISCLOSURE = (
    "file.list",
    "file.read",
    "note.save",
    "py.exec",
    "research.delegate",
    "research.search",
    "route.plan",
    "route.revise",
    "skill.list",
    "skill.load",
    "source.fetch",
    "source.grade",
    "tool.describe",
    "tool.search",
    "topic.build_map",
)


class _DeadHttp:
    """出网桩：任何抓取都当作 404 失败 —— 用来确定性地走 `source.fetch` 的失败分支。"""

    def fetch(
        self,
        url: str,
        *,
        cache: bool = True,
        min_interval_s: float = 0.0,
        peek_media: bool = False,
    ) -> Any:
        raise FetchError("not_found", f"HTTP 404：{url}", url=url, status=404)


@pytest.fixture
def tc(ctx: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ToolContext:
    """真实装配 + 一条真来源；落盘重定向到 tmp，注册表按真实路径注册 11 个工具。"""
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


def _skill_list(tc: ToolContext) -> dict[str, Any]:
    """清单：tmp 家里没有 ``skills/`` → 走"空索引"那条真实分支（不是桩）。"""
    return skills_tool.skill_list(tc)


def _skill_load(tc: ToolContext) -> dict[str, Any]:
    """读一篇：名字不存在 → 必须是 ``ok:false`` 回包，不是异常、也不是 500。"""
    return skills_tool.skill_load(tc, name="no-such-skill")


def _py_exec(tc: ToolContext) -> dict[str, Any]:
    """执行器：**不带 approval** 调一次 → 必被审批门挡回 `needs_approval`。

    这道闸只验回包形状，所以走的是"还没拿到用户授权"那条真实分支 ——
    **不在这里真的起子进程**。真跑起来的那条（`ok:true` 与 `ok:false`）在
    `tests/tools/test_py_exec.py` 里，用工作目录里的固定小脚本 + 全桩。
    """
    return py_exec_tool.py_exec(tc, code='print("shape")')


def _file_list(tc: ToolContext) -> dict[str, Any]:
    """列目录：tmp 家里刚 `ensure_dirs()` 过 → 走"空/很短的目录"那条真实分支。"""
    return file_tools_mod.file_list(tc, path="data")


def _file_read(tc: ToolContext) -> dict[str, Any]:
    """读文件：路径不存在 → 必须是 `ok:false` + 可读 kind（不是异常、也不是 500）。"""
    return file_tools_mod.file_read(tc, path="data/no-such-file.md")


def _tool_search(tc: ToolContext) -> dict[str, Any]:
    """目录：只回名字 + 一句话，且不含元工具自己。"""
    return catalog_tool.tool_search(tc, query="route", limit=5)


def _tool_describe(tc: ToolContext) -> dict[str, Any]:
    """取 schema：`route.plan` 在默认工具集里 → `ok:true` 且带回完整 parameters。"""
    return catalog_tool.tool_describe(tc, names=["route.plan"])


#: 现在就是绿的这些工具（默认注册集 13 个里的 12 个 + 只有披露开着才存在的 2 个；
#: `route.revise` 的样本在下面单独钉）：名字 → 桩调用（每个都真实进 handler）
SAMPLES = {
    "research.search": _search,
    "source.fetch": _fetch,
    "source.grade": _grade,
    "topic.build_map": _topic_map,
    "route.plan": _route_plan,
    "note.save": _note_save,
    "research.delegate": _delegate,
    "py.exec": _py_exec,
    "skill.list": _skill_list,
    "skill.load": _skill_load,
    "file.list": _file_list,
    "file.read": _file_read,
    "tool.search": _tool_search,
    "tool.describe": _tool_describe,
}


def test_registry_matches_the_documented_tool_set(tc: ToolContext) -> None:
    """注册集就是默认那 13 个：不多不少、不重名（名字是 `docs/01 §5` + `docs/24` 的索引）。"""
    names = register_tools()
    assert names == sorted(TOOL_NAMES), f"注册集与契约不一致：{names}"
    assert set(TOOL_NAMES_V11) < set(names), "默认开了 file.*，但旧的 11 个一个都不该消失"
    assert len(names) == len(set(names)), f"注册集里有重名：{names}"

    # 披露开着时**只多**两个元工具（默认关不许把 tool.* 留在注册集里）。
    # 注册表是全局的：先清空再注册，测完再清空并恢复默认集，否则这一条会把 tool.* 漏给同进程里的其它用例。
    registry.clear_registry()
    with_disclosure = register_tools(enable_tool_disclosure=True)
    assert with_disclosure == sorted(TOOL_NAMES_WITH_DISCLOSURE), (
        f"披露开着时的注册集不对：{with_disclosure}"
    )
    assert set(with_disclosure) == set(names) | {"tool.describe", "tool.search"}
    registry.clear_registry()
    assert register_tools() == names, "恢复默认集失败（后续用例会看到披露臂的注册集）"


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_tool_returns_the_shared_envelope(
    name: str, tc: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """13 个工具的样本回包同形（成败两条分支由各自的桩决定，形状只有一种）。"""
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
        ("skill.load", _skill_load, "没有名为"),
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


# ------------------------------------------------------- 迁移前后逐键相等（换写法不换形状）

#: 迁移**前**（HEAD 里手写字典字面量）各分支的键序。
#: 逐条抄自 `HEAD:src/scholarscout/tools/{source_fetch,route_revise,delegate}.py` 的 `return {...}`：
#: 引入构造器只许换写法，键集与键序必须一字不动 —— 前端与模型都按这些键读。
PRE_REFACTOR_KEY_ORDER: dict[str, dict[str, list[str]]] = {
    "source.fetch/失败": {
        "envelope": ["ok", "error", "data", "refs"],
        "data": ["requested_url", "fetched_url", "used_landing_page", "error_kind", "status"],
        "refs": [],
    },
    "source.fetch/成功": {
        "envelope": ["ok", "data", "refs"],
        "data": [
            "doc_id",
            "source_id",
            "mime",
            "chars",
            "lang",
            "bytes",
            "ext",
            "raw_path",
            "text_path",
            "requested_url",
            "fetched_url",
            "used_landing_page",
            "extract_status",
            "extract_engine",
            "extract_detail",
            "truncated",
            "cached",
            "summary",
        ],
        "refs": ["source_ids", "doc_ids", "summary_ids"],
    },
    "route.revise/被拒": {
        "envelope": ["ok", "error", "data", "refs"],
        "data": ["route_id", "changes"],
        "refs": ["route_ids"],
    },
    "research.delegate/失败": {
        "envelope": ["ok", "error", "data", "refs"],
        "data": ["items", "counts", "consumed_tool_calls"],
        "refs": ["source_ids"],
    },
}

#: 抓取成功用的一页正文（够长才会走"抽取 + 摘要"那条完整成功分支）
PAGE = (
    "<html lang='zh'><head><title>信封闸</title></head><body><main>"
    + "<p>教育智能体先检索再作答，正文落盘、摘要入库，回包的键序不许变。</p>" * 20
    + "</main></body></html>"
)


def _assert_same_key_order(case: str, envelope: dict[str, Any]) -> None:
    """一边对齐基线，一边报出到底哪一层漂了（只看键序，不看值）。"""
    baseline = PRE_REFACTOR_KEY_ORDER[case]
    assert list(envelope) == baseline["envelope"], f"{case}：信封键序变了"
    assert list(envelope["data"]) == baseline["data"], f"{case}：data 键序变了"
    assert list(envelope["refs"]) == baseline["refs"], f"{case}：refs 键序变了"


def test_source_fetch_failure_keeps_the_pre_refactor_shape(tc: ToolContext) -> None:
    """抓取失败（404 → 工具内兜成 `ok:false`）：键集/键序与迁移前逐键相同。"""
    envelope = _fetch(tc)
    _assert_same_key_order("source.fetch/失败", envelope)
    assert envelope["ok"] is False and envelope["refs"] == {}


def test_source_fetch_success_keeps_the_pre_refactor_shape(tc: ToolContext) -> None:
    """抓取成功（真抽取 + 落盘，摘要因无模型标 unavailable）：键集/键序与迁移前逐键相同。"""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"}
        )
    )
    stub = replace(tc, http=HttpClient(respect_robots=False, transport=transport), llm=None)

    envelope = fetch_tool.source_fetch(stub, url="https://docs.example.com/envelope")

    _assert_same_key_order("source.fetch/成功", envelope)
    assert envelope["ok"] is True
    assert "error" not in envelope, "成功信封不许带 error（连 null 都不带）"


def test_route_revise_rejection_keeps_the_pre_refactor_shape(tc: ToolContext) -> None:
    """补丁不合法 → `ok:false` + `error`：失败信封的键集/键序与迁移前逐键相同。"""
    created = _route_plan(tc)
    route_id = str(created["data"]["route_id"])
    tc.llm = StubLlm(reply('{"summary": "乱改", "ops": [{"op": "set_week_field"}]}'))

    envelope = route_revise(tc, route_id, "把第 1 周改成别的")

    _assert_same_key_order("route.revise/被拒", envelope)
    assert envelope["ok"] is False
    # 这条用例传入了有效 route_id，被拒时 refs 应当原样带着它（不是空数组）
    assert envelope["refs"] == {"route_ids": [route_id]}, "被拒时 refs 应带着传入的 route_id"


def test_delegate_failure_keeps_the_pre_refactor_shape(tc: ToolContext) -> None:
    """子任务总开关未打开 → 失败信封；连 `data.counts` 的内层键序也与迁移前一致。"""
    envelope = _delegate(tc)

    _assert_same_key_order("research.delegate/失败", envelope)
    assert envelope["ok"] is False
    assert list(envelope["data"]["counts"]) == ["requested", "ran", "failed"]
    assert envelope["refs"] == {"source_ids": []}
