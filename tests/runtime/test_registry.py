"""工具注册表契约（含 OpenAI 声明的层数，那是踩过的坑）。"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest

from scholarscout.config import Features, RuntimeProfile
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec, tool

#: 仓库根（本文件在 ``tests/runtime/`` 下，往上两级）
CONFIG_EXAMPLE = Path(__file__).resolve().parents[2] / "config.example.yaml"

SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear_registry()
    yield
    registry.clear_registry()


def _spec(name: str = "research.search", toolset: str = "research") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="d",
        parameters=SCHEMA,
        handler=lambda *_a, **_k: None,
        toolset=toolset,
    )


def test_register_and_lookup() -> None:
    registry.register(_spec())
    assert registry.registered_names() == ["research.search"]
    assert registry.get_spec("research.search") is not None
    assert registry.get_spec("research.nope") is None


def test_duplicate_name_raises_unless_replace() -> None:
    registry.register(_spec())
    with pytest.raises(ValueError, match="重复"):
        registry.register(_spec())
    registry.register(_spec(), replace=True)
    assert registry.registered_names() == ["research.search"]


def test_unknown_toolset_rejected() -> None:
    with pytest.raises(ValueError, match="未知工具集"):
        registry.register(_spec(toolset="whatever"))


def test_default_toolsets_is_research() -> None:
    registry.register(_spec("research.a"))
    names = [s.name for s in registry.get_tools(None)]
    assert names == ["research.a"]


def test_enable_kb_switch_is_gone() -> None:
    """守卫：``enable_kb`` 空开关已彻底删掉（docs/23，不留假功能）。

    开着什么都不发生比没有这个开关更坏 —— 它让人以为"已支持知识库"。所以四件事一起钉住：
    ① ``Features`` / ``RuntimeProfile`` 上都没有这个属性；② ``kb`` 不在工具集白名单里
    （再注册 ``toolset="kb"`` 会当场 ``ValueError``）；③ 示例配置里不再有这行键；
    ④ ``get_tools()`` 不再收 ``features`` 参数（死过滤没有回来的入口）。
    """
    assert getattr(Features(), "enable_kb", None) is None
    assert getattr(RuntimeProfile(), "enable_kb", None) is None
    assert "kb" not in registry.TOOLSETS
    assert "enable_kb" not in CONFIG_EXAMPLE.read_text(encoding="utf-8")
    assert "features" not in inspect.signature(registry.get_tools).parameters
    with pytest.raises(ValueError, match="未知工具集"):
        registry.register(_spec("kb.b", toolset="kb"))


def test_openai_declaration_nests_parameters_under_function() -> None:
    spec = registry.register(_spec())
    decl = spec.to_openai()
    assert decl["type"] == "function"
    # 线上名不能带点：DeepSeek/OpenAI 只接受 ^[a-zA-Z0-9_-]+$，带点直接 400
    assert decl["function"]["name"] == "research__search"
    # 关键：参数在 function.parameters 里，不是在顶层（踩过的坑）
    assert decl["function"]["parameters"] == SCHEMA
    assert "parameters" not in decl


def test_wire_name_has_no_dots_and_is_reversible() -> None:
    assert registry.wire_name("research.search") == "research__search"
    assert registry.wire_name("kb_search") == "kb_search"  # 本来就合法就不用动
    assert registry.canonical_name("research__search") == "research.search"
    registry.register(_spec("research.search"))
    # 已注册时优先按注册名反查，不靠字符还原
    assert registry.canonical_name("research__search") == "research.search"


def test_tool_names_sent_to_api_are_all_accepted_by_the_pattern() -> None:
    """注册名全部转线上名后，必须都落在 API 允许的字符集里（这条拦的是"工具层整体不可用"）。"""
    import re

    from scholarscout.tools import register_tools

    register_tools()
    allowed = re.compile(r"^[a-zA-Z0-9_-]+$")
    for spec in registry.get_tools(["research"]):
        wire = spec.to_openai()["function"]["name"]
        assert allowed.match(wire), f"{spec.name} → {wire} 会被 API 拒掉"


def test_get_spec_accepts_both_names() -> None:
    registry.register(_spec("research.search"))
    assert registry.get_spec("research.search") is not None
    assert registry.get_spec("research__search") is not None


def test_register_rejects_wire_separator_in_name() -> None:
    with pytest.raises(ValueError, match="双下划线"):
        registry.register(_spec("research__bad"))


def test_openai_tools_helper() -> None:
    registry.register(_spec("research.a"))
    registry.register(_spec("research.b"))
    decls = registry.openai_tools(registry.get_tools(["research"]))
    assert [d["function"]["name"] for d in decls] == ["research__a", "research__b"]
    assert registry.openai_tools([]) == []


def test_tool_decorator_registers_handler() -> None:
    @tool(name="research.echo", description="回显", parameters=SCHEMA)
    def research_echo(ctx: object, query: str) -> dict[str, object]:
        del ctx
        return {"ok": True, "data": {"query": query}}

    spec = registry.get_spec("research.echo")
    assert spec is not None
    assert registry.invoke(spec, object(), {"query": "hi"}) == {"ok": True, "data": {"query": "hi"}}


def test_invoke_requires_ctx_first_argument() -> None:
    """工具 handler 必须把 ctx 放第一位 —— 忘了就会在这里炸，而不是等线上模型恰好调到它。"""

    @tool(name="research.bad", description="忘了 ctx", parameters=SCHEMA)
    def research_bad(query: str) -> dict[str, object]:
        return {"ok": True, "data": {"query": query}}

    spec = registry.get_spec("research.bad")
    assert spec is not None
    with pytest.raises(TypeError):
        registry.invoke(spec, object(), {"query": "x"})


def test_decorator_double_registration_raises() -> None:
    registry.register(_spec("research.echo"))

    with pytest.raises(ValueError, match="重复"):

        @tool(name="research.echo", description="x", parameters=SCHEMA)
        def _dup() -> None:
            return None
