"""`skill.list` / `skill.load` 的 RPC 包装 —— 界面的 `/` 技能菜单用的那条路。

**为什么要有这一层**：这两个名字本来是**模型工具**（`tools/skills.py`），前端调不到；
而输入框里打 `/` 要的正是"先看名字、选中才载一篇"这同一份能力。所以这里只做**薄包装**，
三个刻意的取舍：

1. **不重写逻辑、不改回包形状**：直接调 `tools/skills.py` 的两个 handler，回包原样就是
   工具信封（`{ok, data, refs}`，见 `tools/envelope.py`）。于是排序规则（`skills/index.search`
   的"连续命中越长越靠前"）与"list 不回正文、load 才有全文"只有**一处定义**。
   前端另写一份筛选、或 RPC 另拼一份字段，菜单里排第一的就会与模型 `skill.list`
   拿到的不一样 —— 那种漂移没有任何人能查清该信哪个。
2. **错误按语义分两支**：`name` 缺失/类型不对 → `-32602`；库里没有这个名字 → `-32002`
   （`docs/01 §2`：资源不存在）。工具本身**不抛**（`ok:false` + `data.error_kind`），
   翻译在这一层做 —— 与 `route.revise` 把工具失败翻成 `-32602` 是同一条路，
   但"查不到"和"参数写错"是两件事，界面要能分开说。
3. **失败也把工具的原文透出**：`data` 原样挂在 `RpcError.data` 上（含 `error_kind` 与
   近似名 `suggestions`），排障时不必去后端日志里找"它到底说了什么"。

依赖方向仍然是单向的：`rpc/` → `tools/` → `skills/index`，`tools/` 不 import `rpc/`。
"""

from __future__ import annotations

from typing import Any

from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.registry import AppContext, _int_param, _params, method
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.tools.envelope import ToolResult
from scholarscout.tools.skills import DEFAULT_LIMIT
from scholarscout.tools.skills import skill_list as _tool_list
from scholarscout.tools.skills import skill_load as _tool_load


def _tool_ctx(ctx: AppContext) -> ToolContext:
    """把 RPC 的 `AppContext` 包成工具层要的 `ToolContext`（与 `route.revise` 同一做法）。

    这两个方法只读 `skills/` 下的文件，不碰库与模型；仍然照实把依赖传进去，
    是为了**不为了省两行就改工具签名**（那会让 RPC 与模型走两条不同形状的路径）。
    """
    return ToolContext(db=ctx.db, config=ctx.config, http=ctx.http, llm=ctx.llm)


@method("skill.list")
def skill_list_rpc(ctx: AppContext, params: dict[str, Any]) -> ToolResult:
    """列技能（**不回正文**）：`{query?, limit?}` → `tools/skills.py::skill_list` 的原回包。

    `query` 的字符匹配与排序全在 `skills/index.search` 里，这里一个字的筛选逻辑都没有 ——
    菜单与模型看到的必须是同一份顺序。

    返回类型就是工具的信封（`ToolResult`，与 `route.revise` 同一口径）：这层不翻错误，
    因为 `skill.list` 没有"资源不存在"这一支 —— 库是空的也只是 `count=0`。
    """
    params = _params(params)
    raw_query = params.get("query")
    if raw_query is not None and not isinstance(raw_query, str):
        raise RpcError(Code.INVALID_PARAMS, "参数 query 必须是字符串")
    limit = _int_param(params, "limit", DEFAULT_LIMIT)
    return _tool_list(_tool_ctx(ctx), (raw_query or "").strip(), limit)


@method("skill.load")
def skill_load_rpc(ctx: AppContext, params: dict[str, Any]) -> ToolResult:
    """读一篇的 `SKILL.md` 全文 + 附件清单（只有路径与字节数）。

    名字**必须**来自 `skill.list`：库里没有这个名 → `-32002`，`data.suggestions` 给近似名。
    """
    params = _params(params)
    raw_name = params.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise RpcError(
            Code.INVALID_PARAMS,
            "skill.load 需要 name：先用 skill.list（可带 query）拿到准确的名字。",
        )
    result = _tool_load(_tool_ctx(ctx), raw_name.strip())
    if result.get("ok") is True:
        return result
    kind = (result.get("data") or {}).get("error_kind")
    code = Code.NOT_FOUND if kind == "not_found" else Code.INVALID_PARAMS
    raise RpcError(code, str(result.get("error") or "技能读取失败"), data=result.get("data"))
