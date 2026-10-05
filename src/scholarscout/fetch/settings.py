"""抓取参数（与 `config.example.yaml` 的 ``fetch:`` 段一一对应）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_USER_AGENT = "ScholarScout/0.1 (academic research assistant; contact: <you@example.com>)"

#: 与 fetch/http.py 的默认豁免一致（这里再导出一次，方便 config 层引用而不反向依赖 http）
DEFAULT_ROBOTS_EXEMPT_HOSTS: tuple[str, ...] = ("export.arxiv.org",)


@dataclass(frozen=True)
class FetchSettings:
    user_agent: str = DEFAULT_USER_AGENT
    timeout_s: float = 20.0
    retries: int = 3
    max_bytes: int = 20_000_000
    cache_ttl_hours: float = 6.0
    respect_robots: bool = True
    #: 跳过 robots 检查的 API 主机（robots 是给爬虫看的，不是给 API 客户端看的）
    robots_exempt_hosts: tuple[str, ...] = DEFAULT_ROBOTS_EXEMPT_HOSTS

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> FetchSettings:
        section = raw or {}
        hosts = section.get("robots_exempt_hosts")
        return cls(
            user_agent=str(section.get("user_agent", DEFAULT_USER_AGENT)),
            timeout_s=float(section.get("timeout_s", 20.0)),
            retries=int(section.get("retries", 3)),
            max_bytes=int(section.get("max_bytes", 20_000_000)),
            cache_ttl_hours=float(section.get("cache_ttl_hours", 6.0)),
            respect_robots=bool(section.get("respect_robots", True)),
            robots_exempt_hosts=(
                tuple(str(host) for host in hosts)
                if isinstance(hosts, list)
                else DEFAULT_ROBOTS_EXEMPT_HOSTS
            ),
        )

    def cache_dir(self, home: Path) -> Path:
        return home / "data" / "cache"
