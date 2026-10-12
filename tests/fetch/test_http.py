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


def test_robots_exempt_hosts_bypass_check_for_official_apis() -> None:
    """robots.txt 是给爬虫看的，不是给 API 客户端看的。

    arXiv 的 robots 明确禁掉 ``/api/query`` —— 照字面执行会让**官方检索 API** 整体不可用
    （真机跑 E3 时检索恒返回 0 条，根因就在这里）。
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /api/")
        return httpx.Response(200, text="<feed/>")

    client = make_client(handler, respect_robots=True)
    result = client.fetch("https://export.arxiv.org/api/query?search_query=all:test")
    assert result.text == "<feed/>"
    assert not any(url.endswith("/robots.txt") for url in seen), "豁免主机不该去读 robots.txt"


def test_robots_exemption_does_not_leak_to_other_hosts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /api/")
        return httpx.Response(200, text="secret")

    client = make_client(handler, respect_robots=True)
    with pytest.raises(FetchError):
        client.fetch("https://evil.example.org/api/query")


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


# ---------------------------------------------------------------------------
# 分阶段超时（2026-10-12 补）：真机 trace 里单次 source.fetch 花过 486s，
# 起因是所有阶段共用一个 timeout_s × retries（20s × 3）+ 回落 URL 又各来一遍。
# 下面四条钉住"每段各给各的预算"这件事 —— 只删代码不删测试，否则 486s 会回来。
# ---------------------------------------------------------------------------


def test_staged_timeouts_are_wired_per_stage() -> None:
    """建连 / 读取 / 整次 / robots 探针各是一个数，不是一个 timeout_s 打天下。"""
    client = make_client(
        lambda request: httpx.Response(200, text="x"),
        timeout_s=20.0,
        connect_timeout_s=1.5,
    )
    assert client._timeout.connect == 1.5, "建连阶段必须有独立（更短）的预算"
    assert client._timeout.read == 20.0, "读取阶段沿用原来的 timeout_s 语义"
    assert client.deadline_s == 30.0, "不显式给 deadline 时是 timeout_s × 1.5"
    assert client.robots_timeout_s == 5.0
    # 显式给 deadline 时以显式值为准
    assert (
        make_client(lambda request: httpx.Response(200, text="x"), deadline_s=7.5).deadline_s == 7.5
    )


def test_full_timeout_is_not_multiplied_by_retries() -> None:
    """一次"满超时"= 主机不可达（不是瞬时抖动）→ 装不下预算就不该再试。

    旧行为：3 次重试各等满 timeout_s（20s×3=60s）。真机就是这么叠出 486s 的。
    """
    clock = FakeClock()
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        clock.advance(20.0)  # 这一次尝试真的等满了 timeout_s
        raise httpx.TimeoutException("too slow", request=request)

    client = make_client(handler, clock=clock, timeout_s=20.0, deadline_s=30.0)
    with pytest.raises(FetchError) as info:
        client.fetch("https://example.org/slow")

    assert info.value.kind == "timeout"
    assert len(attempts) == 1, "整次预算 30s 装不下第二次 20s 超时 → 不该再试"


def test_backoff_sleep_cannot_push_the_fetch_past_its_deadline() -> None:
    """退避也要夹进剩余预算：不许 sleep 把整次抓取拖过 deadline。"""
    clock = FakeClock()
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    client = make_client(
        handler,
        clock=clock,
        slept=slept,
        timeout_s=20.0,
        deadline_s=30.0,
        backoff_base_s=100.0,  # 退避本身远超预算
    )
    with pytest.raises(FetchError):
        client.fetch("https://example.org/busy")

    assert slept and sum(slept) <= 30.0, f"累计退避不许超过 deadline，实为 {slept}"


def test_robots_probe_is_one_short_side_query() -> None:
    """robots.txt 是**侧查询**：只试一次 + 用自己的短预算（旧行为 20s×3=63s）。"""
    clock = FakeClock()
    probed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("robots.txt"):
            probed.append(url)
            clock.advance(5.0)  # 探针耗尽自己的 5s 预算
            raise httpx.TimeoutException("robots slow", request=request)
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    client = make_client(handler, clock=clock, respect_robots=True, timeout_s=20.0)
    result = client.fetch("https://example.org/page")

    assert len(probed) == 1, "robots 拿不到就算了（默认放行），不该重试 3 次"
    assert result.status == 200, "探针失败不能连带把正文抓取也判死"
