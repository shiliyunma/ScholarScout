"""D-3 检索偏好位闸门：四条路径 + 「工具声明/系统提示零变更」。

四条路径（口径见 ``runtime/search_intent.py`` 模块 docstring 与 ``docs/01``）：

| 情况 | 期望 |
|---|---|
| 有偏好（高置信判定写进台账） | 只查该族的 provider，返回 ``note`` 说明跳过了谁 |
| 无偏好 + 低置信 | **不搜**，回 ``needs_choice`` + 选项，并发 ``chat.needs_choice`` |
| 偏好 = ``none`` | **不搜**，回 ``skipped`` |
| 分类器报错 | 当 ``other`` **照常全量检索**（失败开放） |

为什么这份测试要盯「零变更」：D-3 的全部收益（省钱、少搜错）都建立在**不动对外契约**上 ——
工具声明和系统提示一字未改，所以 ``system_prompt_hash`` 不变，历史回答与旧报告仍可比。
一旦有人为了"让模型更懂意图"去改 ``DESCRIPTION`` / ``PARAMETERS`` / ``build_system_prompt``，
这条断言会立刻红，提醒他那是**另一个实验条件**，得按对照实验的纪律来。

全离线：arXiv 用 ``tests/fixtures/arxiv_sample.xml``，GitHub 用一行 JSON 桩。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.providers.settings import KNOWN
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import research_state as rs
from scholarscout.runtime import search_intent as si
from scholarscout.runtime.prompts import build_system_prompt, system_prompt_hash
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools import register_tools
from scholarscout.tools.research_search import DESCRIPTION, PARAMETERS, SPEC, research_search

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "arxiv_sample.xml"
SESSION = "ses_intent_gate"
TURN = "trn_1"

#: 引入 D-3 **之前**（HEAD 6a163dd）的对外契约指纹。``prompts.py`` 与工具声明在本批里
#: 一行都没改，所以这三个值就是"没变"的证据；改了它们 = 换了实验条件，必须显式改这里并写理由。
GOLDEN_PARAMETERS_SHA256 = "11e4d85e83153695781140fbdf12acee7947331fb109ba31700aa268b8982a26"
GOLDEN_DESCRIPTION_SHA256 = "dc67f09324a9da9de6d6a310e54a8be3224d16ea3b6423fcf8feaa4851f144a9"
GOLDEN_WIRE_SHA256 = "98486611147a98362abecc9ac644a42a0303a3e53c6e476c236cb07967873016"
#: ``system_prompt_hash(build_system_prompt(toolsets=["research"]))``（同 HEAD 基线）
#: 历史值：`b9ac0c043bf2`（2026-10-22 加 `PY_EXEC_RULE` 那一版，工具集 8 → 9）。
#: 2026-10-22 再更新：**新增 `skill.list` / `skill.load`**（工具集 9 → 11）带来 `SKILL_RULE`，
#: 这是**有意识的契约变更**，不是为凑绿改值；D-3 的四条路径本身一行未改。
GOLDEN_PROMPT_HASH = "20b7b97e452e"

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


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


class _Recorder:
    """记下**真的发了哪些请求**、**真的推了哪些事件**（断言"没搜"要看前者）。"""

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

    def hosts(self) -> set[str]:
        return {httpx.URL(url).host for url in self.urls}


def _tool_ctx(
    ctx: AppContext,
    recorder: _Recorder,
    *,
    preference: str | None = None,
    intent: si.Intent | None = None,
    enabled: bool = True,
) -> ToolContext:
    """按**生产路径**布置：偏好位先写进研究台账，工具再从台账读（不是直接塞 ctx.extra）。"""
    state = rs.empty_state("教育智能体怎么入门")
    si.set_preference(state, preference=preference, turn_id=TURN, intent=intent)
    rs.save_state(ctx.db, SESSION, state)

    config = replace(
        ctx.config,
        runtime=replace(ctx.config.runtime, classify_search_intent=enabled),
        # 两个不同族（论文 / 代码），才看得出"按族筛"到底筛掉了谁
        search_cfg=replace(ctx.config.search_cfg, providers=("arxiv", "github")),
    )
    http = HttpClient(respect_robots=False, transport=httpx.MockTransport(recorder.handler))
    return ToolContext(
        db=ctx.db,
        config=config,
        http=http,
        llm=ctx.llm,
        session_id=SESSION,
        turn_id=TURN,
        emit=recorder.emit,
    )


def _status_names(result: dict[str, Any]) -> list[str]:
    return [str(entry["provider"]) for entry in result["data"]["provider_status"]]


# ------------------------------------------------------------------ 路径 1：高置信


@pytest.mark.parametrize(
    ("kind", "expected_provider", "expected_host", "dropped"),
    [
        ("paper", "arxiv", "export.arxiv.org", "github"),
        ("code", "github", "api.github.com", "arxiv"),
    ],
)
def test_confident_intent_searches_only_its_family(
    ctx: AppContext, kind: str, expected_provider: str, expected_host: str, dropped: str
) -> None:
    """高置信 → 只查该族；跳过的来源要写进 ``note``（否则模型不知道"为什么只搜到论文"）。"""
    recorder = _Recorder()
    intent = si.Intent(kind, 0.9, "model")
    tool_ctx = _tool_ctx(ctx, recorder, preference=kind, intent=intent)

    result = research_search(tool_ctx, "education agent", sources=["arxiv", "github"], limit=2)

    assert result["ok"] is True
    assert _status_names(result) == [expected_provider]
    # 真的只发了这一家的请求（provider_status 是自报，URL 才是证据）
    assert recorder.hosts() == {expected_host}
    note = result["data"]["note"]
    assert note is not None and dropped in note and si.label_of(kind) in note
    assert result.get("skipped") is None and result.get("needs_choice") is None


# ------------------------------------------------------------------ 路径 2：低置信


def test_low_confidence_does_not_search_and_offers_choices(ctx: AppContext) -> None:
    """低置信 → 不猜也不搜：回 ``needs_choice`` + 7 个选项，并把选择卡事件推给界面。"""
    recorder = _Recorder()
    intent = si.Intent("other", 0.2, "model")
    tool_ctx = _tool_ctx(ctx, recorder, preference=None, intent=intent)

    result = research_search(tool_ctx, "帮我找点资料", sources=["arxiv", "github"], limit=2)

    assert recorder.urls == []  # 一个请求都没发
    assert result["ok"] is False
    assert result["needs_choice"] is True
    assert len(result["options"]) == 7
    assert {option["type"] for option in result["options"]} == set(si.PREFERENCES)
    data = result["data"]
    assert data["needs_choice"] is True and data["items"] == []
    assert data["intent"] == {
        "type": "other",
        "confidence": 0.2,
        "source": "model",
        "failed": False,
    }
    assert "问用户" in data["note"]  # 模型要转述给用户，而不是自己替他选

    kinds = [kind for kind, _ in recorder.events]
    assert kinds == ["chat.needs_choice"]
    # 事件名必须登记（漏登记会被服务端加 chat. 前缀 → 变成 chat.chat.needs_choice，界面永远收不到）
    from scholarscout.rpc.server import NOTIFICATION_METHODS

    assert "chat.needs_choice" in NOTIFICATION_METHODS
    _, payload = recorder.events[0]
    assert payload["turn_id"] == TURN and payload["session_id"] == SESSION
    assert len(payload["options"]) == 7
    assert payload["query"] == "帮我找点资料"


# ------------------------------------------------------------------ 路径 3：none


def test_none_preference_skips_search_entirely(ctx: AppContext) -> None:
    """用户说"别搜了" → 零出网、零 token，并明确告诉模型"直接用已有资料回答"。"""
    recorder = _Recorder()
    tool_ctx = _tool_ctx(
        ctx, recorder, preference="none", intent=si.Intent("none", 1.0, "explicit")
    )

    result = research_search(tool_ctx, "教育智能体的定义", sources=["arxiv", "github"], limit=2)

    assert recorder.urls == []
    assert recorder.events == []
    assert result["ok"] is False
    assert result["skipped"] is True
    assert result["data"]["skipped"] is True and result["data"]["items"] == []
    assert "不检索" in result["data"]["note"]
    assert result["refs"] == {"source_ids": []}


# ------------------------------------------------------------------ 路径 4：分类器报错


def test_classifier_failure_falls_back_to_the_full_provider_set(ctx: AppContext) -> None:
    """失败开放：分类挂了就照旧全量搜（``failed`` 只留在台账里，不阻断检索）。"""
    recorder = _Recorder()
    intent = si.Intent("other", 0.0, "fallback", failed=True)
    tool_ctx = _tool_ctx(ctx, recorder, preference=None, intent=intent)

    result = research_search(tool_ctx, "education agent", sources=["arxiv", "github"], limit=2)

    assert result["ok"] is True
    assert _status_names(result) == ["arxiv", "github"]
    assert recorder.hosts() == {"export.arxiv.org", "api.github.com"}
    # 没筛来源就不该有"已按…偏好只查…"的 note，也不能冒充"问用户"
    assert result["data"]["note"] is None
    assert result.get("needs_choice") is None and result.get("skipped") is None


def test_switch_off_restores_pre_intent_behaviour(ctx: AppContext) -> None:
    """关掉开关 → 完全回到引入前：低置信也不问、不过滤（这是消融/回滚的口子）。"""
    recorder = _Recorder()
    tool_ctx = _tool_ctx(
        ctx,
        recorder,
        preference=None,
        intent=si.Intent("other", 0.1, "model"),
        enabled=False,
    )

    result = research_search(tool_ctx, "帮我找点资料", sources=["arxiv", "github"], limit=2)

    assert result.get("needs_choice") is None
    assert _status_names(result) == ["arxiv", "github"]
    assert recorder.events == []


# ------------------------------------------------------------------ 零变更（契约指纹）


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_tool_declaration_is_byte_identical() -> None:
    """``research.search`` 的声明（描述 + 参数 schema + 发给模型的线上形态）一字未改。"""
    canonical = json.dumps(PARAMETERS, ensure_ascii=False, sort_keys=True)
    assert _sha256(canonical) == GOLDEN_PARAMETERS_SHA256
    assert _sha256(DESCRIPTION) == GOLDEN_DESCRIPTION_SHA256
    # SPEC 必须**就是**这两个对象（不是一份复制品：复制品改了会与声明悄悄分叉）
    assert SPEC.description == DESCRIPTION
    assert SPEC.parameters == PARAMETERS
    assert PARAMETERS["required"] == ["query"]
    assert PARAMETERS["properties"]["sources"]["items"]["enum"] == list(KNOWN)
    wire = json.dumps(SPEC.to_openai(), ensure_ascii=False, sort_keys=True)
    assert _sha256(wire) == GOLDEN_WIRE_SHA256


def test_system_prompt_is_byte_identical_even_with_a_preference_on_the_ledger(
    ctx: AppContext,
) -> None:
    """系统提示逐字不变 —— 偏好位只进台账，进提示就会让 ``system_prompt_hash`` 随点选漂移。"""
    prompt = build_system_prompt(toolsets=["research"])
    assert system_prompt_hash(prompt) == GOLDEN_PROMPT_HASH

    # 台账里已经有偏好位时，提示词与"什么选项都没点过"逐字相同
    state = rs.empty_state("教育智能体怎么入门")
    si.set_preference(
        state, preference="paper", turn_id=TURN, intent=si.Intent("paper", 0.9, "model")
    )
    rs.save_state(ctx.db, SESSION, state)
    assert build_system_prompt(toolsets=["research"]) == prompt

    # 偏好位也不能从"随轮注入"的台账摘要里漏进提示词
    note = rs.render_note(rs.load_state(ctx.db, SESSION) or {}, [])
    for key in (si.PREFERENCE_KEY, si.INTENT_KEY):
        assert key not in note
        assert key not in prompt
    assert "needs_choice" not in prompt
