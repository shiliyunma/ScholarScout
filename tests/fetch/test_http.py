"""出网层的契约：Accept 头、限速、退避重试、缓存、体积上限、robots。

用 httpx.MockTransport，全离线；时钟与 sleep 都注入，测试不真的等 3 秒。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from scholarscout.fetch.http import DEFAULT_ACCEPT, FetchError, HttpClient


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    cache_dir: Path | None = None,
    respect_robots: bool = False,
    clock: FakeClock | None = None,
    slept: list[float] | None = None,
    **kwargs,
) -> HttpClient:
    fake = clock or FakeClock()
    delays = slept if slept is not None else []

    def sleep(seconds: float) -> None:
        delays.append(seconds)
        fake.advance(seconds)

    return HttpClient(
        respect_robots=respect_robots,
        cache_dir=cache_dir,
        transport=httpx.MockTransport(handler),
        clock=fake,
        sleep=sleep,
        **kwargs,
    )


def test_request_carries_user_agent_and_accept() -> None:
    """arXiv 必须收到显式 Accept，否则 406 —— 这条是防回归。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    client = make_client(handler)
    result = client.fetch("https://example.org/a")
    assert "user-agent" in seen and "scholarscout" in seen["user-agent"].lower()
    assert seen["accept"] == DEFAULT_ACCEPT
    assert result.text == "ok"


def test_result_text_uses_declared_charset() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content="中文".encode(),
            headers={"content-type": "text/plain; charset=utf-8"},
        )

    result = make_client(handler).fetch("https://example.org/utf8")
    assert result.text == "中文"
    assert result.charset == "utf-8"


def test_json_helper() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    assert make_client(handler).fetch("https://example.org/j").json() == {"ok": True}


def test_requests_are_rate_limited_per_host() -> None:
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x")

    client = make_client(handler, slept=slept)
    client.fetch("https://example.org/1", min_interval_s=3.0)
    client.fetch("https://example.org/2", min_interval_s=3.0)
    assert slept == [3.0], "同 host 第二次请求前必须等满最小间隔"


def test_cache_hit_avoids_second_request(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="cached body", headers={"content-type": "text/plain"})

    client = make_client(handler, cache_dir=tmp_path)
    first = client.fetch("https://example.org/x")
    second = client.fetch("https://example.org/x")
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.text == "cached body"
    assert len(calls) == 1


def test_cache_expires_after_ttl(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="body")

    clock = FakeClock()
    client = make_client(handler, cache_dir=tmp_path, clock=clock, cache_ttl_hours=6.0)
    client.fetch("https://example.org/x")
    clock.advance(6 * 3600 + 1)
    assert client.fetch("https://example.org/x").from_cache is False
    assert len(calls) == 2


def test_cache_disabled_when_no_dir() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="body")

    client = make_client(handler)
    client.fetch("https://example.org/x")
    client.fetch("https://example.org/x")
    assert len(calls) == 2


def test_retries_on_5xx_then_succeeds() -> None:
    attempts: list[int] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="down")
        return httpx.Response(200, text="finally")

    client = make_client(handler, slept=slept)
    assert client.fetch("https://example.org/retry").text == "finally"
    assert len(attempts) == 3
    assert slept == [1.0, 2.0], "指数退避：1s 然后 2s"


def test_server_error_after_all_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    with pytest.raises(FetchError) as info:
        make_client(handler).fetch("https://example.org/500")
    assert info.value.kind == "server"
    assert info.value.status == 500


def test_429_is_retried_with_longer_backoff() -> None:
    slept: list[float] = []
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text="ok")

    client = make_client(handler, slept=slept)
    client.fetch("https://example.org/429")
    assert slept == [2.0], "429 的退避要比普通错误长"


def test_not_found_is_not_retried() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404, text="nope")

    with pytest.raises(FetchError) as info:
        make_client(handler).fetch("https://example.org/missing")
    assert info.value.kind == "not_found"
    assert len(attempts) == 1


def test_forbidden_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="no")

    with pytest.raises(FetchError) as info:
        make_client(handler).fetch("https://example.org/blocked")
    assert info.value.kind == "forbidden"


def test_timeout_is_retried_then_classified() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.TimeoutException("too slow", request=request)

    with pytest.raises(FetchError) as info:
        make_client(handler).fetch("https://example.org/slow")
    assert info.value.kind == "timeout"
    assert len(attempts) == 3


def test_network_error_is_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(FetchError) as info:
        make_client(handler).fetch("https://example.org/x")
    assert info.value.kind == "network"


def test_body_over_limit_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100)

    with pytest.raises(FetchError) as info:
        make_client(handler, max_bytes=10).fetch("https://example.org/big")
    assert info.value.kind == "too_large"


def test_robots_disallow_blocks_request() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        return httpx.Response(200, text="secret")

    client = make_client(handler, respect_robots=True)
    with pytest.raises(FetchError) as info:
        client.fetch("https://example.org/private/page")
    assert info.value.kind == "forbidden"
    assert all("/private/page" not in url for url in seen), "被 robots 拦下时不应真的请求目标页"


def test_robots_allow_lets_request_through() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="fine")

    client = make_client(handler, respect_robots=True)
    assert client.fetch("https://example.org/fine").text == "fine"


def test_robots_unavailable_allows_by_default() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("no robots", request=request)
        return httpx.Response(200, text="fine")

    client = make_client(handler, respect_robots=True)
    assert client.fetch("https://example.org/fine").text == "fine"


def test_robots_cached_per_host() -> None:
    robots_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            robots_calls.append(str(request.url))
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="fine")

    client = make_client(handler, respect_robots=True)
    client.fetch("https://example.org/a")
    client.fetch("https://example.org/b")
    assert len(robots_calls) == 1


def test_cache_written_metadata_is_readable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="body", headers={"content-type": "text/plain"})

    make_client(handler, cache_dir=tmp_path).fetch("https://example.org/meta")
    metas = list(tmp_path.glob("*.json"))
    assert len(metas) == 1
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["url"] == "https://example.org/meta"
    assert meta["status"] == 200
