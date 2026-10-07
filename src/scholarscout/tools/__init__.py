"""模型可见的工具集。``register_tools()`` 显式注册（幂等），由 app.build_context 调用。

每个工具模块导出 ``SPEC``（注册表用的声明）与 ``register()``；
**不在 import 时自动注册** —— 这样测试清空注册表后可以再注册一次，行为可预期。
"""

from __future__ import annotations

__all__: list[str] = []


def register_tools() -> list[str]:
    """注册本版所有工具，返回注册后的工具名列表。"""
    from scholarscout.runtime import registry
    from scholarscout.tools import (
        note_save,
        research_search,
        route_plan,
        route_revise,
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
    return registry.registered_names()
