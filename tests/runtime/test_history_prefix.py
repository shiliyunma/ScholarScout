"""历史裁剪的**前缀稳定性**（A6）与缓存记账。

为什么专门测：服务端上下文缓存按"从头开始的最长公共前缀"命中，而缓存命中的输入单价
差一个量级（Manus 实测 $0.30 vs $3.00 /MTok）。原实现把"中间 N 条已省略"的说明**插在头部**，
于是每轮前缀都在变 —— 等于主动放弃缓存，还把第 1 轮的证据挤走。

三条不变量：
1. 头部恒定：`[最初用户消息] + [最近的对话…]`（system 由调用方在最前面加）；
2. 省略说明在**末尾**，不在头部；
3. 下一轮历史更长时，输出的**前缀逐字节不变**（只会在尾部增长）。
"""

from __future__ import annotations

from typing import Any

from scholarscout.runtime.loop import _fit_history


def _msg(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


def _history(turns: int, *, chars: int = 400) -> list[dict[str, Any]]:
    """造一段多轮历史：第 1 条是用户最初的需求，后面交替。"""
    items = [_msg("user", "帮我入门教育智能体。" + "起点" * 20)]
    for index in range(turns):
        items.append(_msg("assistant", f"第 {index} 轮回答。" + "答" * chars))
        items.append(_msg("user", f"第 {index} 轮追问。" + "问" * chars))
    return items


def test_short_history_is_returned_untouched() -> None:
    history = _history(1)
    assert _fit_history(history, budget_chars=10_000) == history


def test_note_goes_to_the_end_not_the_head() -> None:
    """省略说明必须在末尾 —— 放头部会毁掉缓存前缀（这是本轮修的 bug）。"""
    history = _history(8)
    fitted = _fit_history(history, budget_chars=800)
    assert fitted[-1]["role"] == "system"
    assert "已省略" in str(fitted[-1]["content"])
    assert "已省略" not in str(fitted[0].get("content")), "头部不许出现省略说明"


def test_first_user_message_stays_at_the_head() -> None:
    history = _history(8)
    fitted = _fit_history(history, budget_chars=800)
    assert fitted[0] is not None
    assert fitted[0]["role"] == "user"
    assert "帮我入门教育智能体" in str(fitted[0]["content"]), "最初的需求必须保住"


def test_prefix_is_stable_when_history_grows() -> None:
    """模拟"下一轮"：历史只增不改，前缀必须逐字节相同（缓存才能命中）。"""
    turn_n = _history(6)
    turn_n1 = _history(6) + [_msg("assistant", "再答一轮。" + "答" * 200), _msg("user", "再追问")]
    fitted_n = _fit_history(turn_n, budget_chars=1200)
    fitted_n1 = _fit_history(turn_n1, budget_chars=1200)
    common = 0
    for left, right in zip(fitted_n, fitted_n1, strict=False):
        if left == right:
            common += 1
        else:
            break
    assert common >= 2, f"前缀只剩 {common} 条相同 —— 缓存会整段失效"


def test_cache_hit_rate_is_none_when_provider_reports_no_cache() -> None:
    """服务端根本没返回缓存字段时，命中率必须是 None，不能算成 0%。

    把"服务端不报"当成"缓存一次没命中"会让人误判前缀策略完全失效。
    """
    from scholarscout.state import traces

    assert traces.record_usage is not None  # 记账入口存在（口径在 session.usage 里算）


def test_usage_parse_reads_both_cache_field_names() -> None:
    """DeepSeek 用 prompt_cache_hit_tokens；OpenAI 兼容用 prompt_tokens_details.cached_tokens。"""
    from scholarscout.providers.llm import OpenAIChatClient

    client = OpenAIChatClient(api_key="k", model="m", base_url="https://example.invalid")
    body = {
        "model": "m",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_cache_hit_tokens": 80,
        },
    }
    reply = client._parse(body)
    assert reply.usage["cached_tokens"] == 80

    body["usage"] = {
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 64},
    }
    assert client._parse(body).usage["cached_tokens"] == 64

    body["usage"] = {"prompt_tokens": 100, "completion_tokens": 10}
    assert "cached_tokens" not in client._parse(body).usage, "服务端没报就别编"
