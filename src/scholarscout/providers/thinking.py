"""思考深度（reasoning effort）的**单一真值源**：界面档位 → 线上参数。

为什么单独一个文件：档位映射会被三处用到（请求体、RPC 给界面的下拉、设置白名单），
三处各写一份必然漂移。这里只放纯函数，不 import 任何上层模块。

**本服务的真实档位只有三档**（`GET /models` 自己声明了
``effort.supported_levels = ["low", "high", "max"]``、``default_level = "high"``）。
用户要的是「最小／低／中／高／最高」五档 + 关，所以这里**如实做并档映射**，
并让界面文案直接标出真实档位（"中（high）"）—— 不假装五档各不相同。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

#: 界面档位（弱 → 强），最后一个是「关」。**顺序即界面顺序**。
LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "max", "none")

#: 界面上每个档位的中文名（括号里的真实档位由 :func:`label_for` 拼上去）。
LEVEL_NAMES: dict[str, str] = {
    "minimal": "最小",
    "low": "低",
    "medium": "中",
    "high": "高",
    "max": "最高",
    "none": "关",
}

#: 五档 → 本服务真实档位（DeepSeek 官方映射表：minimal/low→low，medium/high→high，max→max）。
LEVEL_TO_EFFORT: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "max": "max",
}

#: 真实档位（弱 → 强）。服务端不声明时按官方文档这套兜底。
DEFAULT_EFFORTS: tuple[str, ...] = ("low", "high", "max")

#: 默认档：`medium` → 线上 `high`，与"不传 reasoning_effort 时服务端默认 high"完全等价，
#: 所以升级成显式参数不会改变现有行为（这一点由测试钉住）。
DEFAULT_LEVEL = "medium"

#: 请求体里承载"思考"的两个字段（降级时要一起摘掉、也要能一起识别）。
WIRE_FIELDS: tuple[str, ...] = ("reasoning_effort", "thinking")

#: 关档：只发 thinking.type=disabled，**不发** reasoning_effort（官方文档的口径）。
DISABLED_FIELDS: dict[str, object] = {"thinking": {"type": "disabled"}}


class UnknownLevelError(ValueError):
    """不认识的档位。**直接报错**，不许静默回默认档（那等于用户以为改了、其实没改）。"""


def normalize_level(value: object) -> str:
    level = str(value if value is not None else "").strip().lower()
    if not level:
        return DEFAULT_LEVEL
    if level not in LEVELS:
        raise UnknownLevelError(f"未知的思考档位：{value!r}（允许：{'、'.join(LEVELS)}）")
    return level


def clamp_effort(effort: str, supported: Iterable[str]) -> str:
    """把档位压到服务端真支持的那一档：**就近向弱**，绝不悄悄换成更强的档。

    没有更弱的可选时才取该支持集里最弱的一档（与 Hermes 的 ``clamp_effort`` 同一口径）。
    """
    order = [item for item in DEFAULT_EFFORTS if item in set(supported)]
    if not order:
        order = list(DEFAULT_EFFORTS)
    if effort in order:
        return effort
    weaker = [
        item for item in order if DEFAULT_EFFORTS.index(item) <= DEFAULT_EFFORTS.index(effort)
    ]
    return weaker[-1] if weaker else order[0]


@dataclass(frozen=True)
class ReasoningPlan:
    """一个档位最终要发出去的东西（可序列化给界面看，也可直接进请求体）。"""

    level: str
    #: 线上真实档位；``None`` = 不发 reasoning_effort（关档）
    effort: str | None
    #: 服务端是否声明了该档位；False 表示这里做了就近降级
    exact: bool = True

    @property
    def disabled(self) -> bool:
        return self.effort is None

    def wire_fields(self) -> dict[str, object]:
        """请求体里该加的字段（关档只加 thinking，不加 reasoning_effort）。"""
        if self.effort is None:
            return dict(DISABLED_FIELDS)
        return {"thinking": {"type": "enabled"}, "reasoning_effort": self.effort}

    def label(self) -> str:
        """界面文案：**如实标出真实档位**（"中（high）"）。"""
        name = LEVEL_NAMES[self.level]
        if self.effort is None:
            return f"{name}（不发思考参数）"
        return f"{name}（{self.effort}）"


def plan_for(
    level: object,
    *,
    supported: Iterable[str] = DEFAULT_EFFORTS,
    thinking: bool = True,
) -> ReasoningPlan:
    """界面档位 → 真实参数。

    ``thinking=False``（旧的布尔开关）与 ``level="none"`` 等价：两者都是"关"。
    这样老配置（`llm.chat.thinking: false`）不需要迁移也仍然生效。
    """
    normalized = normalize_level(level)
    if normalized == "none" or not thinking:
        return ReasoningPlan(level="none", effort=None, exact=True)
    wanted = LEVEL_TO_EFFORT[normalized]
    effort = clamp_effort(wanted, supported)
    return ReasoningPlan(level=normalized, effort=effort, exact=(effort == wanted))


def options(
    *,
    supported: Iterable[str] = DEFAULT_EFFORTS,
    thinking: bool = True,
) -> list[dict[str, object]]:
    """给界面下拉用的六个选项（label 里带真实档位，value 是界面档位名）。"""
    out: list[dict[str, object]] = []
    for level in LEVELS:
        plan = plan_for(level, supported=supported, thinking=thinking)
        out.append(
            {
                "value": level,
                "label": plan.label(),
                "effort": plan.effort,
                "disabled": plan.disabled,
            }
        )
    return out


def strip_wire_fields(payload: dict[str, object]) -> dict[str, object] | None:
    """摘掉思考相关字段的请求体副本；本来就没有这些字段时返回 ``None``（无从降级）。"""
    if not any(name in payload for name in WIRE_FIELDS):
        return None
    stripped = {key: value for key, value in payload.items() if key not in WIRE_FIELDS}
    return stripped


def mentions_reasoning_param(text: str) -> bool:
    """400 的正文是不是在说"这两/一个参数不认"。

    宁可窄一点：只有提到 reasoning / thinking 才算 —— 否则把真正的参数错误也重试一遍，
    只是把一次失败变成两次（还多花时间）。
    """
    low = (text or "").lower()
    return any(token in low for token in ("reasoning_effort", "reasoning effort", "thinking"))
