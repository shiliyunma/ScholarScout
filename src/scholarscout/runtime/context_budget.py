"""喂回模型之前，把工具结果瘦身 —— **这是最直接的省钱手段**。

## 为什么需要它（2026-10-05 实测数据）

一轮对话里模型会被调用 ~10 次，而**每一次都要把前面所有工具结果重发一遍**：

| 项 | 实测 |
|---|---|
| 系统提示词 | 1,181 字符 ≈ 393 tokens（每轮都发） |
| 工具声明（6 个） | 4,687 字符 ≈ 1,562 tokens（每轮都发） |
| 单条工具结果最长 | 9,583 字符 ≈ 3,194 tokens |
| 库里 38 条工具结果合计 | 97,326 字符 ≈ 32,442 tokens |

于是同一段 JSON 在 10 轮里会被计费 5～9 次（越靠前的轮次被重发得越多）。
`research.search` 每次回 8～12 条，每条都带 `snippet`（摘要片段）——
**而多次检索往往会捞回同一批来源**，摘要就被反复计费。

## 三条策略（都在这里，纯函数便于单测）

1. **去重**：本回合已经发过的 `source_id`，再出现时只回 id/title（摘要不再重复发）；
2. **截断**：`snippet` 截到 N 字符，整条结果也设上限，超出部分留一行说明；
3. **折叠**：更早的工具结果（默认只保留最近 3 条全文）压成一行占位符 —— 需要细节时模型可以再查一次，
   比把 3 万 tokens 的历史一路带着便宜得多。

**不能省的东西**：`source_id` 一个都不能丢 —— 它是引用的锚点，丢了模型就会编 id（反幻觉守卫会拦，
但那是把成本换成了质量事故）。
"""

from __future__ import annotations

import json
from typing import Any

#: 单条工具结果喂回模型时的字符上限（超出截断并留说明）
MAX_RESULT_CHARS = 4000
#: 搜索结果里 snippet（摘要片段）的字符上限
MAX_SNIPPET_CHARS = 200
#: 折叠时保留最近 N 条工具结果的全文
KEEP_RECENT_TOOL_RESULTS = 3
#: 折叠后的占位符（模型看到它就知道"这条已经处理过，别再依赖细节"）
COLLAPSED_TEMPLATE = "[此结果已在上面处理过，细节已折叠以节省上下文；需要时请重新调用 {tool}]"
#: 截断标记
TRUNCATED_TEMPLATE = "\n…（本条结果过长，已截断，原始 {total} 字符）"

#: 搜索结果条目里值得喂给模型的字段（其余字段是给界面/数据库用的，模型不需要）
_ITEM_FIELDS = ("source_id", "title", "url", "year", "source_type")


def _short(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _project_items(items: Any, seen_source_ids: set[str], *, snippet_chars: int) -> list[Any]:
    if not isinstance(items, list):
        return []
    projected: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "")
        slim: dict[str, Any] = {}
        for field in _ITEM_FIELDS:
            if item.get(field) not in (None, ""):
                slim[field] = item[field]
        if source_id and source_id in seen_source_ids:
            # 已经发过：只留锚点，摘要不再重复计费
            slim["snippet"] = "（本条摘要已在前面给过，不再重复）"
        elif item.get("snippet"):
            slim["snippet"] = _short(item.get("snippet"), snippet_chars)
        projected.append(slim)
        if source_id:
            seen_source_ids.add(source_id)
    return projected


def project_tool_result(
    result: Any,
    *,
    seen_source_ids: set[str] | None = None,
    max_chars: int = MAX_RESULT_CHARS,
    snippet_chars: int = MAX_SNIPPET_CHARS,
) -> str:
    """把一次工具结果压成要喂回模型的文本。

    - 形如 ``{"ok":…, "data":{"items":[…], "provider_status":[…]}, "refs":…}`` 的检索结果：
      逐条投影（去重 + snippet 截断），并保留 ``provider_status``（模型需要知道哪个来源没成功）；
    - 其它形状：直接 JSON，超长则截断。
    - ``seen_source_ids`` 会被就地更新（调用方在整轮里共用一份）。
    """
    seen = seen_source_ids if seen_source_ids is not None else set()
    payload = result

    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            slim_data: dict[str, Any] = {
                "items": _project_items(data.get("items"), seen, snippet_chars=snippet_chars)
            }
            for keep in ("provider_status", "query", "total", "note"):
                if data.get(keep) not in (None, "", [], {}):
                    slim_data[keep] = data[keep]
            # 只留 ok + data：`refs`（source_ids 列表）是给界面与应用用的，
            # 每条 item 里已经有 source_id，模型不需要再看一遍这份重复的 id 列表
            payload = {"ok": result.get("ok", True), "data": slim_data}

    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + TRUNCATED_TEMPLATE.format(total=len(text))


def collapse_old_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
) -> int:
    """把更早的工具结果折叠成一行占位符，只保留最近 ``keep_recent`` 条全文。

    返回被折叠的条数（用于测试与日志）。**只会动 tool 消息** ——
    用户/助手消息是对话本身，不能改。
    """
    indices = [index for index, message in enumerate(messages) if message.get("role") == "tool"]
    to_collapse = indices[:-keep_recent] if keep_recent > 0 else indices
    collapsed = 0
    for index in to_collapse:
        message = messages[index]
        content = str(message.get("content") or "")
        if content.startswith("[此结果已在上面处理过"):
            continue
        tool_name = str(message.get("name") or "该工具")
        message["content"] = COLLAPSED_TEMPLATE.format(tool=tool_name)
        collapsed += 1
    return collapsed


def estimate_tokens(text: str) -> int:
    """粗略估 token（中文≈1 字 1 token、英文≈4 字符 1 token，取保守的 chars/3）。"""
    return int(len(text or "") / 3)


def messages_token_report(messages: list[dict[str, Any]]) -> dict[str, int]:
    """按角色拆分当前请求体的大小 —— 用来回答"钱花在哪"。"""
    report: dict[str, int] = {}
    for message in messages:
        role = str(message.get("role") or "?")
        body = message.get("content")
        text = body if isinstance(body, str) else json.dumps(body or "", ensure_ascii=False)
        if message.get("tool_calls"):
            text += json.dumps(message["tool_calls"], ensure_ascii=False)
        report[role] = report.get(role, 0) + estimate_tokens(text)
    report["total"] = sum(value for key, value in report.items() if key != "total")
    return report
