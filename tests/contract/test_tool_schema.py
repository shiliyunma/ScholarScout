"""契约闸 4/4：工具声明的**形状**（`ToolSpec.to_openai()`）与注册表一致（docs/01 §5.0）。

为什么这道闸必须有：`to_openai()` 是唯一一次「把 JSON Schema 交给模型厂商」的翻译。
它写错时**本机一切照跑**（工具直接按 Python 签名调用，根本没走 schema），
错的是线上：参数拿不到、名字带点被 400、`sources` 枚举与后端认的来源漂移 ——
这三种坑项目都踩过（见 `runtime/registry.py` 的 `wire_name()` 注释、`providers/settings.KNOWN`）。

四条规则：

1. **三件套齐全**：`to_openai()` 必须是 `{type:"function", function:{name, description, parameters}}`，
   `name` / `description` / `parameters` 一个都不能空 —— 缺 description 的模型会乱猜它干什么；
2. **`parameters` 是合法 JSON Schema**：`type: "object"` + 非空 `properties` + `required ⊆ properties`
   + `additionalProperties: False`（约定：参数校验失败由 rpc 层映射成 `-32602`，不许悄悄吃多余参数）；
3. **枚举与后端同源**：带 `sources` 参数的工具，`items.enum` 必须**逐字等于** `list(KNOWN)`
   （文档里写的来源名与 `providers/settings.py` 认的来源名，只许有一份清单）；
4. **名字唯一且能上线**：注册名不重复、互不为「点换双下划线」的歧义碰撞，线上名落在 `^[a-zA-Z0-9_-]+$`。

本闸只读 `SPEC`（不调 handler、不出网），所以它跑得比信封闸快，坏消息也来得更早。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from scholarscout.providers.settings import KNOWN
from scholarscout.runtime.registry import ToolSpec, get_spec, register, wire_name
from scholarscout.tools import register_tools

#: 本版注册的 11 个工具（`docs/01 §5` 冻结，**新增的两个工具**——`research.delegate` 与 `py.exec`——
#: 的 schema 与边界写在 `docs/24`；`skill.list` / `skill.load` 是本地技能库的渐进式披露入口，
#: 索引不在提示词里、按需取，见 `skills/index.py`；08-10 那段冻结期不动 docs/01 的表格）
TOOL_NAMES = (
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

#: OpenAI 兼容 API 对 `tools[].function.name` 的校验（DeepSeek 实测，带点直接 400）
WIRE_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def _spec(name: str) -> ToolSpec:
    """按名字取真实 SPEC；取不到就是注册表漏了（不许在断言里静默跳过）。"""
    spec = get_spec(name)
    assert spec is not None, f"工具没有注册：{name}"
    return spec


def _specs() -> list[ToolSpec]:
    return [_spec(name) for name in TOOL_NAMES]


def test_registered_names_are_unique_and_match_registry() -> None:
    """注册集 == 契约里那 11 个，且不重名（重名会让「线上名 → 规范名」反查歧义）。"""
    names = register_tools()
    assert names == sorted(TOOL_NAMES), f"注册集与契约不一致：{names}"
    assert len(names) == len(set(names)), f"注册集里有重名：{names}"
    assert len({wire_name(name) for name in names}) == len(names), (
        f"有两个工具换算出同一个线上名：{[wire_name(name) for name in names]}"
    )


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_to_openai_has_the_three_pieces(name: str) -> None:
    """`to_openai()` 的三件套：`name` / `description` / `parameters`。"""
    spec = _spec(name)
    declaration: dict[str, Any] = spec.to_openai()
    assert declaration["type"] == "function", "工具声明必须是 type=function"
    assert set(declaration) == {"type", "function"}, (
        f"{spec.name} 的声明多了/少了顶层键：{sorted(declaration)}"
    )
    function = declaration["function"]
    assert set(function) == {"name", "description", "parameters"}, (
        f"{spec.name} 的 function 键集不对：{sorted(function)}"
    )
    assert function["name"] == wire_name(spec.name), (
        f"{spec.name} 的线上名不是 wire_name：{function['name']!r}"
    )
    assert WIRE_NAME.match(function["name"]), (
        f"{spec.name} 的线上名 {function['name']!r} 过不了 API 校验（会整层 400）"
    )
    assert isinstance(function["description"], str) and function["description"].strip(), (
        f"{spec.name} 没有 description：模型只能瞎猜它是干什么的"
    )
    assert isinstance(function["parameters"], dict) and function["parameters"], (
        f"{spec.name} 的 parameters 为空"
    )


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_parameters_is_a_valid_object_schema(name: str) -> None:
    """`parameters` 是合法 JSON Schema（对象 + 非空 properties + required 闭合 + 不许多余参数）。"""
    spec = _spec(name)
    params = spec.to_openai()["function"]["parameters"]
    assert params.get("type") == "object", f"{spec.name} 的 parameters.type 不是 object"
    properties = params.get("properties")
    assert isinstance(properties, dict) and properties, f"{spec.name} 没有 properties"
    assert params.get("additionalProperties") is False, (
        f"{spec.name} 少了 additionalProperties: false（多余参数会被静默吞掉）"
    )
    required = params.get("required", [])
    assert isinstance(required, list), f"{spec.name} 的 required 不是数组"
    unknown = [item for item in required if item not in properties]
    assert unknown == [], f"{spec.name} 的 required 里有未声明的参数：{unknown}"
    for param, schema in properties.items():
        assert isinstance(schema, dict), f"{spec.name}.{param} 的 schema 不是对象"
        assert isinstance(schema.get("type"), str) and schema["type"], (
            f"{spec.name}.{param} 缺少 type（模型不知道该填什么类型）"
        )


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_sources_enum_comes_from_known_providers(name: str) -> None:
    """带 `sources` 参数的工具，`enum` 必须逐字等于 `list(KNOWN)`（唯一来源清单）。

    没有 `sources` 参数的工具直接返回（**不 skip**：门禁要求「跳过 0 项」，
    而「有没有工具声明 sources」由 `test_at_least_one_tool_declares_sources` 单独钉住，闸不会空转）。
    """
    spec = _spec(name)
    properties = spec.to_openai()["function"]["parameters"]["properties"]
    if "sources" not in properties:
        return
    items = properties["sources"]
    assert items.get("type") == "array", f"{spec.name}.sources 必须是数组"
    enum = items.get("items", {}).get("enum")
    assert enum == list(KNOWN), (
        f"{spec.name}.sources 的枚举与 providers.settings.KNOWN 漂移：\n"
        f"  schema: {enum}\n  KNOWN : {list(KNOWN)}"
    )


def test_at_least_one_tool_declares_sources() -> None:
    """上一条闸不能因为「一个工具都没有 sources」而空转。"""
    declared = [
        spec.name
        for spec in _specs()
        if "sources" in spec.to_openai()["function"]["parameters"]["properties"]
    ]
    assert declared, "没有任何工具声明 sources —— 枚举闸会变成空跑，契约与代码已经漂了"


def test_register_rejects_ambiguous_names() -> None:
    """注册期就该拦下带双下划线的名字（否则线上名反查有歧义，只能靠人记）。"""
    with pytest.raises(ValueError, match="双下划线"):
        register(
            ToolSpec(
                name="research__search",
                description="重名碰撞样本",
                parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                handler=lambda ctx, **kwargs: {},
            )
        )
