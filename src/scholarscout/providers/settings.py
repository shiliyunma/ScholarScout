"""检索来源参数（与 `config.example.yaml` 的 ``search:`` 段一一对应）。

## 为什么"通用搜索"没有一键免 key 的答案

实测（2026-10-05，本机、国内网络）：
- ``html.duckduckgo.com`` / ``lite.duckduckgo.com``：ConnectTimeout（要梯子）
- 公共 SearxNG 实例：超时/被重置
- ``cn.bing.com/search``：HTTP 200 能解析，但 **robots.txt 明确 Disallow ``/search``**
- 百度/Google 结果页同样在 robots 里被禁

也就是说：**抓搜索引擎结果页要么不通、要么违约**。本项目对 robots 的口径是"只豁免官方 API 主机，
网页一律遵守"（docs/03 §7），所以默认不抓搜索页。合规的通用检索只有三条路：

1. **自建 SearxNG**（免 key、自己可控、返回 JSON）→ 配 ``search.searxng_base_url``
2. **商用检索 API 的免费额度**（Brave / Google CSE / Tavily / Serper 注册即得）→ 配 ``.env``
3. **抓结果页**（Bing/DuckDuckGo）→ 需要你显式打开 ``allow_searchpage_scraping``，
   这是把对应主机加进 robots 豁免名单，属于**你的选择**，不是默认行为

## 默认来源 `auto`

``providers: ["auto"]`` 表示"能用哪个就用哪个"：arxiv + 已配置的 SearxNG 实例 +
已配 key 的商用来源。这样**填了 key 就自动生效**，没填的来源不会在每次检索里刷一堆
``missing_api_key``；需要抓结果页或要梯子的来源不会被 auto 带上（否则每次检索都要等超时）。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: 免 key 来源（github 未认证也能用：Search API 10 次/分钟）
KEYLESS = ("arxiv", "github", "bing", "duckduckgo", "searxng")
#: **可选**密钥 → 环境变量：没有也能用，只是额度更低/间隔更长
OPTIONAL_TOKENS = {"github": "GITHUB_TOKEN"}
#: 商用来源 → 密钥环境变量名
COMMERCIAL_KEYS = {
    "tavily": "TAVILY_API_KEY",
    "serper": "SERPER_API_KEY",
    "brave": "BRAVE_API_KEY",
    "google_cse": "GOOGLE_CSE_KEY",
}
#: 本版真正实现了的来源
IMPLEMENTED = ("arxiv", "github", "bing", "duckduckgo", "searxng", *COMMERCIAL_KEYS)
#: 配置里允许写的来源名（含尚未实现的学术索引，便于前向兼容）
KNOWN = (*IMPLEMENTED, "auto", "openalex", "semantic_scholar")

#: `providers: ["auto"]` 的哨兵值（见模块文档）
AUTO = "auto"
#: 默认值：auto（见模块文档）
DEFAULT_PROVIDERS = ("auto",)
#: auto 的装配顺序（学术 → 代码/教程 → 通用检索 → 商用；顺序只影响结果展示顺序）
AUTO_ORDER = ("arxiv", "github", "searxng", "google_cse", "brave", "tavily", "serper")

#: 抓结果页时才需要豁免 robots 的主机（DuckDuckGo 同样如此，但其网络在国内本就不通）
SEARCHPAGE_HOSTS = ("cn.bing.com", "www.bing.com", "html.duckduckgo.com", "duckduckgo.com")

DEFAULT_MIN_INTERVAL = {
    "arxiv": 3.0,  # arXiv 连续请求会被 406 限速
    "bing": 1.0,
    "duckduckgo": 1.5,
    "searxng": 1.0,
    "tavily": 0.5,
    "serper": 0.5,
    "brave": 1.0,
    "google_cse": 0.5,
    "openalex": 0.5,
    "semantic_scholar": 1.0,
    # 注意：**故意不写 github** —— 它的间隔取决于有没有配 token
    # （未认证 Search API 10 次/分钟 → 6s；认证 30 次/分钟 → 2s），
    # 由 providers/github.py 自己算。写进这张表会覆盖它：1s 间隔会把额度打光，
    # 表现为间歇 403（与 arXiv 406 同类坑）。interval_for 的 fallback 走的就是 provider。
}


def api_key_for(name: str, env: dict[str, str] | None = None) -> str | None:
    """取某个商用来源的密钥；没配返回 None（调用方据此决定"不启用并如实说明"）。"""
    env_name = COMMERCIAL_KEYS.get(name)
    if env_name is None:
        return None
    source = env if env is not None else os.environ
    value = (source.get(env_name) or "").strip()
    return value or None


def optional_token(name: str, env: dict[str, str] | None = None) -> str | None:
    """可选密钥（目前只有 github）：没有也能用，只是额度更低、间隔更长。"""
    env_name = OPTIONAL_TOKENS.get(name)
    if env_name is None:
        return None
    source = env if env is not None else os.environ
    value = (source.get(env_name) or "").strip()
    return value or None


@dataclass(frozen=True)
class SearchSettings:
    providers: tuple[str, ...] = DEFAULT_PROVIDERS
    default_limit: int = 10
    per_provider_limit: int = 8
    searxng_base_url: str = ""
    google_cse_cx: str = ""
    #: 是否允许抓搜索结果页（默认否：Bing robots 禁止 /search，见模块文档）
    allow_searchpage_scraping: bool = False
    min_interval_s: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_MIN_INTERVAL))

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> SearchSettings:
        section = raw or {}
        names = section.get("providers")
        providers = (
            tuple(str(name) for name in names)
            if isinstance(names, list) and names
            else DEFAULT_PROVIDERS
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
            searxng_base_url=str(section.get("searxng_base_url", "") or ""),
            google_cse_cx=str(section.get("google_cse_cx", "") or ""),
            allow_searchpage_scraping=bool(section.get("allow_searchpage_scraping", False)),
            min_interval_s=intervals,
        )

    def interval_for(self, provider: str, fallback: float) -> float:
        return float(self.min_interval_s.get(provider, fallback))

    def auto_names(
        self, *, key_lookup: Callable[[str], str | None] = api_key_for
    ) -> tuple[str, ...]:
        """auto 解析成实际要查的来源：只带"现在真能用"的那些。"""
        names: list[str] = []
        for name in AUTO_ORDER:
            if name == "searxng":
                if self.searxng_base_url.strip():
                    names.append(name)
                continue
            if name == "google_cse":
                if self.google_cse_cx.strip() and key_lookup(name):
                    names.append(name)
                continue
            if name in COMMERCIAL_KEYS:
                if key_lookup(name):
                    names.append(name)
                continue
            names.append(name)  # arxiv
        return tuple(names)

    def robots_exempt_hosts(self) -> tuple[str, ...]:
        """打开抓取开关后要豁免的主机（关着就是空 —— 默认不动 robots）。"""
        return SEARCHPAGE_HOSTS if self.allow_searchpage_scraping else ()
