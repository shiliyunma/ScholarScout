"""思考参数的**降级**：服务端不认 `reasoning_effort` / `thinking` 时不报死，摘掉重试一次并标注。

为什么要单测这一条：这是本轮唯一"会改变请求体"的运行时兜底。两个失败方向都很贵 ——
① 不重试：用户换个模型就直接 400，功能等于只对一个端点可用；
② 无条件重试：真正的参数错误也重发一遍，把一次失败变成两次（还多花一次时间/一分钱）。

所以这里同时钉住"该重试的会重试"和"不该重试的一次都不多发"，并且**必须留痕**
（`reasoning_degraded` + `reasoning_note`），否则用户看到的是"档位选了没生效"却没有任何解释。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from scholarscout.providers.llm import LlmError, OpenAIChatClient

BASE = "https://example.test/v1"
MESSAGES = [{"role": "user", "content": "你好"}]

OK_BODY: dict[str, Any] = {
    "model": "m",
    "choices": [
        {
            "message": {"role": "assistant", "content": "收到"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 7, "completion_tokens": 2},
}


class Recorder:
    """记下每一次真实发出去的请求体（降级=第二次请求必须**少了字段**，不是"重发一遍"）。"""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content.decode("utf-8")))
        if len(self.payloads) == 1:
            return httpx.Response(
                400, json={"error": {"message": "Unsupported parameter: reasoning_effort"}}
            )
        return httpx.Response(200, json=OK_BODY)


def _client(handler: Any, **kwargs: Any) -> OpenAIChatClient:
    return OpenAIChatClient(
        base_url=BASE,
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_400_about_reasoning_strips_fields_and_retries_exactly_once() -> None:
    """桩 400 提到 reasoning → 摘字段重试一次成功，且回包**带标注**。"""
    rec = Recorder()
    reply = _client(rec, reasoning_level="max").chat(MESSAGES)

    assert len(rec.payloads) == 2, f"重试次数不对：发了 {len(rec.payloads)} 次"
    first, second = rec.payloads
    assert first["reasoning_effort"] == "max", "第一发没带上用户选的档位"
    assert "reasoning_effort" not in second, "重试时没摘掉 reasoning_effort"
    assert "thinking" not in second, "重试时没摘掉 thinking"
    # 除了这两个字段，其余必须原样（摘字段不等于换请求）
    assert {k: v for k, v in first.items() if k not in ("reasoning_effort", "thinking")} == second

    assert reply.content == "收到"
    assert reply.reasoning_degraded is True, "降级了但回包没标注（用户只会觉得档位没生效）"
    assert "reasoning" in reply.reasoning_note and "重试" in reply.reasoning_note
    assert reply.reasoning_level == "", "降级时不该声称自己按某个档位跑完了"


def test_non_reasoning_400_is_not_retried() -> None:
    """别的 400（如模型名不存在）**一次都不多试**：盲目重试只是把一次失败变成两次。"""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(400, json={"error": {"message": "model_not_found: m"}})

    with pytest.raises(LlmError) as excinfo:
        _client(handler).chat(MESSAGES)
    assert excinfo.value.http_status == 400
    assert len(calls) == 1, f"不该重试的 400 被重试了：{len(calls)} 次"


def test_when_the_retry_also_fails_the_real_error_surfaces() -> None:
    """两次都 400 → 报第二次的真实错误（不许把原始错误吞掉换成"降级成功"）。"""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(400, json={"error": {"message": "thinking 不支持"}})

    with pytest.raises(LlmError) as excinfo:
        _client(handler).chat(MESSAGES)
    assert len(calls) == 2, "应当只重试一次"
    assert excinfo.value.http_status == 400
    assert "thinking" in str(excinfo.value), "真实错误被换掉了"


def test_stream_path_degrades_the_same_way() -> None:
    """流式路径与一次性路径**同一口径**：同样摘字段重试一次，同样标注。"""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(
                400, json={"error": {"message": "Invalid value for thinking.type"}}
            )
        sse = (
            "".join(
                "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                for event in (
                    {"choices": [{"delta": {"content": "好"}}]},
                    {"choices": [{"delta": {"content": "的"}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                )
            )
            + "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=sse.encode("utf-8"))

    seen: list[str] = []
    reply = _client(handler, reasoning_level="low").chat_stream(MESSAGES, on_delta=seen.append)

    assert len(calls) == 2 and "reasoning_effort" in calls[0]
    assert "reasoning_effort" not in calls[1] and "thinking" not in calls[1]
    assert seen == ["好", "的"] and reply.content == "好的"
    assert reply.reasoning_degraded is True
    assert reply.reasoning_note


def test_successful_call_reports_what_it_actually_sent() -> None:
    """没降级时要如实写出发出去的档位（报告/界面靠它，不靠猜）。"""
    reply = _client(lambda request: httpx.Response(200, json=OK_BODY), reasoning_level="max").chat(
        MESSAGES
    )
    assert reply.reasoning_level == "max"
    assert reply.reasoning_degraded is False
    assert reply.reasoning_note == ""
