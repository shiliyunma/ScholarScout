"""检索来源参数（与 `config.example.yaml` 的 ``search:`` 段一一对应）。

**决议 2**：第一版只做免 key 的来源。``available`` 列出本版真正实现了的 provider，
没实现的名字会被记为 skipped，而不是悄悄忽略。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 本版真正实现的来源（M3 只有 arxiv；其余在 M3 末补齐）
IMPLEMENTED = ("arxiv",)
#: 配置里允许写的来源名（含未实现的，便于前向兼容）
KNOWN = ("arxiv", "openalex", "semantic_scholar", "github")

DEFAULT_MIN_INTERVAL = {
    "arxiv": 3.0,  # arXiv 连续请求会被 406 限速
    "openalex": 0.5,
    "semantic_scholar": 1.0,
    "github": 1.0,
}


@dataclass(frozen=True)
class SearchSettings:
    providers: tuple[str, ...] = IMPLEMENTED
    default_limit: int = 10
    per_provider_limit: int = 8
    min_interval_s: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_MIN_INTERVAL))

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> SearchSettings:
        section = raw or {}
        names = section.get("providers")
        providers = (
            tuple(str(name) for name in names) if isinstance(names, list) and names else IMPLEMENTED
        )
        intervals = dict(DEFAULT_MIN_INTERVAL)
        raw_intervals = section.get("min_interval_s")
        if isinstance(raw_intervals, dict):
            for key, value in raw_intervals.items():
                if isinstance(value, int | float) and not isinstance(value, bool):
                    intervals[str(key)] = float(value)
        return cls(
            providers=providers,
            default_limit=int(section.get("default_limit", 10)),
            per_provider_limit=int(section.get("per_provider_limit", 8)),
            min_interval_s=intervals,
        )

    def interval_for(self, provider: str, fallback: float) -> float:
        return float(self.min_interval_s.get(provider, fallback))
