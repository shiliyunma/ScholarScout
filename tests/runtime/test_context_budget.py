"""喂回模型的工具结果瘦身（`runtime/context_budget.py`）。

为什么值得单测：这块直接对应用户的账单，而且**出错是静默的** ——
去重时如果把 `source_id` 弄丢，模型下一步就会编 id（反幻觉守卫会拦，但那是拿成本换质量事故）；
折叠时如果连用户/助手消息一起改，对话本身就被破坏了。
"""

from __future__ import annotations

import json

from scholarscout.runtime import context_budget as cb


def make_result(items, **extra):
    return {
        "ok": True,
        "data": {
            "items": items,
            "provider_status": [{"provider": "arxiv", "ok": True, "n": len(items)}],
            **extra,
        },
        "refs": {"source_ids": [item["source_id"] for item in items if item.get("source_id")]},
    }


def item(source_id, snippet="x" * 500, **extra):
    payload = {
        "source_id": source_id,
        "title": "标题 " + source_id,
        "url": "https://example.org/" + source_id,
        "domain": "example.org",
        "authors": ["某人"],
        "year": 2022,
        "venue": "某刊",
        "source_type": "arxiv",
        "tier": None,
        "tier_label": None,
        "score": None,
        "snippet": snippet,
    }
    payload.update(extra)
    return payload


def test_items_are_projected_to_the_fields_the_model_needs() -> None:
    text = cb.project_tool_result(make_result([item("src_a")]))
    payload = json.loads(text)
    slim = payload["data"]["items"][0]
    assert set(slim) <= {"source_id", "title", "url", "year", "source_type", "snippet"}
    assert "domain" not in slim and "score" not in slim and "tier_label" not in slim


def test_source_ids_are_never_dropped() -> None:
    """去重不能丢锚点：丢了模型就会编 id。"""
    seen: set[str] = set()
    cb.project_tool_result(make_result([item("src_a"), item("src_b")]), seen_source_ids=seen)
    assert seen == {"src_a", "src_b"}
    second = json.loads(cb.project_tool_result(make_result([item("src_a")]), seen_source_ids=seen))
    assert second["data"]["items"][0]["source_id"] == "src_a"
    assert seen == {"src_a", "src_b"}


def test_repeated_sources_do_not_resend_the_snippet() -> None:
    """同一批来源被第二次捞回来时，摘要不再重复计费（实测里这是最大的一笔浪费）。"""
    seen: set[str] = set()
    first = cb.project_tool_result(
        make_result([item("src_a", snippet="真实摘要" * 100)]), seen_source_ids=seen
    )
    second = cb.project_tool_result(
        make_result([item("src_a", snippet="真实摘要" * 100)]), seen_source_ids=seen
    )
    assert len(second) < len(first)
    # 单条结果里固定字段（id/title/url）本来就要发，所以"必须减半"在单条场景下不成立；
    # 比例断言放在下面的"一批 8 条"用例里
    assert "真实摘要" in first
    assert "不再重复" in second


def test_repeated_batch_shrinks_by_more_than_half() -> None:
    """真实场景是一批 8 条：第二次同批来源应当压掉一半以上。"""
    seen: set[str] = set()
    items = [item(f"src_{i}") for i in range(8)]
    first = cb.project_tool_result(make_result(items), seen_source_ids=seen)
    second = cb.project_tool_result(make_result(items), seen_source_ids=seen)
    assert len(second) < len(first) / 2, f"第一次 {len(first)}，第二次 {len(second)}"


def test_long_snippet_is_truncated() -> None:
    payload = json.loads(cb.project_tool_result(make_result([item("src_a", snippet="长" * 900)])))
    snippet = payload["data"]["items"][0]["snippet"]
    assert len(snippet) <= cb.MAX_SNIPPET_CHARS


def test_provider_status_survives_projection() -> None:
    """模型要知道哪个来源没成功，否则会拿旧结果硬凑。"""
    payload = json.loads(cb.project_tool_result(make_result([item("src_a")])))
    assert payload["data"]["provider_status"][0]["provider"] == "arxiv"


def test_non_search_results_are_truncated_not_mangled() -> None:
    big = {"ok": True, "data": {"nodes": ["n" * 100] * 200}, "refs": {}}
    text = cb.project_tool_result(big, max_chars=500)
    assert len(text) <= 500 + len(cb.TRUNCATED_TEMPLATE) + 10
    assert "已截断" in text


def test_small_results_pass_through_untouched() -> None:
    """非检索类结果（没有 data.items）原样透传，不做"投影"这种破坏性改写。"""
    small = {"ok": True, "data": {"path": "run/x", "n": 1}, "refs": {}}
    assert json.loads(cb.project_tool_result(small)) == small


def test_collapse_keeps_recent_tool_results_and_untouched_dialogue() -> None:
    messages = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "问题"},
        {"role": "tool", "name": "research.search", "content": "A" * 100},
        {"role": "tool", "name": "research.search", "content": "B" * 100},
        {"role": "tool", "name": "source.fetch", "content": "C" * 100},
        {"role": "tool", "name": "source.fetch", "content": "D" * 100},
        {"role": "assistant", "content": "回答"},
    ]
    collapsed = cb.collapse_old_tool_results(messages, keep_recent=2)
    assert collapsed == 2
    assert messages[2]["content"].startswith("[此结果已在上面处理过")
    assert messages[3]["content"].startswith("[此结果已在上面处理过")
    assert messages[4]["content"] == "C" * 100
    assert messages[5]["content"] == "D" * 100
    assert messages[1]["content"] == "问题" and messages[6]["content"] == "回答"


def test_collapse_is_idempotent() -> None:
    messages = [{"role": "tool", "name": "t", "content": "A" * 50} for _ in range(5)]
    assert cb.collapse_old_tool_results(messages, keep_recent=2) == 3
    assert cb.collapse_old_tool_results(messages, keep_recent=2) == 0


def test_collapse_with_keep_recent_zero_folds_everything() -> None:
    messages = [{"role": "tool", "name": "t", "content": "A"} for _ in range(3)]
    assert cb.collapse_old_tool_results(messages, keep_recent=0) == 3


def test_messages_token_report_splits_by_role() -> None:
    messages = [
        {"role": "system", "content": "x" * 300},
        {"role": "tool", "name": "t", "content": "y" * 300},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
    ]
    report = cb.messages_token_report(messages)
    assert report["system"] == 100
    assert report["tool"] == 100
    assert report["total"] == report["system"] + report["tool"] + report["assistant"]


def test_projection_actually_shrinks_real_world_payload() -> None:
    """一条真实的检索结果（8 条、每条 500 字摘要）要能压掉一半以上。"""
    items = [item(f"src_{i}") for i in range(8)]
    raw = json.dumps(make_result(items), ensure_ascii=False)
    seen: set[str] = set()
    first = cb.project_tool_result(make_result(items), seen_source_ids=seen)
    assert len(first) < len(raw) / 2, f"原始 {len(raw)} 字符，瘦身后 {len(first)}"
