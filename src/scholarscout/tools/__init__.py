"""模型可见的工具集。``register_tools()`` 显式注册（幂等），由 app.build_context 调用。

每个工具模块导出 ``SPEC``（注册表用的声明）与 ``register()``；
**不在 import 时自动注册** —— 这样测试清空注册表后可以再注册一次，行为可预期。
"""

from __future__ import annotations

__all__: list[str] = []


def register_tools(*, enable_kb: bool = False) -> list[str]:
    """注册本版所有工具，返回注册后的工具名列表。

    ``enable_kb`` 决定要不要注册 ``kb.search``（docs/01 §5.8）：**默认关**，
    与产品默认取值一致（``Features.enable_kb = False`` / ``config.example.yaml`` 的
    ``features.enable_kb: false``）。默认值必须跟产品默认一致：调用方忘了传参数时，
    得到的是"产品默认那一套 11 个工具"，而不是"多出一个默认关掉的工具" ——
    后者会让契约清单（``tests/contract`` 的 11 个名字）与真实注册集静默分叉。
    真正跑应用的是 `app.build_context`，它一定显式传 ``cfg.features.enable_kb``。

    关着时**不注册**（而不是注册了再过滤）：注册表里没这个东西，模型清单、
    `tool_traces` 与线上声明就都不可能出现它，也就不会出现"看着能用其实没接线"的假象。
    """
    from scholarscout.runtime import registry
    from scholarscout.tools import (
        delegate,
        kb_search,
        note_save,
        py_exec,
        research_search,
        route_plan,
        route_revise,
        skills,
        source_fetch,
        source_grade,
        topic_map,
    )

    research_search.register()
    source_fetch.register()
    source_grade.register()
    topic_map.register()
    route_plan.register()
    route_revise.register()
    note_save.register()
    delegate.register()
    py_exec.register()
    skills.register()
    if enable_kb:
        kb_search.register()
    return registry.registered_names()
