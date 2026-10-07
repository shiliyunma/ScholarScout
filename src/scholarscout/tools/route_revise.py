"""``route.revise`` —— 对**已存在**的学习路线做增量修改（docs/01 §5.5）。

守则：
- 模型**只输出补丁**（几处改动），不重吐整条路线 —— 没被点到的部分一个字节都不变；
- 补丁里挂的 `source_id` 必须是当前候选资料里真实存在的；资料类条目**不允许"删掉出处"**；
- 补丁不合法（操作名/字段名/周号/类型写错）就**整条拒绝**并如实说明，绝不写半截；
- 改写之前**先归档旧版**（`artifact_versions`），改错了能退回去 —— 路线是花钱生成的。

与 ``route.plan`` 的分工：新方向用 `route.plan`；改已有路线（"第 3 周加一条练习"、
"把每周时长改成 8 小时"）用 `route.revise`，**不要**重新 `route.plan`。
"""

from __future__ import annotations

from typing import Any

from scholarscout.learning.route_patch import PatchError, apply_ops, parse_patch
from scholarscout.runtime.registry import ToolSpec
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state.learning import get_route, revise_route
from scholarscout.summary.prompts import PromptPack
from scholarscout.tools.source_context import candidate_sources, render_sources, valid_ids

DESCRIPTION = (
    "修改一条**已经存在**的学习路线：只提交要改的那几处（增量），不要重新生成整条。"
    "用于用户对已有路线提出调整时（改某周的目标/检查点、加/删一条资料或练习、"
    "改每周时长、加/删一周）。需要 route_id 与一句话要求；返回改了什么（before → after）与新版本号。"
)

PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "route_id": {"type": "string", "description": "要修改的路线 id（如 rte_xxx）"},
        "instruction": {
            "type": "string",
            "description": "用户对这条路线的一处/几处修改要求，原样转述即可（如「第 3 周加一条动手练习」）",
        },
    },
    "required": ["route_id", "instruction"],
    "additionalProperties": False,
}


def render_route(plan: dict[str, Any]) -> str:
    """把路线渲染成**给模型看的紧凑文本**（比塞 JSON 省 token，也不容易被格式带偏）。"""
    lines: list[str] = []
    if plan.get("weekly_hours"):
        lines.append(f"每周时长：{plan['weekly_hours']} 小时")
    if plan.get("level"):
        lines.append(f"水平：{plan['level']}")
    for entry in plan.get("weeks") or []:
        lines.append(f"\n第 {entry.get('idx')} 周")
        lines.append(f"  目标：{entry.get('goal')}")
        lines.append(f"  检查点：{entry.get('checkpoint')}")
        for position, item in enumerate(entry.get("items") or [], start=1):
            source = item.get("source_id") or "-"
            minutes = item.get("est_minutes")
            tail = f"（{minutes} 分钟）" if minutes else ""
            lines.append(
                f"  {position}. [{item.get('kind')}] {item.get('title')} | source_id={source}{tail}"
            )
    return "\n".join(lines).strip() or "（空路线）"


def route_revise(
    ctx: ToolContext,
    route_id: str,
    instruction: str,
) -> dict[str, Any]:
    route_id = (route_id or "").strip()
    if not route_id:
        raise ValueError("route_id 不能为空")
    request = (instruction or "").strip()
    if not request:
        raise ValueError("instruction 不能为空（要改什么必须说清楚）")

    row = get_route(ctx.db, route_id)
    if row is None:
        raise ValueError(f"路线不存在：{route_id}（新方向请用 route.plan）")
    plan = row["plan"]

    rows = candidate_sources(ctx, source_ids=None, topic=row["topic"])
    allowed = valid_ids(rows)
    if ctx.llm is None:
        raise ValueError("没有可用模型，无法做增量修改")

    pack = PromptPack(ctx.config.summary_cfg.prompts_dir)
    prompt = pack.compose(
        "revise-route",
        route=render_route(plan),
        instruction=request,
        sources=render_sources(rows),
    )
    reply = ctx.llm.chat([{"role": "user", "content": prompt}])
    content = str(getattr(reply, "content", "") or "")
    try:
        patch = parse_patch(content)
        changes_ops = patch.get("ops")
        new_plan, changes = apply_ops(plan, changes_ops, allowed_source_ids=allowed)
    except PatchError as exc:
        # 不合法就整条拒绝：不写半截，也不假装改过
        raise ValueError(f"增量修改被拒绝（{exc}）—— 原路线未改动") from exc

    summary = str(patch.get("summary") or "").strip() or "按你的要求做了增量修改"
    version = revise_route(ctx.db, route_id, new_plan, note=summary)

    source_ids = sorted(
        {
            str(item["source_id"])
            for entry in new_plan.get("weeks") or []
            for item in entry.get("items") or []
            if item.get("source_id")
        }
    )
    return {
        "route_id": route_id,
        "version": version,
        "summary": summary,
        "changes": changes,
        "weeks": len(new_plan.get("weeks") or []),
        "refs": {"route_ids": [route_id], "source_ids": source_ids},
    }


SPEC = ToolSpec(
    name="route.revise",
    description=DESCRIPTION,
    parameters=PARAMETERS,
    handler=route_revise,
    toolset="research",
)


def register() -> None:
    from scholarscout.runtime import registry

    registry.register(SPEC, replace=True)
