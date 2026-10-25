"""工具渐进披露：**核心常驻 + 会话内按需加载**（`tool.search` / `tool.describe`）。

为什么要这一层（E4 实测，docs/26）：把全部 11 个工具的 **schema 常驻**在请求里时，真机 E4 的
逐轮工具名准确率只有 **0.067**，实际调用 64 次对 gold 15 次 —— 模型不是"不会用"，是**选不准**：
十几个函数声明挤在一个 payload 里，名字相近的工具（`source.fetch` / `source.grade`、
`topic.build_map` / `route.plan`）彼此干扰，还都要为它们付每一轮的 token。

做法照 Hermes 的两层目录：

1. **核心常驻**（:data:`CORE_TOOLS`）：检索、取原文、存笔记 —— 每一轮几乎都会用到，必须随叫随到；
2. **按需加载**：其余的放进目录，模型先 :func:`search_entries`（只有名字 + 一句话）看有什么，
   再 :func:`load`（拿完整 schema 并把它们加进**本会话**的可用集），之后才能直接调用。
   没加载就直调一律回 ``tool_not_loaded``（:func:`not_loaded_message`），并提示先 describe。

**为什么核心集是这三个**：它们是"默认工具集"（``runtime.registry.enabled_toolsets`` 的
``research`` 分组，``tools/__init__.py::register_tools`` 注册的那 11 个）里**每一步流程都会用到**的
那三个 —— 提示词的三段流程（检索 → 取原文 → 出产物）第一、二步就压在这两个上，产物一步的
``note.save`` 是唯一"输出"工具。其余 8 个要么只在特定意图下才用（地图/路线/判权/委派/写代码/技能），
要么本身就该按需取（``skill.load`` 读的是一篇几万字的全文）。核心集**必须是默认集里的成员**：
:func:`tests.runtime.test_tool_disclosure` 用包含关系钉住，改名/移出工具集会立刻红。

**加载状态存在哪**：本模块的进程内表 :data:`_STATE`，键是 ``session_id``。
- **会话隔离**：键就是会话 id，A 会话 describe 过的工具，B 会话拿不到（新会话必须重新 describe）；
- 为什么不落库：加载集是"这一轮对话里模型已经看过 schema 的工具"，它换不来任何跨进程价值，
  而落库会引入"重启后加载集与真实 payload 不一致"的新漂移面。进程重启后模型手上没有那份 schema，
  本来就该重新 describe —— ``tool_not_loaded`` 的文案直接这么教它。
- **开关照旧**：目录与 describe 都走 :func:`allowed_specs`（工具集 + 特性开关的同一处收敛），
  关掉的工具集既列不出来也加载不到。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from scholarscout.config import Config
from scholarscout.runtime import registry

#: 核心常驻工具（每轮都发给模型的那些）。依据见模块顶注：流程三步各一个 —— 但这还不够。
#: **2026-10-25 修复（默认开启披露后暴露的真坑）**：`file.list` / `file.read` 也必须在核心集里。
#: 它们在 `enable_files` 默认开后是「搜一下 X 并读一份本地文件」这类请求的**第二步**，
#: 却在目录里——模型扫不到就直接调，回 `tool_not_loaded` 白烧一轮；即使它去 search，
#: `tool.search` 的关键词也得恰好命中。只读工具没有审批成本、体量也小，常驻比藏起来划算。
#: 代价：披露档的常驻声明从 5 个（3 核心 + 2 元）变成 7 个 —— 仍然远小于 15 个全量。
CORE_TOOLS: tuple[str, ...] = (
    "file.list",
    "file.read",
    "note.save",
    "research.search",
    "source.fetch",
)

#: 元工具本身：它们**常驻**，且**不进目录**（目录是"还没加载的能力"，不该列自己）。
META_TOOLS: tuple[str, ...] = ("tool.describe", "tool.search")

#: 一次 `tool.describe` 最多加载几个：防"一口气把目录全拉回来"，那样渐进披露就白做了。
MAX_DESCRIBE = 3


@dataclass
class _SessionState:
    """一个会话的披露状态：已加载的工具名 + 这个会话**允许**的工具全集（目录范围）。"""

    loaded: set[str] = field(default_factory=set)
    universe: tuple[str, ...] | None = None


_STATE: dict[str, _SessionState] = {}
_LOCK = threading.Lock()


def _state(session_id: str | None) -> _SessionState:
    """取（必要时新建）会话状态。``session_id`` 为空时用一个固定的匿名桶 —— CLI 兜底。"""
    key = session_id or "-"
    with _LOCK:
        return _STATE.setdefault(key, _SessionState())


def remember_universe(session_id: str | None, names: Sequence[str]) -> None:
    """记下这个会话**允许**的工具全集（``TurnContext.tools()`` 的产物，已过特性开关）。

    为什么记：目录必须**只列当前会话真正允许的工具**。会话的 ``toolsets`` 可能来自前端
    （RPC ``session.create``），本模块拿不到它，所以由调用方（``loop.run_turn``）在
    取工具那一步顺手登记 —— 一处登记，`tool.search` / `tool.describe` / 直调闸三处共用。
    """
    _state(session_id).universe = tuple(names)


def universe(session_id: str | None, config: Config) -> tuple[str, ...]:
    """会话允许的工具全集：**优先用登记过的**，没登记就按特性开关现算（CLI / 单测直调）。

    "没登记"只发生在没有走 ``run_turn`` 的调用点（脚本、单测）；它算出来的就是产品默认那一套，
    所以不会静默放宽。
    """
    noted = _state(session_id).universe
    if noted is not None:
        return noted
    return tuple(
        spec.name
        for spec in registry.get_tools(
            registry.enabled_toolsets(
                None,
                enable_kb=config.features.enable_kb,
                enable_files=config.features.enable_files,
            )
        )
    )


def loaded_names(session_id: str | None) -> frozenset[str]:
    """本会话已 describe 过的工具名（只读快照，调用方改不了内部集合）。"""
    with _LOCK:
        return frozenset(_STATE.get(session_id or "-", _SessionState()).loaded)


def load(session_id: str | None, names: Sequence[str]) -> tuple[str, ...]:
    """把工具加进本会话的可用集（幂等），返回加载后的快照。"""
    with _LOCK:
        state = _STATE.setdefault(session_id or "-", _SessionState())
        state.loaded.update(names)
        return tuple(sorted(state.loaded))


def reset(session_id: str | None = None) -> None:
    """清掉加载状态。**只给测试与"新开会话"用**：传 ``None`` 清全部。"""
    with _LOCK:
        if session_id is None:
            _STATE.clear()
        else:
            _STATE.pop(session_id or "-", None)


def is_core(name: str) -> bool:
    return name in CORE_TOOLS


def is_meta(name: str) -> bool:
    return name in META_TOOLS


def is_available(name: str, *, session_id: str | None) -> bool:
    """这个名字现在能不能**直接调**：核心 / 元工具 / 本会话已加载，三者之一。

    注意它**不判工具集开关** —— 开关已经体现在"目录里有没有它"（:func:`allowed_specs`）与
    会话的 payload 上；这里只回答"模型有没有见过它的 schema"。
    """
    return is_core(name) or is_meta(name) or name in loaded_names(session_id)


def not_loaded_message(name: str) -> str:
    """未加载直调的**可读**回话：说清原因 + 下一步怎么做（模型据此自救，不用人教）。"""
    return (
        f"tool_not_loaded：{name} 不在本会话的常驻工具里"
        f"（常驻只有 {', '.join(CORE_TOOLS)} 与 tool.search / tool.describe）。"
        f'先调 tool.describe({{"names": ["{name}"]}}) 取回它的参数定义，'
        "之后本会话内就能直接调用；不要换个名字重试，也不要假装调用成功。"
    )


def declared_specs(
    specs: Sequence[registry.ToolSpec], *, session_id: str | None
) -> list[registry.ToolSpec]:
    """这一轮真正写进 ``tools=`` 的那几个：核心 + 元工具 + 本会话已加载。

    ``specs`` 必须是**本会话允许**的全集（``TurnContext.tools()``）；这里只做减法，
    不做加法 —— 所以已加载但会话不允许的工具也不会被带回来。
    """
    loaded = loaded_names(session_id)
    return [
        spec for spec in specs if is_core(spec.name) or is_meta(spec.name) or spec.name in loaded
    ]


def allowed_specs(config: Config, *, session_id: str | None = None) -> list[registry.ToolSpec]:
    """目录范围：本会话允许的、**排除元工具自己**的工具（按名字排序，顺序稳定）。

    按**名字**查注册表（:func:`registry.get_spec`），不是 ``registry.get_tools`` ——
    后者按**工具集**筛，把工具名当工具集名传进去会一条都匹配不上，目录永远是空的。
    这也正是 ``tests/runtime/test_tool_disclosure.py`` 第一条目录用例钉住的东西。
    """
    allowed = set(universe(session_id, config))
    specs: list[registry.ToolSpec] = []
    for name in sorted(allowed):
        spec = registry.get_spec(name)
        if spec is not None and not is_meta(spec.name):
            specs.append(spec)
    return specs


def one_line(description: str, *, limit: int = 80) -> str:
    """把工具描述压成**一句话**：到第一个句号为止，超长截断加省略号。

    目录里只给这一句 —— 完整描述是 schema 的一部分，要它就得 describe（那样才会计入
    "本会话可用集"）。压到 80 字符是为了让整页目录控制在一屏以内。
    """
    text = " ".join((description or "").split())
    head = text.split("。", 1)[0].strip() or text
    return head if len(head) <= limit else head[: limit - 1] + "…"


#: 命中门槛：连续命中少于这么长就算"没匹配上"（与 ``skills/index.py::MIN_HIT`` 同一个值）。
#: 为什么要门槛：一两个字符的命中几乎条条都沾，排序会退化成按名字 —— 那不是搜索。
MIN_HIT: int = 3


def _longest_hit(query: str, field: str) -> tuple[int, int]:
    """``query`` 在 ``field`` 里**连续命中**的最长片段：返回 ``(长度, 最早位置)``。

    与 ``skills/index.py::_longest_hit`` 逐字同一套口径（大小写不敏感、最长连续子串），
    这样"找工具"和"找技能"的手感一致 —— 模型不用学两套匹配规则。
    """
    if not query or not field:
        return 0, 0
    for length in range(len(query), 0, -1):
        for start in range(len(query) - length + 1):
            piece = query[start : start + length]
            at = field.find(piece)
            if at >= 0:
                return length, at
    return 0, 0


def search_entries(
    config: Config, *, query: str | None, limit: int, session_id: str | None = None
) -> tuple[list[dict[str, Any]], int]:
    """按字符匹配翻目录，返回 ``(这一页, 命中总数)``；每项只有 ``name`` + 一句话用途。

    排序：连续命中越长越靠前 → 命中在名字里的靠前 → 名字字典序（同一棵树每次同一顺序）。
    不给 query 就按名字回全部（受 ``limit`` 限制）。
    """
    text = (query or "").strip().casefold()
    specs = allowed_specs(config, session_id=session_id)
    if not text:
        page = specs[:limit]
        return [_entry(spec, 0) for spec in page], len(specs)

    floor = min(MIN_HIT, len(text))
    ranked: list[tuple[tuple[int, int, str], registry.ToolSpec, int]] = []
    for spec in specs:
        name_run, _ = _longest_hit(text, spec.name.casefold())
        line = one_line(spec.description)
        desc_run, _ = _longest_hit(text, line.casefold())
        longest = max(name_run, desc_run)
        if longest < floor:
            continue
        ranked.append(((-longest, 0 if name_run else 1, spec.name.casefold()), spec, longest))
    ranked.sort(key=lambda item: item[0])
    return [_entry(spec, hit) for _, spec, hit in ranked[:limit]], len(ranked)


def _entry(spec: registry.ToolSpec, hit: int) -> dict[str, Any]:
    """目录项：**只有名字与一句话**（不带 parameters，不带完整 description）。"""
    return {"name": spec.name, "purpose": one_line(spec.description), "hit": hit}


def describe_payload(spec: registry.ToolSpec) -> dict[str, Any]:
    """describe 回的**完整 schema**（模型拿到它才有资格构造参数）。"""
    return {
        "name": spec.name,
        "wire_name": registry.wire_name(spec.name),
        "toolset": spec.toolset,
        "description": spec.description,
        "parameters": spec.parameters,
    }


def payload_chars(prompt: str, specs: Sequence[registry.ToolSpec]) -> int:
    """模型每一轮**实际看到的工具清单**大小 = 系统提示 + 工具声明 JSON 的字符数。

    为什么把两段加在一起算：``tools=`` 那段 schema 与系统提示是**同一笔上下文账单**，
    也是"选不准"的真正来源（E4：12 个声明常驻 → 逐轮工具名准确率 0.067）。只量系统提示
    会漏掉这次改动省下来的那一大块（见 ``tests/runtime/test_tool_disclosure.py`` 的改前/改后数字）。
    """
    return len(prompt) + len(json.dumps(registry.openai_tools(specs), ensure_ascii=False))
