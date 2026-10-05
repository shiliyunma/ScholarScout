"""抓取参数（与 `config.example.yaml` 的 ``fetch:`` 段一一对应）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_USER_AGENT = "ScholarScout/0.1 (academic research assistant; contact: <you@example.com>)"


@dataclass(frozen=True)
class FetchSettings:
    user_agent: str = DEFAULT_USER_AGENT
    timeout_s: float = 20.0
    retries: int = 3
    max_bytes: int = 20_000_000
    cache_ttl_hours: float = 6.0
    respect_robots: bool = True

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> FetchSettings:
        section = raw or {}
        return cls(
            user_agent=str(section.get("user_agent", DEFAULT_USER_AGENT)),
            timeout_s=float(section.get("timeout_s", 20.0)),
            retries=int(section.get("retries", 3)),
            max_bytes=int(section.get("max_bytes", 20_000_000)),
            cache_ttl_hours=float(section.get("cache_ttl_hours", 6.0)),
            respect_robots=bool(section.get("respect_robots", True)),
        )

    def cache_dir(self, home: Path) -> Path:
        return home / "data" / "cache"
