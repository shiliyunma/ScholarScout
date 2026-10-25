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


def test_enable_kb_switch_gates_kb_search() -> None:
    """`enable_kb` 现在是**真开关**：它决定 `kb.search` 有没有被注册（docs/01 §5.8）。

    这条是 docs/23 那条"空开关"缺陷的**反面守卫**。当时的教训是：开关开了什么都不发生，
    比没有这个开关更坏。所以这里只断言**行为差异**，不断言"字段存在"：

    ① 产品默认是关（`Features()` / 示例配置）→ 注册集里不含 `kb.search`；
    ② `enable_kb=True` → 注册集多一个 `kb.search`；
    ③ 收敛点只有一个：`enabled_toolsets` —— 关着时**会话显式要 `kb` 也摘掉**，开着时没要也补上；
    ④ `get_tools()` 仍不收 `features` 参数（开关不许散进注册表，否则绕过路径会再长出来）。

    注册集总数在 2026-10-25 变过一次（11 → **13**：`enable_files` 改默认开，多出 `file.list` /
    `file.read`；`enable_tool_disclosure` 仍是默认**关**，所以 `tool.search` / `tool.describe`
    不在默认集里 —— 实测开着会把整轮调用数 19 → 25 而没有包含率收益，见 `Features` 的注释），
    但这些断言钉的是**kb 开关的增量**（+1），所以写成「基数 → 基数 + 1」而不是硬写新数字：
    再改别的默认值时这条用例不会跟着红，也不会因此变得没意义；而行首的基数仍然显式钉死，
    免得「默认集被谁悄悄削掉两个」也照样绿。
    """
    from scholarscout.tools import register_tools

    # ① 默认关：产品默认值与注册集默认值必须是同一个答案（否则契约清单会静默分叉）
    assert Features().enable_kb is False
    example = CONFIG_EXAMPLE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "\n  enable_kb: false\n" in example
    assert "enable_kb: true" not in example
    assert "kb" in registry.TOOLSETS
    off = register_tools()
    assert "kb.search" not in off
    assert len(off) == 13, (
        f"默认注册集是 13 个（旧值 11，2026-10-25 起 file.* 默认开；披露仍关）：{off}"
    )

    # ② 开了就真的多一个工具（不是"字段变了、行为没变"）
    registry.clear_registry()
    on = register_tools(enable_kb=True)
    assert "kb.search" in on
    assert len(on) == len(off) + 1
    assert on == sorted({*off, "kb.search"})

    # ③ 收敛点：关着时显式要 kb 被摘掉，开着时不用要也会补上
    assert registry.enabled_toolsets(["research", "kb"], enable_kb=False) == ("research",)
    assert registry.enabled_toolsets(["research"], enable_kb=True) == ("research", "kb")

    # ④ 注册表只按工具集筛，不看开关（开关的唯一实现处是 enabled_toolsets）
    assert "features" not in inspect.signature(registry.get_tools).parameters
    assert getattr(RuntimeProfile(), "enable_kb", None) is None


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
