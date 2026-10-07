"""学习路线的**增量补丁**引擎（纯函数，不碰数据库、不调模型）。

为什么要有它：用户说"第 3 周改成 6 小时"时，让模型**重新生成整条路线**既贵又不稳
（同一份资料可能被换掉、周号可能漂移、还可能悄悄丢掉上次挂好的 source_id）。
增量补丁把这件事变成"只改这几处"：

- **省**：模型只输出补丁（几百字符），不重吐整条路线；
- **稳**：没被点到的部分**一个字节都不变**（挂好的出处不会丢）；
- **可核对**：应用完给一份 `before → after` 的明细，用户能逐条看改了什么。

三条硬规矩（都是"宁可报错也不静默改坏"）：

1. **只认白名单字段**：操作名、字段名、条目类型写错就报错 —— 不做"尽力而为"的忽略，
   否则用户以为改了、其实没改；
2. **source_id 必须是库里真实存在的 id**：挂不上就报错（这与 `route.plan` 里
   \"资料类条目必须有出处\"的规矩一致，不能让增量修改成为绕过它的后门）；
3. **应用完重排周号**（1..N 连续）：删掉一周之后编号必须还连续，否则界面上的"第 N 周"、
   统计与 `weeks` 计数会互相错位。
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

#: 允许修改的周级字段
WEEK_FIELDS = ("goal", "checkpoint")
#: 允许修改的条目字段
ITEM_FIELDS = ("kind", "title", "why", "est_minutes", "source_id")
#: 允许修改的计划级字段
PLAN_FIELDS = ("weekly_hours", "level", "mode_reason")
#: 条目类型（与 route.plan 的守则一致）
ITEM_KINDS = ("paper", "doc", "video", "code", "exercise")
#: 资料类条目（**必须**有出处）
RESOURCE_KINDS = ("paper", "doc", "video", "code")
#: 一次补丁最多几处改动（防止把"改一处"写成"重写整条"）
MAX_OPS = 50


class PatchError(ValueError):
    """补丁不合法 —— 这不是"忽略掉就行"的情况，必须让用户看见。"""


def _as_int(value: Any, *, what: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PatchError(f"{what} 必须是整数，收到 {type(value).__name__}")
    number = int(value)
    if not low <= number <= high:
        raise PatchError(f"{what} 必须在 {low}~{high} 之间，收到 {number}")
    return number


def _as_text(value: Any, *, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PatchError(f"{what} 必须是非空字符串")
    return value.strip()


def _check_source_id(value: Any, allowed: set[str] | None) -> str | None:
    if value in (None, "", "-"):
        return None
    if not isinstance(value, str):
        raise PatchError("source_id 必须是字符串或 null")
    if allowed is not None and value not in allowed:
        raise PatchError(f"source_id {value} 不在当前候选资料里（不许凭空引用）")
    return value


def _week_at(plan: dict[str, Any], week: Any) -> tuple[int, dict[str, Any]]:
    """按**人话的 1 起编号**取周；越界就报错（含可选的周号列表）。"""
    weeks = plan.get("weeks") or []
    index = _as_int(week, what="week", low=1, high=10**6)
    if index > len(weeks):
        raise PatchError(f"第 {index} 周不存在（这条路线只有 {len(weeks)} 周）")
    return index, weeks[index - 1]


def _item_at(entry: dict[str, Any], item: Any) -> tuple[int, dict[str, Any]]:
    """按 1 起编号取条目。"""
    items = entry.get("items") or []
    index = _as_int(item, what="item", low=1, high=10**6)
    if index > len(items):
        raise PatchError(f"这一周只有 {len(items)} 条，取不到第 {index} 条")
    return index, items[index - 1]


def _clean_item(raw: Any, allowed: set[str] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PatchError("条目必须是对象")
    kind = raw.get("kind")
    if kind not in ITEM_KINDS:
        raise PatchError(f"条目类型必须是 {'/'.join(ITEM_KINDS)} 之一")
    item: dict[str, Any] = {
        "kind": kind,
        "title": _as_text(raw.get("title"), what="title"),
    }
    item["source_id"] = _check_source_id(raw.get("source_id"), allowed)
    if kind in RESOURCE_KINDS and not item["source_id"]:
        raise PatchError("资料类条目必须挂一个真实的 source_id（拿不出来就别加这条）")
    if raw.get("why"):
        item["why"] = _as_text(raw.get("why"), what="why")
    if raw.get("est_minutes") is not None:
        item["est_minutes"] = _as_int(raw["est_minutes"], what="est_minutes", low=1, high=600)
    return item


def apply_ops(
    plan: dict[str, Any],
    ops: Any,
    *,
    allowed_source_ids: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """应用补丁 → `(新计划, 改动明细)`；任何一处不合法都抛 `PatchError`（不写半截结果）。"""
    if not isinstance(ops, list) or not ops:
        raise PatchError("ops 必须是非空数组（没有要改的就不要调用这个工具）")
    if len(ops) > MAX_OPS:
        raise PatchError(f"一次最多改 {MAX_OPS} 处，收到 {len(ops)} 处")

    new_plan = copy.deepcopy(plan)
    changes: list[dict[str, Any]] = []

    for position, op in enumerate(ops, start=1):
        if not isinstance(op, dict):
            raise PatchError(f"第 {position} 处改动不是对象")
        name = op.get("op")
        where = f"第 {position} 处（{name}）"

        if name == "set_plan_field":
            field = op.get("field")
            if field not in PLAN_FIELDS:
                raise PatchError(f"{where}：计划级只允许改 {'/'.join(PLAN_FIELDS)}")
            before = new_plan.get(field)
            value: Any = None
            if field == "weekly_hours":
                value = _as_int(op.get("value"), what="weekly_hours", low=1, high=40)
            else:
                value = _as_text(op.get("value"), what=field)
            new_plan[field] = value
            changes.append({"target": f"整条路线 / {field}", "before": before, "after": value})

        elif name == "set_week_field":
            index, entry = _week_at(new_plan, op.get("week"))
            field = op.get("field")
            if field not in WEEK_FIELDS:
                raise PatchError(f"{where}：周级只允许改 {'/'.join(WEEK_FIELDS)}")
            before = entry.get(field)
            value = _as_text(op.get("value"), what=field)
            entry[field] = value
            changes.append({"target": f"第 {index} 周 / {field}", "before": before, "after": value})

        elif name == "set_item_field":
            index, entry = _week_at(new_plan, op.get("week"))
            item_index, item = _item_at(entry, op.get("item"))
            field = op.get("field")
            if field not in ITEM_FIELDS:
                raise PatchError(f"{where}：条目只允许改 {'/'.join(ITEM_FIELDS)}")
            before = item.get(field)
            value = None
            if field == "kind":
                if op.get("value") not in ITEM_KINDS:
                    raise PatchError(f"条目类型必须是 {'/'.join(ITEM_KINDS)} 之一")
                value = op["value"]
            elif field == "est_minutes":
                value = _as_int(op.get("value"), what="est_minutes", low=1, high=600)
            elif field == "source_id":
                value = _check_source_id(op.get("value"), allowed_source_ids)
                if not value and item.get("kind") in RESOURCE_KINDS:
                    raise PatchError(
                        "资料类条目不能把 source_id 改成空（要么换一条真实资料，要么删掉这条）"
                    )
            else:
                value = _as_text(op.get("value"), what=field)
            item[field] = value
            changes.append(
                {
                    "target": f"第 {index} 周 第 {item_index} 条 / {field}",
                    "before": before,
                    "after": value,
                }
            )

        elif name == "add_item":
            index, entry = _week_at(new_plan, op.get("week"))
            item = _clean_item(op.get("item"), allowed_source_ids)
            entry.setdefault("items", []).append(item)
            changes.append(
                {
                    "target": f"第 {index} 周 / 新增第 {len(entry['items'])} 条",
                    "before": None,
                    "after": item.get("title"),
                }
            )

        elif name == "remove_item":
            index, entry = _week_at(new_plan, op.get("week"))
            item_index, item = _item_at(entry, op.get("item"))
            entry["items"].pop(item_index - 1)
            changes.append(
                {
                    "target": f"第 {index} 周 / 删除第 {item_index} 条",
                    "before": item.get("title"),
                    "after": None,
                }
            )

        elif name == "add_week":
            raw = op.get("week")
            if not isinstance(raw, dict):
                raise PatchError(f"{where}：week 必须是一个对象")
            entry = {
                "goal": _as_text(raw.get("goal"), what="goal"),
                "checkpoint": _as_text(raw.get("checkpoint"), what="checkpoint"),
                "items": [],
            }
            for raw_item in raw.get("items") or []:
                entry["items"].append(_clean_item(raw_item, allowed_source_ids))
            new_plan.setdefault("weeks", []).append(entry)
            changes.append(
                {
                    "target": f"新增第 {len(new_plan['weeks'])} 周",
                    "before": None,
                    "after": entry["goal"],
                }
            )

        elif name == "remove_week":
            index, entry = _week_at(new_plan, op.get("week"))
            if len(new_plan.get("weeks") or []) <= 1:
                raise PatchError("不能删掉最后一周（路线至少要有一周）")
            new_plan["weeks"].pop(index - 1)
            changes.append(
                {"target": f"删除第 {index} 周", "before": entry.get("goal"), "after": None}
            )

        else:
            raise PatchError(
                f"{where}：不认识的操作名（可用：set_plan_field / set_week_field / set_item_field / add_item / remove_item / add_week / remove_week）"
            )

    _renumber(new_plan)
    new_plan["stats"] = _restats(new_plan, plan.get("stats") or {})
    return new_plan, changes


def _renumber(plan: dict[str, Any]) -> None:
    """周号重排成 1..N（删周后编号必须连续，否则界面与统计会错位）。"""
    for position, entry in enumerate(plan.get("weeks") or [], start=1):
        entry["idx"] = position


def _restats(plan: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    """条目数/总分钟数必须跟着变 —— 统计不跟着改就是假数据。"""
    items = [item for entry in plan.get("weeks") or [] for item in entry.get("items") or []]
    minutes = sum(int(item.get("est_minutes") or 0) for item in items)
    fresh = dict(stats)
    fresh["items"] = len(items)
    fresh["est_minutes"] = minutes
    return fresh


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_patch(content: str) -> dict[str, Any]:
    """从模型回复里取出补丁 JSON。

    容错与"绝不猜"的边界：允许 ```json 围栏、允许前后有说明文字（取第一个花括号块）；
    取不到就抛错 —— 不合法就当没改，绝不写半截。
    """
    text = (content or "").strip()
    if not text:
        raise PatchError("模型没有返回任何内容")
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise PatchError("回复里找不到 JSON 对象")
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PatchError(f"补丁不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise PatchError("补丁必须是 JSON 对象")
    return payload
