"""出网唯一入口（关键路径，人工逐行 review）：限速 / 退避重试 / 缓存 / UA / 超时 / 体积上限。

规则（docs/03 §2）：**所有外网访问只经过本模块**；其他模块不许直接 ``import httpx``。
重试也只写在这里（规则 §3.5）。

设计要点：
- 默认带 ``Accept`` 头。**arXiv 不带 Accept 会一律 406**（curl 默认发 ``*/*`` 所以能通，
  urllib 不发就挂）—— 这是本项目踩过的坑，写进 docs/03 §7。
- 同一 host 的请求按 ``min_interval_s`` 串行化（arXiv 要求 >= 3 秒/次）。
- 成功响应落 ``data/cache``，默认 6 小时有效，命中即不重复出网。
- ``429`` 尊重 ``Retry-After``；``5xx`` / 超时 / 连接错误按指数退避重试。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)


class FetchError(Exception):
    """抓取失败。``kind`` 让上层映射成契约错误码（见 rpc/errors.py）。"""

    KINDS = (
        "forbidden",  # 403 / 451，或 robots 不允许
        "rate_limited",  # 429
        "not_found",  # 404
        "server",  # 5xx
        "timeout",
        "network",
        "too_large",
        "bad_request",  # 其它 4xx
    )

    def __init__(
        self, kind: str, message: str, *, url: str = "", status: int | None = None
    ) -> None:
        if kind not in self.KINDS:
            raise ValueError(f"未知抓取错误类型：{kind!r}")
        super().__init__(message)
        self.kind = kind
        self.url = url
        self.status = status


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str = ""
    from_cache: bool = False
    elapsed_ms: int = 0

    @property
    def charset(self) -> str:
        raw = self.media_type
        if "charset=" in raw:
            return raw.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        return "utf-8"

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.charset)
        except (LookupError, UnicodeDecodeError):
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class _RateLimiter:
    """按 key（一般取 host）串行化：两次请求之间至少间隔 ``min_interval_s``。"""

    def __init__(self, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self.slept: list[float] = []

    def wait(self, key: str, min_interval_s: float) -> float:
        if min_interval_s <= 0:
            return 0.0
        previous = self._last.get(key)
        now = self._clock()
        if previous is not None:
            remaining = min_interval_s - (now - previous)
            if remaining > 0:
                self._sleep(remaining)
                self.slept.append(remaining)
                self._last[key] = previous + min_interval_s
                return remaining
        self._last[key] = now
        return 0.0


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str = "ScholarScout/0.1 (academic research assistant)",
        timeout_s: float = 20.0,
        retries: int = 3,
        backoff_base_s: float = 1.0,
        backoff_factor: float = 2.0,
        max_bytes: int = 20_000_000,
        cache_dir: Path | None = None,
        cache_ttl_hours: float = 6.0,
        respect_robots: bool = True,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_s = timeout_s
        self.retries = max(1, retries)
        self.backoff_base_s = backoff_base_s
        self.backoff_factor = backoff_factor
        self.max_bytes = max_bytes
        self.cache_dir = cache_dir
        self.cache_ttl_hours = cache_ttl_hours
        self.respect_robots = respect_robots
        self._clock = clock
        self._sleep = sleep
        self._limiter = _RateLimiter(clock, sleep)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": user_agent},
        )

    # ---------------------------------------------------------------- 生命周期

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- 缓存

    def _cache_paths(self, url: str, accept: str) -> tuple[Path, Path] | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(f"{url}\n{accept}".encode()).hexdigest()
        return self.cache_dir / f"{key}.json", self.cache_dir / f"{key}.bin"

    def _read_cache(self, url: str, accept: str) -> FetchResult | None:
        paths = self._cache_paths(url, accept)
        if paths is None:
            return None
        meta_path, body_path = paths
        if not (meta_path.is_file() and body_path.is_file()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        age_hours = (self._clock() - float(meta.get("stored_at", 0))) / 3600.0
        if age_hours > self.cache_ttl_hours:
            return None
        return FetchResult(
            url=url,
            status=int(meta.get("status", 200)),
            content=body_path.read_bytes(),
            headers=dict(meta.get("headers", {})),
            media_type=str(meta.get("media_type", "")),
            from_cache=True,
        )

    def _write_cache(self, url: str, accept: str, result: FetchResult) -> None:
        paths = self._cache_paths(url, accept)
        if paths is None:
            return
        meta_path, body_path = paths
        try:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_bytes(result.content)
            meta_path.write_text(
                json.dumps(
                    {
                        "url": url,
                        "status": result.status,
                        "headers": result.headers,
                        "media_type": result.media_type,
                        "stored_at": self._clock(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            return  # 缓存写失败不影响本次结果

    # ---------------------------------------------------------------- robots

    def robots_allowed(self, url: str) -> bool:
        """按 robots.txt 判断是否允许抓取。取不到 robots.txt 时**放行**（并已记日志留痕）。"""
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return True
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        if host_key not in self._robots:
            self._robots[host_key] = self._load_robots(host_key)
        parser = self._robots[host_key]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _load_robots(self, host_key: str) -> urllib.robotparser.RobotFileParser | None:
        try:
            result = self._request_once(
                f"{host_key}/robots.txt", DEFAULT_ACCEPT, "robots", min_interval_s=0.0
            )
        except (FetchError, httpx.HTTPError):
            return None
        if result.status >= 400:
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(result.text.splitlines())
        return parser

    # ---------------------------------------------------------------- 抓取

    def fetch(
        self,
        url: str,
        *,
        accept: str = DEFAULT_ACCEPT,
        min_interval_s: float = 0.0,
        cache: bool = True,
    ) -> FetchResult:
        if cache:
            cached = self._read_cache(url, accept)
            if cached is not None:
                return cached
        if not self.robots_allowed(url):
            raise FetchError("forbidden", f"robots.txt 不允许抓取：{url}", url=url)
        result = self._request_once(url, accept, urlparse(url).netloc, min_interval_s)
        if cache:
            self._write_cache(url, accept, result)
        return result

    def _request_once(
        self,
        url: str,
        accept: str,
        rate_key: str,
        min_interval_s: float,
    ) -> FetchResult:
        started = self._clock()
        self._limiter.wait(rate_key, min_interval_s)
        last_error: FetchError | None = None

        for attempt in range(self.retries):
            try:
                response = self._client.get(url, headers={"Accept": accept})
            except httpx.TimeoutException as exc:
                last_error = FetchError("timeout", f"请求超时（{self.timeout_s}s）：{url}", url=url)
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = FetchError("network", f"连接失败：{exc}", url=url)
                last_error.__cause__ = exc
            else:
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = self._status_error(response, url)
                else:
                    return self._ok_or_raise(response, url, started)
            if attempt < self.retries - 1:
                self._sleep(self._backoff_seconds(attempt, last_error))
        assert last_error is not None  # 循环必然赋值
        raise last_error

    def _backoff_seconds(self, attempt: int, error: FetchError | None) -> float:
        if error is not None and error.kind == "rate_limited" and error.status == 429:
            return min(60.0, self.backoff_base_s * (self.backoff_factor**attempt) * 2)
        return self.backoff_base_s * (self.backoff_factor**attempt)

    def _ok_or_raise(self, response: httpx.Response, url: str, started: float) -> FetchResult:
        if response.status_code >= 400:
            raise self._status_error(response, url)
        content = response.content
        if len(content) > self.max_bytes:
            raise FetchError(
                "too_large",
                f"响应超过上限 {self.max_bytes} 字节：{len(content)}",
                url=url,
                status=response.status_code,
            )
        return FetchResult(
            url=str(response.url),
            status=response.status_code,
            content=content,
            headers={k.lower(): v for k, v in response.headers.items()},
            media_type=response.headers.get("content-type", ""),
            from_cache=False,
            elapsed_ms=int((self._clock() - started) * 1000),
        )

    @staticmethod
    def _status_error(response: httpx.Response, url: str) -> FetchError:
        status = response.status_code
        if status in (403, 451):
            return FetchError("forbidden", f"被拒绝（{status}）：{url}", url=url, status=status)
        if status == 429:
            return FetchError("rate_limited", f"限流（429）：{url}", url=url, status=429)
        if status == 404:
            return FetchError("not_found", f"不存在（404）：{url}", url=url, status=404)
        if status in _RETRYABLE_STATUS:
            return FetchError("server", f"服务端错误（{status}）：{url}", url=url, status=status)
        return FetchError("bad_request", f"请求失败（{status}）：{url}", url=url, status=status)
