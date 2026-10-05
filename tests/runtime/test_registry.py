"""工具注册表契约（含 OpenAI 声明的层数，那是踩过的坑）。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from scholarscout.config import Features
from scholarscout.runtime import registry
from scholarscout.runtime.registry import ToolSpec, tool

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
    registry.register(_spec("kb.b", toolset="kb"))
    names = [s.name for s in registry.get_tools(None)]
    assert names == ["research.a"]


def test_kb_toolset_gated_by_feature_flag() -> None:
    registry.register(_spec("kb.b", toolset="kb"))
    off = registry.get_tools(["kb"], features=Features(enable_kb=False))
    on = registry.get_tools(["kb"], features=Features(enable_kb=True))
    assert off == []
    assert [s.name for s in on] == ["kb.b"]


def test_openai_declaration_nests_parameters_under_function() -> None:
    spec = registry.register(_spec())
    decl = spec.to_openai()
    assert decl["type"] == "function"
    assert decl["function"]["name"] == "research.search"
    # 关键：参数在 function.parameters 里，不是在顶层（踩过的坑）
    assert decl["function"]["parameters"] == SCHEMA
    assert "parameters" not in decl


def test_openai_tools_helper() -> None:
    registry.register(_spec("research.a"))
    registry.register(_spec("research.b"))
    decls = registry.openai_tools(registry.get_tools(["research"]))
    assert [d["function"]["name"] for d in decls] == ["research.a", "research.b"]
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
