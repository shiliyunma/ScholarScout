"""`/models` 探测与回落：**探测失败不许让界面报错**，候选要有东西可显示。

这条是"模型下拉"的地基：探测是一把 GET（不花钱），但它会失败 —— 没 key、端点没这个接口、
网络抖、返回形状不认识。任何一种都不该把输入区变成"读不到模型清单"，
而应回落到 `config.yaml` 里记着的候选 + 当前模型，并**如实说明没探到**（note 非空）。

同时钉住解析口径：本服务在模型对象里直接声明真实思考档位（`effort.supported_levels`），
不认识的形状一律当"没声明"，按官方三档兜底 —— 不许把垃圾字段当成档位传下去。
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from scholarscout.providers import models
from scholarscout.providers.models import ModelInfo, ModelsProbeError, parse_models


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    """进程内缓存按 base_url 做键 —— 每个用例都从干净状态开始，否则会串。"""
    models.reset_cache()
    yield
    models.reset_cache()


def _client(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 解析


def test_parses_the_data_shape_and_dedupes() -> None:
    body = {
        "data": [
            {"id": "m-a", "name": "A"},
            {"id": "m-b"},
            {"id": "m-a", "name": "重复"},  # 同一个模型出现两次
            {"id": ""},  # 空 id 直接丢
            "m-c",  # 有的端点直接给字符串
        ]
    }
    found = parse_models(body)
    assert [item.id for item in found] == ["m-a", "m-b", "m-c"], "顺序/去重口径变了"
    assert found[0].label == "A"


def test_parses_the_models_shape_and_a_bare_list() -> None:
    assert [item.id for item in parse_models({"models": [{"id": "x"}]})] == ["x"]
    assert [item.id for item in parse_models([{"id": "y"}])] == ["y"]


def test_unknown_shapes_raise_instead_of_returning_a_fake_list() -> None:
    for body in ({"error": "nope"}, {"data": "not-a-list"}, 42):
        with pytest.raises(ModelsProbeError):
            parse_models(body)
    with pytest.raises(ModelsProbeError):
        parse_models({"data": []})


def test_declared_efforts_are_used_and_unknown_names_dropped() -> None:
    """服务端声明的档位是**唯一真值**；不认识的名字丢掉，不往界面传。"""
    found = parse_models(
        {
            "data": [
                {
                    "id": "m",
                    "effort": {
                        "supported_levels": ["max", "low", "high", "ultra"],
                        "default_level": "high",
                    },
                }
            ]
        }
    )
    assert found[0].efforts == ("low", "high", "max"), "顺序应按官方三档排，且丢掉 ultra"
    assert found[0].default_effort == "high"


def test_missing_effort_falls_back_to_the_official_three() -> None:
    found = parse_models({"data": [{"id": "m"}]})
    assert found[0].efforts == models.DEFAULT_EFFORTS
    assert found[0].default_effort == "high"


def test_default_level_outside_the_declared_set_falls_back_to_the_strongest_known() -> None:
    found = parse_models(
        {
            "data": [
                {"id": "m", "effort": {"supported_levels": ["low", "high"], "default_level": "x"}}
            ]
        }
    )
    assert found[0].default_effort == "high"


# ------------------------------------------------------------------ 探测失败


def test_probe_reports_auth_failure_in_plain_words() -> None:
    transport = _client(lambda request: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(ModelsProbeError) as excinfo:
        models.probe(base_url="https://a.test/v1", api_key="k", transport=transport)
    assert "鉴权" in str(excinfo.value)


def test_probe_reports_missing_endpoint_and_timeout() -> None:
    with pytest.raises(ModelsProbeError) as missing:
        models.probe(
            base_url="https://a.test/v1",
            api_key="k",
            transport=_client(lambda request: httpx.Response(404)),
        )
    assert "404" in str(missing.value)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(ModelsProbeError) as timeout:
        models.probe(base_url="https://a.test/v1", api_key="k", transport=_client(boom))
    assert "超时" in str(timeout.value)


def test_probe_rejects_non_json() -> None:
    transport = _client(lambda request: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ModelsProbeError) as excinfo:
        models.probe(base_url="https://a.test/v1", api_key="k", transport=transport)
    assert "JSON" in str(excinfo.value)


# ------------------------------------------------------------------ 回落（关键）


def test_discover_never_raises_and_falls_back_to_config() -> None:
    """探测 500 → **不抛异常**，候选 = 配置 + 当前模型，note 说清原因。"""
    transport = _client(lambda request: httpx.Response(500))
    found = models.discover(
        base_url="https://a.test/v1",
        api_key="k",
        current="m-now",
        configured=("m-old", "m-now2"),
        transport=transport,
    )
    assert found["source"] == "config"
    assert "探测失败" in found["note"]
    assert "m-now" in found["available"], "当前模型不在候选里（下拉会显示空，像模型没配）"
    assert found["available"][:2] == ["m-old", "m-now2"], "配置里记着的候选顺序被改了"
    assert found["current"] == "m-now"
    assert found["efforts"] == list(models.DEFAULT_EFFORTS)


def test_discover_without_a_key_does_not_even_try() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调到
        raise AssertionError("没有 key 还去探测了")

    found = models.discover(
        base_url="https://a.test/v1",
        api_key="",
        current="m-now",
        configured=("m-old",),
        transport=_client(handler),
    )
    assert found["source"] == "config"
    assert "LLM_API_KEY" in found["note"]


def test_discover_with_nothing_configured_still_returns_the_current_model() -> None:
    """最坏情况（没 key、没候选）：至少要有当前模型一条，界面不会空。"""
    found = models.discover(
        base_url="https://a.test/v1", api_key="", current="m-now", transport=_client(lambda r: None)
    )
    assert found["available"] == ["m-now"]


def test_discover_prefers_the_probe_and_reuses_its_cache() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "m-a", "effort": {"supported_levels": ["low", "high"]}},
                    {"id": "m-b"},
                ]
            },
        )

    first = models.discover(
        base_url="https://a.test/v1", api_key="k", current="m-a", transport=_client(handler)
    )
    assert first["source"] == "probe"
    assert first["available"] == ["m-a", "m-b"]
    assert first["models"][0]["efforts"] == ["low", "high"]

    second = models.discover(
        base_url="https://a.test/v1", api_key="k", current="m-a", transport=_client(handler)
    )
    assert len(calls) == 1, "同一进程内重复开下拉不该每次都打一次网"
    assert "缓存" in second["note"]


def test_discover_inserts_the_current_model_when_the_probe_omits_it() -> None:
    """当前模型不在探测结果里（比如刚换 key）也要能显示 —— 否则用户以为模型被清掉了。"""
    transport = _client(lambda request: httpx.Response(200, json={"data": [{"id": "m-b"}]}))
    found = models.discover(
        base_url="https://a.test/v1", api_key="k", current="m-mine", transport=transport
    )
    assert found["available"] == ["m-mine", "m-b"]
    assert [item["id"] for item in found["models"]] == ["m-mine", "m-b"]


def test_failed_probe_is_not_cached() -> None:
    """失败不缓存：下一次打开下拉要能重新探到（缓存失败会把"暂时不通"钉成"永远不通"）。"""
    down = _client(lambda request: httpx.Response(503))
    up = _client(lambda request: httpx.Response(200, json={"data": [{"id": "m-a"}]}))
    assert (
        models.discover(base_url="https://h.test/v1", api_key="k", current="m", transport=down)[
            "source"
        ]
        == "config"
    )
    assert (
        models.discover(base_url="https://h.test/v1", api_key="k", current="m", transport=up)[
            "source"
        ]
        == "probe"
    )


def test_model_info_serializes_for_the_interface() -> None:
    item = ModelInfo(id="m", label="", efforts=("low", "high"), default_effort="high")
    assert item.as_dict() == {
        "id": "m",
        "label": "m",  # 没给 label 时用 id（界面不会空一行）
        "efforts": ["low", "high"],
        "default_effort": "high",
    }
