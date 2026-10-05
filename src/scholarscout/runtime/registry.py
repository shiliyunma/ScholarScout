"""工具注册表。

**M2 阶段注册表是空的** —— M3 起在各 ``tools/*.py`` 里用 ``@tool`` 注册。
空注册表是刻意的：模型只能调用真正注册过的工具，绝不在 loop 里塞假工具或特例分支。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from scholarscout.config import Features

#: 工具集白名单（docs/01 §5.0）
TOOLSETS = ("research", "kb")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    #: 签名必须是 ``handler(ctx: ToolContext, **arguments)`` —— 由 runtime/registry.invoke 强制
    handler: Callable[..., Any]
    toolset: str = "research"

    def to_openai(self) -> dict[str, Any]:
        """OpenAI 工具声明。

        层数必须是 ``function.parameters``；写成 ``tools[i]["parameters"]`` 会拿不到参数
        （本项目踩过的坑，见 docs/03 §7）。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
    if spec.toolset not in TOOLSETS:
        raise ValueError(f"未知工具集：{spec.toolset!r}（允许：{TOOLSETS}）")
    if spec.name in _REGISTRY and not replace:
        raise ValueError(f"工具名重复：{spec.name}")
    _REGISTRY[spec.name] = spec
    return spec


def tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    toolset: str = "research",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器：``@tool(name="research.search", description=..., parameters={...})``。"""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        register(
            ToolSpec(
                name=name,
                description=description,
                parameters=parameters,
                handler=func,
                toolset=toolset,
            )
        )
        return func

    return decorator


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


def get_spec(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def get_tools(
    toolsets: Sequence[str] | None = None,
    *,
    features: Features | None = None,
) -> list[ToolSpec]:
    """按工具集取工具。``kb`` 工具集受 ``features.enable_kb`` 控制（论文消融开关）。"""
    wanted = set(toolsets) if toolsets else {"research"}
    specs: list[ToolSpec] = []
    for spec in _REGISTRY.values():
        if spec.toolset not in wanted:
            continue
        if spec.toolset == "kb" and features is not None and not features.enable_kb:
            continue
        specs.append(spec)
    return sorted(specs, key=lambda s: s.name)


def openai_tools(specs: Iterable[ToolSpec] | None) -> list[dict[str, Any]]:
    return [spec.to_openai() for spec in (specs or [])]


def invoke(spec: ToolSpec, ctx: Any, arguments: dict[str, Any]) -> Any:
    """统一的工具调用入口：注入 ctx，校验参数签名。

    工具 handler 的第一位参数必须是 ctx（:class:`~scholarscout.runtime.toolctx.ToolContext`）。
    签名不对会在这里就报成 ``TypeError``，而不是等到线上某个模型恰好调到它。
    """
    return spec.handler(ctx, **arguments)


def clear_registry() -> None:
    """只给测试用：清空注册表。"""
    _REGISTRY.clear()
