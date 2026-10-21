"""信封构造器**自身**的契约：键集、键序、已知例外、拒绝未知键（docs/01 §5.0）。

为什么单独一道闸：`test_tool_envelope.py` 盯的是「8 个工具回出来的形状」，只有工具跑起来
才发现走样；而 `tools/envelope.py` 是回包的**唯一构造点** —— 它的键序一旦漂移，8 个工具
会一起漂移，那时闸只会报「某个工具形状不对」，指不到根。这里把根钉住：

- `ok()` 恒为 `ok, data, refs`（成功**没有** `error` 键，不是 `"error": null`）；
- `fail()` 恒为 `ok, error, data, refs`，且 `error` 必非空（空文案直接拒造）；
- 额外顶层键只认 `KNOWN_EXTRA_KEYS`（`skipped` / `needs_choice` / `options`）—— 这是
  `research.search` 三个早退分支的**已知例外**（界面正拿它们渲染状态卡），名单之外一律报错。

不碰数据库、不装配上下文：这里测的就是两个函数的拼装行为。
"""

from __future__ import annotations

from typing import Any

import pytest

from scholarscout.tools.envelope import KNOWN_EXTRA_KEYS, fail, ok


def test_ok_has_exactly_three_keys_in_a_fixed_order() -> None:
    """成功信封的键集与键序：`ok, data, refs`（`refs` 缺省补 `{}`，`data` 缺省补 `{}`）。"""
    envelope = ok()
    assert list(envelope) == ["ok", "data", "refs"]
    assert envelope == {"ok": True, "data": {}, "refs": {}}


def test_ok_never_carries_an_error_key() -> None:
    """成功**不带** `error` 键 —— 连 `"error": null` 都不许（docs/01 的示例比代码宽，以代码为准）。"""
    assert "error" not in ok()
    assert "error" not in ok({"items": []}, refs={"source_ids": ["src_1"]})


def test_fail_has_exactly_four_keys_in_a_fixed_order() -> None:
    """失败信封的键集与键序：`ok, error, data, refs`（与 `ok()` 的区别只有多一个 `error`）。"""
    envelope = fail("fetch_failed", "抓取失败：HTTP 404")
    assert list(envelope) == ["ok", "error", "data", "refs"]
    assert envelope == {
        "ok": False,
        "error": "抓取失败：HTTP 404",
        "data": {},
        "refs": {},
    }


@pytest.mark.parametrize("message", ["", "   ", "None"])
def test_fail_refuses_a_blank_or_none_string_message(message: str) -> None:
    """`ok:false` 却没给可用文案 = 让模型猜，构造器直接拒造（静默放过就会流出空 `error`）。"""
    with pytest.raises(ValueError, match="非空"):
        fail("fetch_failed", message)


def test_note_lands_in_data_and_never_on_the_top_level() -> None:
    """`note` 是「这一轮为什么这么搜」的说明，只进 `data.note`；不传就不补键。"""
    with_note = ok({"items": []}, note="已按偏好只查 arxiv")
    assert "note" not in with_note
    assert list(with_note["data"]) == ["items", "note"]

    # 不传 = 不动 data（不是补一个 null）
    assert ok({"items": []})["data"] == {"items": []}
    # 显式传 None = 照写 null（有的工具的成功回包一直带这个键）
    assert ok({"items": []}, note=None)["data"] == {"items": [], "note": None}


def test_refs_default_to_an_empty_object_and_are_passed_through_untouched() -> None:
    """`refs` 一律出现（没引用就是 `{}`）；给了就原样收下，不改名不排序。"""
    assert ok()["refs"] == {}
    refs = {"source_ids": ["src_b", "src_a"], "doc_ids": []}
    assert ok({}, refs=refs)["refs"] == refs
    assert list(ok({}, refs=refs)["refs"]) == ["source_ids", "doc_ids"]


def test_known_extra_keys_pass_through_in_a_stable_order() -> None:
    """三个已知例外能带、值原样进信封、键序按名单拼（与调用时的书写顺序无关）。"""
    envelope = fail(
        "search_needs_choice",
        "没说清要哪类资料：先问用户",
        options=[{"type": "paper"}],
        needs_choice=True,
        skipped=False,
    )
    assert list(envelope) == ["ok", "error", "data", "refs", *KNOWN_EXTRA_KEYS]
    assert envelope["skipped"] is False
    assert envelope["needs_choice"] is True
    assert envelope["options"] == [{"type": "paper"}]

    # 成功信封同样能带（构造器对称，免得下一个工具为了带一个状态键去手写字典）
    success: dict[str, Any] = ok({"items": []}, skipped=True)
    assert list(success) == ["ok", "data", "refs", "skipped"]


def test_unknown_top_level_key_is_refused_by_both_builders() -> None:
    """名单之外的顶层键一律报错：多一个键就是改契约，不许「顺手带一下」。"""
    with pytest.raises(TypeError, match="信封之外的顶层键"):
        ok({"items": []}, data_note="放错层了")
    with pytest.raises(TypeError, match="信封之外的顶层键"):
        fail("x", "原因", error_kind="fetch_failed")
