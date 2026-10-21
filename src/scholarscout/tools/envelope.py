"""工具结果信封的类型与**唯一构造点**（契约见 `docs/01 §5.0`）。

为什么要有这个模块：`{"ok": true, "data": {...}, "refs": {...}}` 这种回包原先在代码里
散写了 30+ 处（`tools/` 8 个模块 + rpc/channels/fetch 的非工具回执），键集靠人记：
有人写 `ok/error/data`，有人写 `ok/skipped/data`，有人失败时连 `error` 都不带 ——
契约闸只能**事后**发现，管不住构造点。这里把「成功长什么样、失败长什么样」收成两个
`TypedDict` 与两个构造器，迁移过的模块不再手写字典。

**回包的对外形状是本项目的硬契约**（`tests/contract/test_tool_envelope.py` 与前端都按它读），
所以两个构造器只做「按固定键序拼装」，不做任何补键、改名、字符串化：

- 成功：`{"ok": True, "data": {...}, "refs": {...}}`（键序就是 `ok, data, refs`）
- 失败：`{"ok": False, "error": "非空文案", "data": {...}, "refs": {...}}`

`refs` 一律出现（没引用就是 `{}`）—— 与现有 8 个工具的实际回包逐键一致；
`error` 成功时**不出现**（不是 `null`），这一点也照现有代码，别照 docs/01 那张示例图的
`"error": null`（示例比代码宽，闸以代码为准）。

**为什么这里放行三个异形顶层键（已知例外，不是先例）**：`research.search` 的三个早退分支
还带 `skipped` / `needs_choice` / `options` 顶层键 —— 界面正拿它们渲染「这轮没搜 / 你来选」
两种状态卡（`tests/desktop/test_choice_card.py` 与 `test_search_intent_gate.py` 都钉着），
把它们挪进 `data` 就是**改对外形状 + 改前端**，不在本批范围。所以这里把它们列进
:data:`KNOWN_EXTRA_KEYS` 显式放行，其余未知顶层键仍然直接报错 —— 例外有名单、有原因，
新增工具不许再往顶层加键（要交给模型的东西一律进 `data`）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal, Required, TypedDict

#: 引用表：`{"source_ids": [...], "doc_ids": [...], ...}`（任何进入回答的 id 都要回填在这里）
Refs = dict[str, list[str]]


class _Unset:
    """哨兵类型：区分「调用方没传 note」与「显式传了 None」。"""

    __slots__ = ()


_UNSET: Final = _Unset()


class _ExtraKeys(TypedDict, total=False):
    """已知例外的三个额外顶层键（见模块顶注）。拼装时按这个顺序追加在 `refs` 之后。"""

    skipped: bool
    needs_choice: bool
    options: list[dict[str, Any]]


class OkEnvelope(TypedDict, total=False):
    """成功回包：`ok` / `data` 必有，`refs` 由构造器补齐（缺省 `{}`）。"""

    ok: Required[Literal[True]]
    data: Required[dict[str, Any]]
    refs: Refs
    skipped: bool
    needs_choice: bool
    options: list[dict[str, Any]]


class ErrorEnvelope(TypedDict, total=False):
    """失败回包：`error` 必须是非空人话（模型据此换法重试），不抛异常。"""

    ok: Required[Literal[False]]
    error: Required[str]
    data: Required[dict[str, Any]]
    refs: Refs
    skipped: bool
    needs_choice: bool
    options: list[dict[str, Any]]


#: 工具 handler 的返回类型（成败两支同形，调用方不必先判分支）
ToolResult = OkEnvelope | ErrorEnvelope

#: 信封顶层允许的**已知例外**（顺序即拼装顺序，保证键序稳定）。
#: 只有 `research.search` 的三个早退分支会用；前端在读，所以放行而不是报错。
#: 这是名单，不是先例：新工具往顶层加键一律报错（见 :func:`_reject_unknown`）。
KNOWN_EXTRA_KEYS: Final = ("skipped", "needs_choice", "options")


def ok(
    data: Mapping[str, Any] | None = None,
    *,
    note: str | _Unset | None = _UNSET,
    **extra: Any,
) -> OkEnvelope:
    """拼一个成功信封。`extra` 只认 `refs` 与 :data:`KNOWN_EXTRA_KEYS`（其余＝契约变更，报错）。

    `note` 是「这一轮为什么这么搜/这么判」的说明，按约定放在 **`data.note`**，
    永不放到信封顶层（顶层多一个键就是改形状）。不传 `note` 时不动 `data`；
    显式传 `None` 会写出 `data["note"] = None` —— 有些工具的成功回包一直带这个 null 键。
    """
    envelope: OkEnvelope = {
        "ok": True,
        "data": _payload(data, note),
        "refs": _refs(extra.pop("refs", None)),
        **_known_extras(extra),
    }
    _reject_unknown("ok", extra)
    return envelope


def fail(
    code: str,
    message: str,
    *,
    data: Mapping[str, Any] | None = None,
    note: str | _Unset | None = _UNSET,
    **extra: Any,
) -> ErrorEnvelope:
    """拼一个失败信封。**不抛异常**：失败也是正常回包（`ok:false` + 非空 `error`）。

    `code` 是给日志/断言看的错误类别（`fetch_failed` / `revise_rejected` …），
    **不进回包**：契约里没有 `code` 键，想把它交给模型就写进 `data`（如 `data.error_kind`）。
    `message` 原样写进 `error`（不做 strip/截断/拼接，改了就等于改输出）。
    """
    if not code.strip():
        raise ValueError("fail() 必须给非空 code（它是日志/断言里的错误类别）")
    text = (message or "").strip()
    if not text or text == "None":
        raise ValueError(f"fail({code!r}) 必须给非空 error 文案：模型靠它判断下一步")
    envelope: ErrorEnvelope = {
        "ok": False,
        "error": message,
        "data": _payload(data, note),
        "refs": _refs(extra.pop("refs", None)),
        **_known_extras(extra),
    }
    _reject_unknown("fail", extra)
    return envelope


def _payload(data: Mapping[str, Any] | None, note: str | _Unset | None) -> dict[str, Any]:
    """`data` 原样拷贝（不丢键、不排序）；`note` 只在调用方表达过意见时才写。"""
    payload: dict[str, Any] = dict(data) if data else {}
    if not isinstance(note, _Unset):
        payload["note"] = note
    return payload


def _refs(value: Any) -> Refs:
    """`refs` 缺省 `{}`；给了就原样收下（键/值都不动，id 已在调用方生成）。"""
    if value is None:
        return {}
    refs: Refs = dict(value)
    return refs


def _known_extras(extra: dict[str, Any]) -> _ExtraKeys:
    """取出 :data:`KNOWN_EXTRA_KEYS` 里的键（按名单顺序拼，键序与调用顺序无关）。

    只做「搬过去」：`skipped` / `needs_choice` 转成布尔、`options` 转成列表，
    都是为了形状稳定（前端按布尔渲染、按数组迭代），不做任何补键/改名。
    名单之外的键**留在 `extra` 里**，由 :func:`_reject_unknown` 报错。
    """
    taken: _ExtraKeys = {}
    if "skipped" in extra:
        taken["skipped"] = bool(extra.pop("skipped"))
    if "needs_choice" in extra:
        taken["needs_choice"] = bool(extra.pop("needs_choice"))
    if "options" in extra:
        taken["options"] = list(extra.pop("options"))
    return taken


def _reject_unknown(name: str, extra: dict[str, Any]) -> None:
    """名单之外的顶层键一律报错：多一个键就是改契约，不许「顺手带一下」。"""
    if extra:
        raise TypeError(
            f"{name}() 收到信封之外的顶层键 {sorted(extra)}："
            "回包顶层只有 ok/data/error/refs"
            f"（外加已知例外 {'/'.join(KNOWN_EXTRA_KEYS)}，docs/01 §5.0）；"
            "要交给模型的东西请放进 data"
        )
