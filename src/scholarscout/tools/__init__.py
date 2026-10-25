"""模型可见的工具集。``register_tools()`` 显式注册（幂等），由 app.build_context 调用。

每个工具模块导出 ``SPEC``（注册表用的声明）与 ``register()``；
**不在 import 时自动注册** —— 这样测试清空注册表后可以再注册一次，行为可预期。
"""

from __future__ import annotations

__all__: list[str] = []


def register_tools(
    *,
    enable_kb: bool = False,
    enable_files: bool = True,
    enable_tool_disclosure: bool = False,
) -> list[str]:
    """注册本版所有工具，返回注册后的工具名列表。

    ``enable_kb`` 决定要不要注册 ``kb.search``（docs/01 §5.8）；``enable_files`` 决定要不要
    注册只读的 ``file.list`` / ``file.read``（docs/01 §3.16）；``enable_tool_disclosure``
    决定要不要注册工具目录的 ``tool.search`` / ``tool.describe``（docs/01 §3.17）。
    与产品默认一致（``Features`` 的两个字段 / ``config.example.yaml``）：默认是
    「13 个工具、**不带**渐进披露」，``enable_kb`` 仍默认关。披露默认关是实测结论 ——
    开着会把整轮调用数 19 → 25、冗余 7 → 15，而包含率没有收益（见 `Features` 的注释）。

    默认值必须跟产品默认一致：调用方忘了传参数时，得到的就是「产品默认那一套 13 个工具」，
    而不是「多两个默认关掉的元工具」—— 后者会让契约清单（``tests/contract`` 的名字表）
    与真实注册集静默分叉。真正跑应用的是 `app.build_context`，它一定显式传这三个开关。

    关着时**不注册**（而不是注册了再过滤）：注册表里没这个东西，模型清单、
    `tool_traces` 与线上声明就都不可能出现它，也就不会出现「看着能用其实没接线」的假象。
    """
    from scholarscout.runtime import registry
    from scholarscout.tools import (
        delegate,
        file_tools,
        kb_search,
        note_save,
        py_exec,
        research_search,
        route_plan,
        route_revise,
        skills,
        source_fetch,
        source_grade,
        tool_catalog,
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
    # 工具目录的两个元工具（`tool.search` / `tool.describe`，docs/01 §3.17）：**只在渐进披露
    # 开着时注册**。常驻工具的清单是契约的一部分（`tests/contract` 逐名比对那一串），
    # 所以它们不能无条件存在 —— 披露关着时那 13 个工具全常驻，元工具留着没意义。
    if enable_tool_disclosure:
        tool_catalog.register()
    if enable_kb:
        kb_search.register()
    if enable_files:
        file_tools.register()
    return registry.registered_names()
