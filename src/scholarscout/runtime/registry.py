"""工具注册表。

**M2 阶段注册表是空的** —— M3 起在各 ``tools/*.py`` 里用 ``@tool`` 注册。
空注册表是刻意的：模型只能调用真正注册过的工具，绝不在 loop 里塞假工具或特例分支。

**命名**：注册名是带点的命名空间（``research.search``），这是项目内部与文档里的**规范名**；
但 OpenAI 兼容 API（DeepSeek 实测）只接受 ``^[a-zA-Z0-9_-]+$``，点号会被 400 拒掉 ——
所以发给模型时换成 ````（点 → 双下划线），收到调用再映射回来。规范名始终只有一份。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

#: 工具集白名单（docs/01 §5.0）。`kb` **只在 `features.enable_kb` 开着时**才生效 ——
#: 收敛逻辑只有一处：:func:`enabled_toolsets`（工具注册与提示词都从它取）。
TOOLSETS = ("research", "kb")

#: API 允许的工具名字符集（DeepSeek / OpenAI 同款校验）
_WIRE_SAFE = re.compile(r"^[a-zA-Z0-9_-]+$")

#: 点号在线上名字里的替代物（双下划线，同样落在允许字符集内）
_WIRE_SEPARATOR = "__"


def wire_name(name: str) -> str:
    """规范名 → 线上名：``research.search`` → ``research__search``。

    已经是合法字符集的（如 ``research_search``）原样返回。
    """
    return name if _WIRE_SAFE.match(name) else name.replace(".", _WIRE_SEPARATOR)


def canonical_name(wire: str) -> str:
    """线上名 → 规范名。优先按已注册工具精确反查，查不到再按分隔符还原创名。"""
    for registered in _REGISTRY:
        if wire_name(registered) == wire:
            return registered
    return wire.replace(_WIRE_SEPARATOR, ".")


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

        - 层数必须是 ``function.parameters``；写成 ``tools[i]["parameters"]`` 会拿不到参数
          （本项目踩过的坑，见 docs/03 §7）
        - ``name`` 必须换成**线上名**：点号会被 API 判为非法字符直接 400
          （2026-10-05 真机跑 E3 才暴露：整个工具层对着 DeepSeek 从来没通过）
        """
        return {
            "type": "function",
            "function": {
                "name": wire_name(self.name),
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
    if spec.toolset not in TOOLSETS:
        raise ValueError(f"未知工具集：{spec.toolset!r}（允许：{TOOLSETS}）")
    if _WIRE_SEPARATOR in spec.name:
        # 名字里带双下划线会让「线上名 → 规范名」的映射出现歧义，注册时就拦下
        raise ValueError(f"工具名不得包含双下划线 {_WIRE_SEPARATOR!r}：{spec.name}")
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
    """按名字取工具：规范名（``research.search``）与线上名（``research__search``）都认。"""
    return _REGISTRY.get(name) or _REGISTRY.get(canonical_name(name))


def enabled_toolsets(requested: Sequence[str] | None = None, *, enable_kb: bool) -> tuple[str, ...]:
    """把请求的工具集按**特性开关**收敛 —— 这是 `kb` 门控的唯一实现处。

    - `enable_kb=False`：从请求里**摘掉** `kb`（会话显式要它也不行：RPC 的 `toolsets`
      参数来自前端，不可信）；
    - `enable_kb=True`：请求里没有 `kb` 也**补上**（开关的语义是「开了就有这个工具」，
      而不是「开了还得再手工指定一遍」）。

    为什么不留一个 `features` 参数给 `get_tools()`：那会让「开关过滤」散在注册表里，
    调用方越多漏一次就多一条静默失效的路径。收敛成纯函数之后，
    `app.build_context` / `loop` / `TurnContext.tools` 三处都调它，谁都不许自己判断。
    """
    kept = [name for name in (requested or ("research",)) if name in TOOLSETS and name != "kb"]
    if enable_kb:
        kept.append("kb")
    # 全都认不出来时退回默认工具集：空工具集会让模型以为「这轮不能调工具」
    return tuple(kept or ("research",))


def get_tools(toolsets: Sequence[str] | None = None) -> list[ToolSpec]:
    """按工具集取工具（工具集白名单见 :data:`TOOLSETS`）。

    **这里只按工具集筛，不看特性开关** —— 传进来的 `toolsets` 必须已经过
    :func:`enabled_toolsets` 收敛（`enable_kb` 关着时 `kb` 不在里面）。
    为什么这样切：注册表是纯数据结构，开关是配置，两者混在一起就会出现
    「有人绕过开关直接 `get_tools(["kb"])`」的缝。
    """
    wanted = set(toolsets) if toolsets else {"research"}
    specs: list[ToolSpec] = []
    for spec in _REGISTRY.values():
        if spec.toolset not in wanted:
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
