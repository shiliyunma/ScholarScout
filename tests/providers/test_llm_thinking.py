"""关掉推理模型的思考 = 省钱杠杆（但必须默认保留质量）。

2026-10-06 探针（`run/cost_probe2.py`，真 API）实测：

| 请求体 | completion tokens | reasoning 字符 |
|---|---|---|
| 不带参数（现状） | 162 | 227 |
| `reasoning_effort=low` | 154 | 217 |  ← 这个模型**不认**它
| `reasoning_effort=minimal` | 160 | 231 |
| `thinking={"type":"disabled"}` | **36** | **0** |

所以开关落在 `thinking` 上。默认必须是 True（质量优先），省钱由用户自己选 ——
推理 token 正是这个模型花费的大头，但也正是它把工具选对的原因。
"""

from __future__ import annotations

import pytest

from scholarscout.config import load_config
from scholarscout.providers.llm import OpenAIChatClient, create_client


def make_client(**kwargs: object) -> OpenAIChatClient:
    return OpenAIChatClient(base_url="https://example.test/v1", api_key="k", model="m", **kwargs)


def test_thinking_on_by_default_sends_the_servers_default_effort() -> None:
    """默认档 `medium` → 线上 `high`，与"不传参数时服务端默认 high"**等价**。

    **这条换了判据（不是放宽）**：2026-10-06 那版断言"不带参数" —— 当时还没有档位概念，
    请求体里确实什么都没有。现在档位是**真实存在**的（`/models` 自己声明 low/high/max），
    默认 medium 映射到 high，所以会显式发出去；行为不变，但断言更强了：
    它同时钉住"默认档不许悄悄变成更强的档"（否则成本会无声上涨）。
    """
    client = make_client()
    assert client.thinking is True
    assert client.reasoning_level == "medium"
    payload = client._payload([{"role": "user", "content": "hi"}], None)
    assert payload["reasoning_effort"] == "high", "默认档没映射到服务端默认的 high"
    assert payload["thinking"] == {"type": "enabled"}


def test_thinking_off_disables_it_in_the_request() -> None:
    client = make_client(thinking=False)
    payload = client._payload([{"role": "user", "content": "hi"}], None)
    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload, "关档还发 reasoning_effort，服务端会不认"


def test_level_none_has_the_same_wire_shape_as_thinking_off() -> None:
    """界面「关」与老配置 `thinking: false` 必须发出**一模一样**的字段（老配置无需迁移）。"""
    payload_none = make_client(reasoning_level="none")._payload([], None)
    payload_bool = make_client(thinking=False)._payload([], None)
    assert payload_none == payload_bool


def test_tools_are_still_sent_when_thinking_is_off() -> None:
    """关思考不能顺手动掉工具声明 —— 那会让工具调用直接失效。"""
    client = make_client(thinking=False)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    payload = client._payload([{"role": "user", "content": "hi"}], tools)
    assert payload["tools"] == tools and payload["tool_choice"] == "auto"


def test_config_parses_thinking(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  chat:\n    model: x\n    base_url: https://a.test/v1\n    thinking: false\n",
        encoding="utf-8",
    )
    config = load_config(path, load_env=False)
    assert config.chat.thinking is False
    assert config.summary.thinking is True, "没写就是默认开（质量优先）"


def test_create_client_passes_thinking_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "k")
    config = load_config(load_env=False)
    from dataclasses import replace

    profile = replace(config.chat, thinking=False)
    client = create_client(profile)
    assert client.thinking is False


def test_thinking_param_shape_matches_the_probe() -> None:
    """形状要对得上探针里验证过的那次（形状错了服务端会 400）。"""
    client = make_client(thinking=False)
    payload = client._payload([], None)
    assert isinstance(payload["thinking"], dict)
    assert payload["thinking"].get("type") == "disabled"


def test_no_network_call_happens_when_building_payload() -> None:
    """纯函数：拼请求体不该建出 HTTP 客户端（建了就意味着有人在这里顺手发了请求）。"""
    client = make_client(thinking=False)
    client._payload([{"role": "user", "content": "x"}], None)
    assert client._client is None
