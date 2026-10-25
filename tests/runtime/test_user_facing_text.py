"""工具失败时**喂回模型/用户的文案**不许带内部标识，内部标识只进 trace 与日志（缺陷 4）。

真机现象：模型在回答里写 `research.search`、`tool_not_loaded`、`FetchError` 这类东西。两个来源 ——
① 提示词没禁止（`runtime/prompts.py::USER_FACING_RULE`，哈希在 `test_prompt_arms.py` 里钉住）；
② **模型照抄工具失败文案**：`execute_tool` 把 `工具 {call.name} 未注册` / `工具 X 执行失败：{exc}`
喂回模型，模型再好心地转述给用户。所以文案侧也要只说人话。

**这里必须成对断言**（只测一边就会跑偏）：

- 只测"文本干净" → trace/日志里的码可能被一起改掉，事故后查不出是哪种失败（排障能力静默消失）；
- 只测"trace 有码" → 文案可能又悄悄把 `tool_not_registered` / 异常类名写回去，模型继续照抄。

反向断言用的正则 ``[a-z_]+\\.[a-z_]+|[A-Z][A-Za-z]+Error|tool_not_loaded`` 就是缺陷现象里那几类写法的形状：
工具名（`research.search`）、异常类名（`FetchError`）、闸的码（`tool_not_loaded`）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from scholarscout.config import Config
from scholarscout.fetch.contract import FetchError
from scholarscout.providers.llm import ChatReply, ToolCall
from scholarscout.runtime import registry
from scholarscout.runtime.subtasks import run_subtask
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.runtime.turn_context import TurnContext
from scholarscout.runtime.turn_steps import execute_tool
from scholarscout.state.db import Database
from scholarscout.tools import register_tools

from ..conftest import StubLlm, insert_session

#: 缺陷 4 里那几类写法的形状：`research.search` / `FetchError` / `tool_not_loaded`
INTERNAL_ID = re.compile(r"[a-z_]+\.[a-z_]+|[A-Z][A-Za-z]+Error|tool_not_loaded")


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """注册表是**进程内全局**：不清理会让前一条用例的工具漏到下一条。"""
    registry.clear_registry()
    yield
    registry.clear_registry()


def _turn(db: Database, config: Config) -> TurnContext:
    return TurnContext(db=db, config=config, llm=StubLlm())


def _execute(
    db: Database, config: Config, session_id: str, name: str, arguments: str = "{}"
) -> tuple[str, bool]:
    """跑一次 ``execute_tool``：``(喂回模型的文本, 是否成功)``。"""
    message, _refs, _canonical, ok = execute_tool(
        _turn(db, config),
        session_id,
        f"trn_{session_id}",
        1,
        ToolCall(id="call_1", name=name, arguments=arguments),
        None,
    )
    return message, ok


def _trace(db: Database, session_id: str) -> dict[str, Any]:
    row = db.query_one(
        "SELECT tool_name, error FROM tool_traces WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
        (session_id,),
    )
    assert row is not None, "失败也必须落一条 trace —— 否则事后查不出模型绕过声明调过什么"
    return dict(row)


# --------------------------------------------------------------------------- #
# 1. 工具不存在：喂回模型的是人话，trace 里仍是 tool_not_registered + 原名
# --------------------------------------------------------------------------- #


def test_unregistered_tool_text_is_free_of_internal_identifiers(
    db: Database, config: Config
) -> None:
    """模型点了一个不存在的工具 → 文案不许出现那个工具名，也不许出现内部码。

    文案里**不能**再写 `工具 ghost.tool 未注册`：那就是 `research.search` 进回答的直通车。
    但"未注册"这个**人话**要留着（`tests/rpc/test_session_methods.py` 也钉了它）——
    模型得知道"这个能力现在不可用"，而不是"工具坏了、再试一次"。
    """
    register_tools()
    insert_session(db, "ses_unreg")

    message, ok = _execute(db, config, "ses_unreg", "ghost.tool")

    assert ok is False
    assert "未注册" in message, "人话仍要留着：模型据此告诉用户能力不可用"
    assert "ghost.tool" not in message, "内部工具名只进 trace，不进喂回模型的文案"
    assert not INTERNAL_ID.search(message), f"文案里还有内部标识：{message!r}"
    # ↓ 反面对照：内部标识必须仍在 trace 里（排障与论文取数都靠它）
    row = _trace(db, "ses_unreg")
    assert row["tool_name"] == "ghost.tool"
    assert row["error"] == "tool_not_registered"


# --------------------------------------------------------------------------- #
# 2. 工具抛异常：文案不含异常类名，trace 里的 error 仍以类名开头
# --------------------------------------------------------------------------- #


def test_tool_crash_text_hides_the_exception_class_but_trace_keeps_it(
    db: Database, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具**抛异常**（不是回 ok=false）→ 文案只说"这一步没成功 + 原因"，类名只进 trace。

    真因：这一类异常最常见的就是 ``FetchError``，原文案是 `工具 source.fetch 执行失败：...`。
    异常类名对用户毫无意义，对排障却是唯一的分类依据 —— 所以两边分开：
    文案提原因（"站点拒绝了访问"这种），trace 提 ``FetchError: ...``。
    """

    def _boom(spec: object, ctx: object, arguments: object) -> dict[str, Any]:
        raise FetchError("forbidden", "站点拒绝了访问")

    register_tools()
    insert_session(db, "ses_crash")
    monkeypatch.setattr(registry, "invoke", _boom)

    message, ok = _execute(db, config, "ses_crash", "source.fetch", '{"source_id": "src_1"}')

    assert ok is False
    assert "站点拒绝了访问" in message, "原因要留着：模型得能向用户解释为什么没做成"
    assert "source.fetch" not in message, "内部工具名不许出现在喂回模型的文案里"
    assert not INTERNAL_ID.search(message), f"文案里还有内部标识：{message!r}"
    # ↓ 反面对照：类名与原因都还在 trace 里
    row = _trace(db, "ses_crash")
    assert row["tool_name"] == "source.fetch"
    assert row["error"] == "FetchError: 站点拒绝了访问"


# --------------------------------------------------------------------------- #
# 3. 子任务整体失败：text 不含类名/kind，日志里仍在
# --------------------------------------------------------------------------- #


class _CrashingLlm:
    """一调就炸的模型桩：用来逼出 ``run_subtask`` 的 error 分支。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    def chat(self, messages: list[dict[str, Any]], *, tools: object = None) -> ChatReply:
        self.calls += 1
        raise self._exc


def _subtask_ctx(db: Database, config: Config, llm: object, session_id: str) -> ToolContext:
    """与 ``tests/runtime/test_subtasks.py::_tool_ctx`` 同形状的子任务上下文。"""
    return ToolContext(
        db=db,
        config=replace(config, runtime=replace(config.runtime, subtask_enabled=True)),
        llm=llm,
        session_id=session_id,
        turn_id="trn_sub",
    )


def test_subtask_failure_text_hides_ids_while_the_log_keeps_them(
    db: Database, config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """子任务的 ``text`` 会被塞回主模型（也可能直接给用户看）→ 不许带类名；日志照留。

    原文案是 ``子任务执行失败：{type(exc).__name__}: {exc}`` —— 那个类名就是用户看到的
    `FetchError` 的另一条来路。改成"原因已记进日志"之后，排障靠的是 ``logger.warning``。
    """
    register_tools()
    insert_session(db, "ses_sub")
    ctx = _subtask_ctx(db, config, _CrashingLlm(RuntimeError("内部细节 boom")), "ses_sub")

    with caplog.at_level(logging.WARNING, logger="scholarscout.runtime.subtasks"):
        result = run_subtask(ctx, "子问题：XX 方法的代表论文")

    assert result.stop_reason == "error"
    assert result.text, "失败也要有话说 —— 空串会被主模型当成'什么都没查到'"
    assert not INTERNAL_ID.search(result.text), f"子任务文本里还有内部标识：{result.text!r}"
    assert "boom" not in result.text
    # ↓ 反面对照：日志里仍在（类名 + 原始信息）
    assert "RuntimeError" in caplog.text
    assert "内部细节 boom" in caplog.text
