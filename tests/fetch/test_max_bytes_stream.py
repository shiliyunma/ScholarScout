"""``max_bytes`` 的契约：**边读边数、一超就断**，而不是"整包下完再说太大"。

三条各自钉一件事：

1. ``Content-Length`` 已经写明超限 → **一个字节都不读**就拒（头里的话先信，别搬进内存再骂）；
2. chunked（没有 ``Content-Length``）→ 边读边累加，一超就关流；已读字节数**不超过上限 + 一块**
   （超了再读下一块，就是白花的流量与内存）；
3. ``too_large`` 是**永久**错误 → **不重试**（同一个地址再试一次只会再超一次）。

全程 `httpx.MockTransport` + 自定义 `SyncByteStream`，不真出网。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from scholarscout.fetch.contract import FetchError
from scholarscout.fetch.http import HttpClient

#: 一块的大小：由我们自己的桩决定，这样"多读了几块"能被精确数出来。
CHUNK = 1024

#: 用来撑爆上限的正文：不压缩、不重复得太有规律（真要解压也躲不过这层字节计数）。
_BIG = b"x" * (CHUNK * 200)


class _CountingStream(httpx.SyncByteStream):
    """记下**实际被消费的字节数** —— "超限即断"的断言全靠它。"""

    def __init__(self, payload: bytes, *, chunk: int = CHUNK) -> None:
        self.payload = payload
        self.chunk = chunk
        self.consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self.payload), self.chunk):
            piece = self.payload[start : start + self.chunk]
            self.consumed += len(piece)
            yield piece


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: object) -> HttpClient:
    return HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        clock=lambda: 1000.0,
        sleep=lambda seconds: None,
        **kwargs,
    )


def _response(stream: _CountingStream, **headers: str) -> httpx.Response:
    """流式回包：`stream=` 不会自动补 `Content-Length`（和真实 chunked 一致）。"""
    return httpx.Response(200, headers=dict(headers), stream=stream)


def test_content_length_over_limit_is_rejected_before_any_byte_is_read() -> None:
    """头里写明 ``Content-Length: 200KB``、上限 1KB → 不读 body 就拒，错误仍是 ``too_large``。"""
    stream = _CountingStream(_BIG)
    response = httpx.Response(
        200,
        headers={"content-length": str(len(_BIG)), "content-type": "text/plain"},
        stream=stream,
    )

    with pytest.raises(FetchError) as info:
        _client(lambda request: response, max_bytes=CHUNK).fetch("https://example.org/huge.html")

    assert info.value.kind == "too_large"
    assert f"响应超过上限 {CHUNK} 字节：{len(_BIG)}" in str(info.value), (
        "超限文案要沿用原口径（上限多少、实际多少）"
    )
    assert stream.consumed == 0, f"头里已经写了超限，body 却被读了 {stream.consumed} 字节"


def test_chunked_body_stops_within_one_chunk_of_the_limit() -> None:
    """没有 ``Content-Length``（chunked）→ 边读边累加，一超就停：多读的不得超过一块。"""
    stream = _CountingStream(_BIG)
    response = _response(stream, **{"content-type": "application/octet-stream"})

    with pytest.raises(FetchError) as info:
        # `peek_media=True` 是真路径（`source.fetch` 就这么调）：探针窗口也要算在上限里。
        _client(lambda request: response, max_bytes=CHUNK * 4).fetch(
            "https://example.org/stream", peek_media=True
        )

    assert info.value.kind == "too_large"
    assert stream.consumed > CHUNK * 4, "上限都没碰到就断了，说明根本没在数"
    assert stream.consumed <= CHUNK * 4 + CHUNK, (
        f"超限后还在读：已读 {stream.consumed} 字节，上限 {CHUNK * 4}、块 {CHUNK}"
    )
    assert stream.consumed < len(_BIG), "整包被读完了，说明判定发生在读完之后"


def test_too_large_is_a_permanent_error_and_is_not_retried() -> None:
    """超限重试只会再超一次：桩计到的请求次数必须是 **1**。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-length": str(len(_BIG))},
            stream=_CountingStream(_BIG),
        )

    with pytest.raises(FetchError) as info:
        _client(handler, max_bytes=CHUNK).fetch("https://example.org/huge.bin")

    assert info.value.kind == "too_large"
    assert len(calls) == 1, f"too_large 是永久错误，却发了 {len(calls)} 次请求：{calls}"
