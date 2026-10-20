"""出网唯一入口（关键路径，人工逐行 review）：限速 / 退避重试 / 缓存 / UA / 超时 / 体积上限。

规则（docs/03 §2）：**所有外网访问只经过本模块**；其他模块不许直接 ``import httpx``。
重试也只写在这里（规则 §3.5）。

设计要点：
- 默认带 ``Accept`` 头。**arXiv 不带 Accept 会一律 406**（curl 默认发 ``*/*`` 所以能通，
  urllib 不发就挂）—— 这是本项目踩过的坑，写进 docs/03 §7。
- 同一 host 的请求按 ``min_interval_s`` 串行化（arXiv 要求 >= 3 秒/次）。
- 成功响应落 ``data/cache``，默认 6 小时有效，命中即不重复出网。
- ``429`` 尊重 ``Retry-After``；``5xx`` / 超时 / 连接错误按指数退避重试。

**分阶段超时（2026-10-12 补，起因是真机 trace 里单次 ``source.fetch`` 花了 486s）**：

| 阶段 | 参数 | 默认 | 为什么单独给 |
|---|---|---|---|
| ① 建连（DNS+TCP+TLS） | ``connect_timeout_s`` | 5s | 实测 DNS 17ms + TLS 240ms，5s 足够；连不上不该拖 20s |
| ② 首字节 / 读取 | ``timeout_s`` | 20s | 原语义不变（大 PDF 下载慢是正常的） |
| ③ 整次抓取（含重试与退避） | ``deadline_s`` | ``timeout_s × 1.5`` | **重试不叠乘超时**：一次"满超时"说明主机不可达、不是瞬时抖动，重试只留给快失败（429/5xx/连接被拒） |
| ④ robots.txt 探针 | ``robots_timeout_s`` | 5s | 它是**侧查询**：不该比正文抓取还贵，且只试一次 |

为什么必须分阶段：以前所有阶段共用一个 20s 超时 + 3 次重试，最坏一次抓取 =
``robots(3×20+3=63s) + 真文件(63s) + 回落原 URL(63s) ≈ 189s``；而回落目标若是 ``http://``
（本机不可达）再加一整段。真机库（``run/state.db``）里就是 **486.3s / 243.2s** 的单次调用，
三条 486s 全是 ``http://arxiv.org/abs/...``，那一轮 6 次 ``source.fetch`` 合计 836s ——
远超桌面端 180s 的响应超时，界面直接判定"调用失败"，整条链路在用户眼里停摆。

契约类型（``FetchError`` / ``FetchResult``）在 ``fetch/contract.py``；本模块重导出，旧 import 路径不变。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.robotparser
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

# 契约类型已拆到 `fetch/contract.py`：此处 import 即重导出（`from ...http import FetchError` 照旧）。
from scholarscout.fetch.contract import FetchError, FetchResult

__all__ = [
    "DEFAULT_ACCEPT",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_ROBOTS_EXEMPT_HOSTS",
    "DEFAULT_ROBOTS_TIMEOUT_S",
    "FetchError",
    "FetchResult",
    "HttpClient",
]

DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

#: 建连阶段的默认预算（DNS + TCP + TLS）。实测 17ms + 240ms，5s 已很宽松。
DEFAULT_CONNECT_TIMEOUT_S = 5.0
#: robots.txt 探针的默认预算：它是侧查询，不该比正文抓取还贵。
DEFAULT_ROBOTS_TIMEOUT_S = 5.0

#: 这些主机的 robots.txt 不约束我们：它们是**官方 API**，不是待爬的网页
#: （arXiv 的 robots 禁止 /api/query，照字面执行会让检索整体不可用）
DEFAULT_ROBOTS_EXEMPT_HOSTS: tuple[str, ...] = ("export.arxiv.org",)
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)
#: 错误回包正文最多留多少字符（只要够解析 `code`/`msg`，不要把整页 HTML 塞进日志）
_ERROR_BODY_CHARS = 500


def _body_snippet(response: httpx.Response) -> str:
    """取回包正文的截断片段（读不到就回空串 —— 取证失败不能把原错误盖掉）。"""
    try:
        raw = response.content
    except Exception:  # 已读流 / 解码失败都只当作「没有正文」
        return ""
    return raw.decode("utf-8", errors="replace")[:_ERROR_BODY_CHARS]


class _RateLimiter:
    """按 key（一般取 host）串行化：两次请求之间至少间隔 ``min_interval_s``。

    **并发安全**（docs/20 §4.2 / §6.1）：同一 key 的请求在同一把锁上**排队到** sleep 结束 ——
    否则多会话并行抓同一个 host 时会各自读到"上次请求时间"、谁都不等，把限速整体绕过
    （原来就是裸 dict，注释声称"并行不绕过限速"但代码做不到）。
    锁是**按 key** 的：抓 arxiv 与抓 github 互不挡（跨 host 的并发抓取本来就该并行）。
    """

    def __init__(self, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}
        #: 每个 key 一把锁（跨 host 不互相排队）+ 一把守护锁只保护这张表本身
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.slept: list[float] = []

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def wait(self, key: str, min_interval_s: float) -> float:
        if min_interval_s <= 0:
            return 0.0
        with self._lock_for(key):
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
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        deadline_s: float | None = None,
        robots_timeout_s: float = DEFAULT_ROBOTS_TIMEOUT_S,
        retries: int = 3,
        backoff_base_s: float = 1.0,
        backoff_factor: float = 2.0,
        max_bytes: int = 20_000_000,
        cache_dir: Path | None = None,
        cache_ttl_hours: float = 6.0,
        respect_robots: bool = True,
        robots_exempt_hosts: Iterable[str] = DEFAULT_ROBOTS_EXEMPT_HOSTS,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        #: 阶段②：首字节 / 读取
        self.timeout_s = timeout_s
        #: 阶段①：建连（DNS + TCP + TLS）
        self.connect_timeout_s = connect_timeout_s
        #: 阶段③：**整次抓取**（含全部重试与退避）的墙钟上限。None → ``timeout_s × 1.5``。
        self.deadline_s = timeout_s * 1.5 if deadline_s is None else deadline_s
        #: 阶段④：robots.txt 探针自己的预算
        self.robots_timeout_s = robots_timeout_s
        self.retries = max(1, retries)
        self.backoff_base_s = backoff_base_s
        self.backoff_factor = backoff_factor
        self.max_bytes = max_bytes
        self.cache_dir = cache_dir
        self.cache_ttl_hours = cache_ttl_hours
        self.respect_robots = respect_robots
        #: 跳过 robots 检查的 API 主机（robots 是给爬虫看的，不是给 API 客户端看的）
        self.robots_exempt_hosts = {host.lower() for host in robots_exempt_hosts}
        self._clock = clock
        self._sleep = sleep
        self._limiter = _RateLimiter(clock, sleep)
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        # 分阶段超时交给 httpx：connect 单给，read/write/pool 用 timeout_s（原语义）
        self._timeout = httpx.Timeout(
            connect=connect_timeout_s, read=timeout_s, write=timeout_s, pool=timeout_s
        )
        self._client = httpx.Client(
            timeout=self._timeout,
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
        # 只缓存"成功且有内容"的响应：
        # 把 4xx/5xx 或空响应缓存下来，等于把一次瞬时故障钉死成 6 小时的"事实"。
        # 真机上就吃过这个：SearxNG 容器刚起还没加载完引擎 → 200 + results: [] 被缓存
        # → 之后每次检索都读缓存，看着像"这个来源永远搜不到东西"。
        if result.status != 200 or not result.content:
            return
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
        """按 robots.txt 判断是否允许抓取。取不到 robots.txt 时**放行**（并已记日志留痕）。

        **豁免**（``robots_exempt_hosts``，默认含 ``export.arxiv.org``）：
        robots.txt 是给爬虫看的，不是给 API 客户端看的 —— arXiv 的 robots 明确禁掉
        ``/api/query``，照字面执行会让**官方检索 API** 整体不可用（2026-10-05 真机跑 E3 才暴露：
        检索恒返回 0 条，模型反复重试直到退化）。所以对白名单里的 API 主机跳过 robots 检查。
        """
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return True
        if parsed.netloc.lower() in self.robots_exempt_hosts:
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
            # robots.txt 是**侧查询**：只试一次，且用它自己的短预算 ——
            # 以前它跟正文一样是"20s × 3 次重试"，主机不可达时光一个 robots 探针就烧 63s，
            # 而正文抓取还没开始（真机 486s 的调用里就包含这一段）。
            result = self._request_once(
                f"{host_key}/robots.txt",
                DEFAULT_ACCEPT,
                "robots",
                min_interval_s=0.0,
                attempts=1,
                request_timeout=httpx.Timeout(self.robots_timeout_s),
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
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        if cache:
            cached = self._read_cache(url, accept)
            if cached is not None:
                return cached
        if not self.robots_allowed(url):
            raise FetchError("forbidden", f"robots.txt 不允许抓取：{url}", url=url)
        result = self._request_once(
            url,
            accept,
            urlparse(url).netloc,
            min_interval_s,
            headers=headers,
        )
        if cache:
            self._write_cache(url, accept, result)
        return result

    def upload_bytes(
        self,
        url: str,
        body: bytes,
        *,
        min_interval_s: float = 0.0,
    ) -> FetchResult:
        """PUT 原始字节到**预签名上传地址**（MinerU 的"签名上传"就是这么走的）。

        三条都是实测得来的：
        - **一个头都别多带**（尤其 `Content-Type`）：预签名里把它算进了签名，
          带上就 `403 SignatureDoesNotMatch`（实测：不带 → 200，带 application/pdf 或
          application/octet-stream → 403）。所以这里刻意不设 Content-Type；
        - **不走缓存**：预签名 URL 是一次性的，缓存它只会让第二次上传打到失效地址；
        - **不过 robots**：这是对象存储的上传端点，不是抓取目标（官方示例也只做 PUT）。
        """
        return self._request_once(
            url,
            "*/*",
            urlparse(url).netloc,
            min_interval_s,
            method="PUT",
            body=body,
            omit_content_type=True,
        )

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        accept: str = "application/json",
        min_interval_s: float = 0.0,
        cache: bool = True,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """POST 一个 JSON 请求体（商用检索 API 都走 POST）。

        - 出网仍然**只有这一层**：robots、限速、退避、体积上限、缓存都在这里生效
        - 缓存键 = URL + 请求体哈希（POST 不能只用 URL 做键，否则两次不同查询会串台）
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        cache_key = f"{url}#post:{hashlib.sha256(body).hexdigest()[:16]}"
        if cache:
            cached = self._read_cache(cache_key, accept)
            if cached is not None:
                return cached
        if not self.robots_allowed(url):
            raise FetchError("forbidden", f"robots.txt 不允许抓取：{url}", url=url)
        result = self._request_once(
            url,
            accept,
            urlparse(url).netloc,
            min_interval_s,
            method="POST",
            body=body,
            headers=headers,
        )
        if cache:
            self._write_cache(cache_key, accept, result)
        return result

    def post_form(
        self,
        url: str,
        form: dict[str, Any],
        *,
        accept: str = "application/json",
        min_interval_s: float = 0.0,
        cache: bool = False,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """POST 一个 ``application/x-www-form-urlencoded`` 请求体（飞书注册接口要的就是它）。

        - 出网仍然**只有这一层**：robots、限速、退避、体积上限都在这里生效；
        - **默认不缓存**：设备码注册/轮询是状态机（同一个 poll 体每次结果都不同），
          缓存会把"等待用户授权"钉死成第一次的 `authorization_pending`（与 `upload_bytes`
          预签名上传同理：一次性的地址不该缓存）。
        """
        body = urlencode([(key, str(value)) for key, value in form.items()]).encode("utf-8")
        if not self.robots_allowed(url):
            raise FetchError("forbidden", f"robots.txt 不允许抓取：{url}", url=url)
        result = self._request_once(
            url,
            accept,
            urlparse(url).netloc,
            min_interval_s,
            method="POST",
            body=body,
            headers=headers,
            content_type="application/x-www-form-urlencoded",
        )
        if cache:
            cache_key = f"{url}#form:{hashlib.sha256(body).hexdigest()[:16]}"
            self._write_cache(cache_key, accept, result)
        return result

    def post_multipart(
        self,
        url: str,
        *,
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        accept: str = "application/json",
        min_interval_s: float = 0.0,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """POST 一个 ``multipart/form-data`` 请求体（飞书上传文件要的就是它）。

        - 出网仍然**只有这一层**：robots、限速、退避、体积上限都在这里生效；
        - **Content-Type 由 httpx 自己写**（它要带 boundary）。手写一个
          ``multipart/form-data`` 会缺 boundary，服务端直接 400/解析失败 ——
          所以这里刻意 `omit_content_type`，与 `upload_bytes` 同一个道理（少带一个头才不会错）；
        - 调用方给的是**已读好的字节**（`files={"file": (文件名, 内容, mime)}`），
          重试会重新编码同一份字节，不会漏读文件句柄。
        """
        if not self.robots_allowed(url):
            raise FetchError("forbidden", f"robots.txt 不允许抓取：{url}", url=url)
        return self._request_once(
            url,
            accept,
            urlparse(url).netloc,
            min_interval_s,
            method="POST",
            headers=headers,
            multipart=(fields, files),
        )

    def _request_once(
        self,
        url: str,
        accept: str,
        rate_key: str,
        min_interval_s: float,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
        omit_content_type: bool = False,
        attempts: int | None = None,
        request_timeout: httpx.Timeout | None = None,
        multipart: tuple[dict[str, str], dict[str, tuple[str, bytes, str]]] | None = None,
    ) -> FetchResult:
        self._limiter.wait(rate_key, min_interval_s)
        started = self._clock()
        last_error: FetchError | None = None
        request_headers = {"Accept": accept, **(headers or {})}
        total_attempts = max(1, self.retries if attempts is None else attempts)

        for attempt in range(total_attempts):
            if attempt:
                # ---- 阶段③：整次抓取的墙钟上限（分阶段超时的"总预算"那一层）。
                # **重试不叠乘超时**：下一次尝试必须能装进剩余预算，装不下就停手。
                # 一次"满超时"意味着主机不可达（不是瞬时抖动），再试一次只是白等；
                # 而 429/5xx/连接被拒这类**快失败**仍然照常重试（预算几乎没被消耗）。
                elapsed = self._clock() - started
                if elapsed + self.timeout_s > self.deadline_s:
                    break
            try:
                if multipart is not None:
                    # multipart：Content-Type 必须由 httpx 自己生成（要带 boundary）
                    form_fields, form_files = multipart
                    response = self._client.post(
                        url,
                        headers=request_headers,
                        data=form_fields,
                        files=form_files,
                        **self._timeout_kwargs(request_timeout),
                    )
                elif method in ("POST", "PUT"):
                    send_headers = dict(request_headers)
                    if not omit_content_type:
                        send_headers["Content-Type"] = content_type or "application/json"
                    send = self._client.post if method == "POST" else self._client.put
                    response = send(
                        url,
                        headers=send_headers,
                        content=body,
                        **self._timeout_kwargs(request_timeout),
                    )
                else:
                    response = self._client.get(
                        url, headers=request_headers, **self._timeout_kwargs(request_timeout)
                    )
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
            if attempt < total_attempts - 1:
                # 退避同样被夹到剩余预算内：不让 sleep 把整次抓取拖过 deadline
                delay = self._backoff_seconds(attempt, last_error)
                remaining = max(0.0, self.deadline_s - (self._clock() - started))
                self._sleep(min(delay, remaining))
        assert last_error is not None  # 循环必然赋值
        raise last_error

    @staticmethod
    def _timeout_kwargs(request_timeout: httpx.Timeout | None) -> dict[str, Any]:
        """按请求覆盖超时（robots 探针用短预算）；不覆盖时不传，免得把 ``timeout=None`` 传成"永不超时"。"""
        return {} if request_timeout is None else {"timeout": request_timeout}

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
        # 回包正文留一份（截断）：平台把 code/msg 全放这里，丢了它上层只能报「400」
        body = _body_snippet(response)
        if status in (403, 451):
            return FetchError(
                "forbidden", f"被拒绝（{status}）：{url}", url=url, status=status, body=body
            )
        if status == 429:
            return FetchError("rate_limited", f"限流（429）：{url}", url=url, status=429, body=body)
        if status == 404:
            return FetchError("not_found", f"不存在（404）：{url}", url=url, status=404, body=body)
        if status in _RETRYABLE_STATUS:
            return FetchError(
                "server", f"服务端错误（{status}）：{url}", url=url, status=status, body=body
            )
        return FetchError(
            "bad_request", f"请求失败（{status}）：{url}", url=url, status=status, body=body
        )
