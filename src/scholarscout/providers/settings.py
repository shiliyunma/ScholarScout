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

#: 免 key 来源（github 未认证也能用：Search API 10 次/分钟；openalex 免 key 可用）
KEYLESS = ("arxiv", "openalex", "github", "bing", "duckduckgo", "searxng")
#: **可选**密钥 → 环境变量：没有也能用，只是额度更低/间隔更长
OPTIONAL_TOKENS = {"github": "GITHUB_TOKEN"}
#: 商用来源 → 密钥环境变量名
COMMERCIAL_KEYS = {
    "tavily": "TAVILY_API_KEY",
    "serper": "SERPER_API_KEY",
    "brave": "BRAVE_API_KEY",
    "google_cse": "GOOGLE_CSE_KEY",
    # Semantic Scholar 无 key 恒 429（真机实测）→ 按"需要 key"处理，不进 auto
    "semantic_scholar": "S2_API_KEY",
}
#: 本版真正实现了的来源
IMPLEMENTED = ("arxiv", "openalex", "github", "bing", "duckduckgo", "searxng", *COMMERCIAL_KEYS)
#: 配置里允许写的来源名（= 已实现 + auto；不再有"配置写了但没实现"的占位）
KNOWN = (*IMPLEMENTED, "auto")

#: `providers: ["auto"]` 的哨兵值（见模块文档）
AUTO = "auto"
#: 默认值：auto（见模块文档）
DEFAULT_PROVIDERS = ("auto",)
#: auto 的装配顺序（学术 → 代码/教程 → 通用检索 → 商用；顺序只影响结果展示顺序）
AUTO_ORDER = (
    "arxiv",
    "openalex",
    "github",
    "searxng",
    "google_cse",
    "brave",
    "tavily",
    "serper",
    "semantic_scholar",
)

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
    "semantic_scholar": 1.5,
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
    #: OpenAlex 的"礼貌池"联系邮箱（官方按这个给更好的服务；不是密钥，进 config.yaml）
    openalex_mailto: str = ""
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
            openalex_mailto=str(section.get("openalex_mailto", "") or ""),
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


#: 界面上来源的展示顺序（学术 → 代码 → 通用/商用 → 抓页）
DISPLAY_ORDER = (
    "arxiv",
    "openalex",
    "semantic_scholar",
    "github",
    "searxng",
    "tavily",
    "serper",
    "brave",
    "google_cse",
    "bing",
    "duckduckgo",
)

#: 每个来源的分组名（界面直接显示，别让用户去读源码）
KIND_LABEL = {
    "arxiv": "学术",
    "openalex": "学术",
    "semantic_scholar": "学术",
    "github": "代码与教程",
    "searxng": "通用检索",
    "tavily": "商用检索",
    "serper": "商用检索",
    "brave": "商用检索",
    "google_cse": "商用检索",
    "bing": "抓搜索结果页",
    "duckduckgo": "抓搜索结果页",
}

#: 不可用时的说明（**必须写清怎么才能用**，否则用户只能猜）
NOTE = {
    "arxiv": "免 key；只索引英文、≥3s 限速",
    "openalex": "免 key；本机实测常返 429，配「联系邮箱」可走礼貌池",
    "semantic_scholar": "无 key 恒 429（实测）；填 key 后可用",
    "github": "免 key 也能用（10 次/分钟）；填 Token 提到 30 次/分钟",
    "searxng": "自建实例，免 key：`scripts/searxng_up.py` 一键起（Docker）",
    "tavily": "注册即得免费额度；填 key 后自动进入可用来源",
    "serper": "注册即得免费额度；填 key 后自动进入可用来源",
    "brave": "注册即得免费额度；填 key 后自动进入可用来源",
    "google_cse": "需要 Key **和** 搜索引擎 ID(CX) 两个都填才可用",
    "bing": "Bing robots 明确禁 /search；默认不抓，要显式打开「允许抓搜索结果页」",
    "duckduckgo": "国内需梯子；robots 同样不允抓结果页，且要显式打开「允许抓搜索结果页」",
}


@dataclass(frozen=True)
class ProviderStatus:
    """一个来源此刻的状态 —— 界面要回答"哪些能用、为什么不能用、怎么才能用"。"""

    name: str
    kind: str
    keyless: bool
    env_var: str | None
    key_configured: bool
    usable: bool
    reason: str  # ok | missing_api_key | missing_config | blocked_by_robots | off
    note: str
    optional_env_var: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "keyless": self.keyless,
            "env_var": self.env_var,
            "optional_env_var": self.optional_env_var,
            "key_configured": self.key_configured,
            "usable": self.usable,
            "reason": self.reason,
            "note": self.note,
        }


def provider_statuses(
    settings: SearchSettings, *, key_lookup: Callable[[str], str | None] = api_key_for
) -> tuple[ProviderStatus, ...]:
    """算出每个来源此刻能不能用、不能用为什么、怎么才能用。

    判定口径必须与 `auto_names` 一致 —— 不一致会出现"界面说可用、实际检索时不带上它"。
    """
    statuses: list[ProviderStatus] = []
    for name in DISPLAY_ORDER:
        env_var = COMMERCIAL_KEYS.get(name)
        optional_env_var = OPTIONAL_TOKENS.get(name)
        has_key = bool(key_lookup(name)) if env_var else False
        configured = has_key or (bool(optional_token(name)) if optional_env_var else False)
        if name == "searxng":
            usable = bool(settings.searxng_base_url.strip())
            reason = "ok" if usable else "missing_config"
        elif name == "google_cse":
            has_cx = bool(settings.google_cse_cx.strip())
            usable = has_key and has_cx
            reason = "ok" if usable else ("missing_config" if has_key else "missing_api_key")
        elif name in COMMERCIAL_KEYS:
            usable = has_key
            reason = "ok" if usable else "missing_api_key"
        elif name in ("bing", "duckduckgo"):
            usable = bool(settings.allow_searchpage_scraping)
            reason = "ok" if usable else "blocked_by_robots"
        else:  # arxiv / openalex / github：免 key
            usable = True
            reason = "ok"
        statuses.append(
            ProviderStatus(
                name=name,
                kind=KIND_LABEL.get(name, "其他"),
                keyless=env_var is None,
                env_var=env_var,
                optional_env_var=optional_env_var,
                key_configured=configured,
                usable=usable,
                reason=reason,
                note=NOTE.get(name, ""),
            )
        )
    return tuple(statuses)
