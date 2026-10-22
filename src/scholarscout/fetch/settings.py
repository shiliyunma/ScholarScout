"""抓取参数（与 `config.example.yaml` 的 ``fetch:`` 段一一对应）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scholarscout.fetch.http import (
    # 分阶段超时的默认值**只在 `fetch/http.py` 顶部定义一次**，这里只转出给配置层用
    # （两处各写一份常数 = 改了一处忘了另一处；lint 会在重复定义处报 F811）
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_ROBOTS_TIMEOUT_S,
)

DEFAULT_USER_AGENT = "ScholarScout/0.1 (academic research assistant; contact: <you@example.com>)"

#: 与 fetch/http.py 的默认豁免一致（这里再导出一次，方便 config 层引用而不反向依赖 http）
DEFAULT_ROBOTS_EXEMPT_HOSTS: tuple[str, ...] = ("export.arxiv.org",)

#: MinerU 精准解析接口的 token 环境变量名（未配置时走免 token 的 Agent 轻量接口）
MINERU_TOKEN_ENV = "MINERU_API_TOKEN"

#: 抽取阶段（pymupdf 逐页）的墙钟预算
DEFAULT_EXTRACT_TIMEOUT_S = 60.0
#: MinerU 轮询阶段的单次预算（原为写死的 180s，两次调用最坏 360s）
DEFAULT_MINERU_POLL_TIMEOUT_S = 90.0


def mineru_token(env: dict[str, str] | None = None) -> str | None:
    """取 MinerU token；没配返回 None（调用方据此走免 token 的 Agent 接口）。"""
    import os

    source = env if env is not None else os.environ
    value = (source.get(MINERU_TOKEN_ENV) or "").strip()
    return value or None


@dataclass(frozen=True)
class FetchSettings:
    user_agent: str = DEFAULT_USER_AGENT
    timeout_s: float = 20.0
    #: 分阶段超时（fetch/http.py 顶部有全表）：建连 / robots 探针 / 整次抓取各给各的预算，
    #: 而不是所有阶段共用一个 timeout_s × retries。
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    deadline_s: float | None = None
    robots_timeout_s: float = DEFAULT_ROBOTS_TIMEOUT_S
    retries: int = 3
    max_bytes: int = 20_000_000
    cache_ttl_hours: float = 6.0
    respect_robots: bool = True
    #: 跳过 robots 检查的 API 主机（robots 是给爬虫看的，不是给 API 客户端看的）
    robots_exempt_hosts: tuple[str, ...] = DEFAULT_ROBOTS_EXEMPT_HOSTS
    #: **抽取阈值**（MB，可为小数，默认 20）：文件字节数超过它 → 不抽正文，只登记
    #: 元数据 + 保留源地址，标 `extract_status="skipped_large"`（docs/19 §8.3）。
    #: 与 `max_bytes`（出网层硬上限，超了直接拒收）分工不同：这个是"落盘后要不要抽正文"。
    large_file_mb: float = 20.0
    #: **抽取阶段**的墙钟预算（秒）：本地抽正文（目前只有 pymupdf 的 PDF 逐页循环）
    #: 超过它就停下并如实标 `extract_status="extract_timeout"` —— 宁可这一条没摘要，
    #: 也不让一轮对话卡在本地解析上。实测 arXiv 800KB / 54k 字符的 PDF 是 0.07–0.2s，
    #: 正常路径永远碰不到这个上限。
    extract_timeout_s: float = DEFAULT_EXTRACT_TIMEOUT_S
    #: **MinerU 轮询阶段**的单次预算（秒）。以前写死 180s，且"直传 + URL 提交"两条各来一次
    #: → 最坏 360s；配置里的 ``mineru.poll_timeout_s`` 此前**没有任何代码在读**（死配置）。
    mineru_poll_timeout_s: float = DEFAULT_MINERU_POLL_TIMEOUT_S

    @property
    def large_file_bytes(self) -> int:
        """抽取阈值的字节数（按十进制 MB，与 `max_bytes` 同口径）。"""
        return int(self.large_file_mb * 1_000_000)

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> FetchSettings:
        section = raw or {}
        hosts = section.get("robots_exempt_hosts")
        deadline = section.get("deadline_s")
        return cls(
            user_agent=str(section.get("user_agent", DEFAULT_USER_AGENT)),
            timeout_s=float(section.get("timeout_s", 20.0)),
            connect_timeout_s=float(section.get("connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S)),
            # 不写就是 None → 由出网层算成 timeout_s × 1.5（不在两处各写一份常数）
            deadline_s=None if deadline is None else float(deadline),
            robots_timeout_s=float(section.get("robots_timeout_s", DEFAULT_ROBOTS_TIMEOUT_S)),
            retries=int(section.get("retries", 3)),
            max_bytes=int(section.get("max_bytes", 20_000_000)),
            cache_ttl_hours=float(section.get("cache_ttl_hours", 6.0)),
            respect_robots=bool(section.get("respect_robots", True)),
            large_file_mb=float(section.get("large_file_mb", 20.0)),
            extract_timeout_s=float(section.get("extract_timeout_s", DEFAULT_EXTRACT_TIMEOUT_S)),
            mineru_poll_timeout_s=float(
                section.get("mineru_poll_timeout_s", DEFAULT_MINERU_POLL_TIMEOUT_S)
            ),
            robots_exempt_hosts=(
                tuple(str(host) for host in hosts)
                if isinstance(hosts, list)
                else DEFAULT_ROBOTS_EXEMPT_HOSTS
            ),
        )

    def cache_dir(self, home: Path) -> Path:
        return home / "data" / "cache"


#: MinerU 两条接口的官方限制（``config.example.yaml`` 的 ``mineru:`` 段，以前**没人读**）：
#: 轻量 Agent 接口 10MB / 20 页，精准接口 200MB / 200 页。写在配置里是为了让"哪天官方调了限制"
#: 只改一处；写死在代码里就只能等用户撞到服务端报错才知道。
DEFAULT_MINERU_AGENT_MAX_BYTES = 10_000_000
DEFAULT_MINERU_AGENT_MAX_PAGES = 20
DEFAULT_MINERU_PRECISE_MAX_BYTES = 200_000_000
DEFAULT_MINERU_PRECISE_MAX_PAGES = 200


def _positive_int(key: str, value: object, default: int) -> int:
    """读一个**正整数**配置。写错了直接报错 —— 静默回默认等于"用户以为改了、其实没改"。

    为什么连 ``0`` 也不收：``max_pages: 0`` 会让每一份 PDF 都"超限"，看着像功能坏了；
    真要关掉 MinerU 有 ``features.enable_mineru``，不该用限制值来表达。
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是正整数（当前 {value!r}，类型 {type(value).__name__}）")
    if value <= 0:
        raise ValueError(f"{key} 必须大于 0（当前 {value}）")
    return value


@dataclass(frozen=True)
class MineruSettings:
    """MinerU 的体积/页数上限（与 `config.example.yaml` 的 ``mineru:`` 段一一对应）。

    **四个键原来都是死的**：YAML 里写了，`load_config` 不读，`mineru.py` 里只在注释里
    提过 10MB/20 页、200MB/200 页 —— 于是超限只能等 MinerU 服务端报错，
    用户看到的是一句含糊的 ``failed``。现在读进来，并在提交前**提前**判掉。
    """

    agent_max_bytes: int = DEFAULT_MINERU_AGENT_MAX_BYTES
    agent_max_pages: int = DEFAULT_MINERU_AGENT_MAX_PAGES
    precise_max_bytes: int = DEFAULT_MINERU_PRECISE_MAX_BYTES
    precise_max_pages: int = DEFAULT_MINERU_PRECISE_MAX_PAGES

    @classmethod
    def from_raw(cls, section: object) -> MineruSettings:
        data = section if isinstance(section, dict) else {}
        return cls(
            agent_max_bytes=_positive_int(
                "mineru.agent_max_bytes",
                data.get("agent_max_bytes"),
                DEFAULT_MINERU_AGENT_MAX_BYTES,
            ),
            agent_max_pages=_positive_int(
                "mineru.agent_max_pages",
                data.get("agent_max_pages"),
                DEFAULT_MINERU_AGENT_MAX_PAGES,
            ),
            precise_max_bytes=_positive_int(
                "mineru.precise_max_bytes",
                data.get("precise_max_bytes"),
                DEFAULT_MINERU_PRECISE_MAX_BYTES,
            ),
            precise_max_pages=_positive_int(
                "mineru.precise_max_pages",
                data.get("precise_max_pages"),
                DEFAULT_MINERU_PRECISE_MAX_PAGES,
            ),
        )

    def max_bytes_for(self, engine: str) -> int:
        """这条接口的字节上限（``engine`` 只有 ``agent`` / ``precise`` 两种）。"""
        return self.precise_max_bytes if engine == "precise" else self.agent_max_bytes

    def max_pages_for(self, engine: str) -> int:
        return self.precise_max_pages if engine == "precise" else self.agent_max_pages
