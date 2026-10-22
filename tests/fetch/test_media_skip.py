"""「音视频在下载之前就判掉」的契约：`Content-Type` → 前 8KB 魔数 → 才决定读不读整包。

全程 `httpx.MockTransport` + 自定义 `SyncByteStream`（记下**实际被消费的字节数**），
不真出网、不真 sleep。这里钉的是**顺序**（先判后读）与**早断**（判出来就不再往下读一个字节），
魔数表本身另有一张参数化表。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.fetch.media import MEDIA_SNIFF_BYTES, looks_like_media


class _CountingStream(httpx.SyncByteStream):
    """把「到底从流里读走了多少字节」记在 `consumed` 上 —— 早断的断言全靠它。"""

    def __init__(self, payload: bytes, *, chunk: int = 4096) -> None:
        self.payload = payload
        self.chunk = chunk
        self.consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self.payload), self.chunk):
            piece = self.payload[start : start + self.chunk]
            self.consumed += len(piece)
            yield piece


#: 真实的 MP4 头（ISO-BMFF：偏移 4 处是 `ftyp`）。
MP4_HEAD = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: object) -> HttpClient:
    return HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        clock=lambda: 1000.0,
        sleep=lambda seconds: None,
        **kwargs,
    )


def _streamed(
    payload: bytes, *, content_type: str | None, chunk: int = 4096
) -> tuple[httpx.Response, _CountingStream]:
    """造一个**流式**回包（`stream=` 不会自动补 Content-Length，和真实 chunked 一样）。"""
    stream = _CountingStream(payload, chunk=chunk)
    headers = {} if content_type is None else {"content-type": content_type}
    return httpx.Response(200, headers=headers, stream=stream), stream


def test_video_content_type_is_judged_before_any_byte_is_read(tmp_path: Path) -> None:
    """`video/*` → 一个字节都不读（15MB 的 mp4 不再白下），也不许落进缓存。"""
    payload = MP4_HEAD + b"\x00" * 15_000_000
    streams: list[_CountingStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response, stream = _streamed(payload, content_type="video/mp4")
        streams.append(stream)
        return response

    result = _client(handler, cache_dir=tmp_path).fetch(
        "https://cdn.example.com/lecture.mp4", peek_media=True
    )

    assert result.body_skipped is True, "音视频必须标成「正文没下载」，不能装作正文是空的"
    assert result.content == b""
    assert result.media_type == "video/mp4"
    assert streams[0].consumed == 0, f"body 被读了 {streams[0].consumed} 字节"
    assert not list(tmp_path.iterdir()), "没读过正文就不许写缓存（空 body 会被回放成空文件）"


def test_audio_content_type_is_skipped_too() -> None:
    payload = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 1_000_000
    streams: list[_CountingStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response, stream = _streamed(payload, content_type="audio/mpeg")
        streams.append(stream)
        return response

    assert _client(handler).fetch("https://cdn.example.com/talk", peek_media=True).body_skipped
    assert streams[0].consumed == 0


def test_octet_stream_with_mp4_magic_stops_after_the_sniff_window() -> None:
    """头里说不清（`application/octet-stream`）→ 只看前 8KB 魔数；判出就立刻关流。"""
    payload = MP4_HEAD + b"\x00" * 5_000_000
    streams: list[_CountingStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response, stream = _streamed(payload, content_type="application/octet-stream")
        streams.append(stream)
        return response

    result = _client(handler).fetch("https://files.example.com/v", peek_media=True)

    assert result.body_skipped is True
    assert result.content == b""
    consumed = streams[0].consumed
    assert consumed <= MEDIA_SNIFF_BYTES, f"判出音视频后还在读：{consumed} 字节"
    assert consumed < len(payload), "整包被读完了，说明判定发生在读完之后"


def test_octet_stream_text_is_read_to_the_end_unchanged() -> None:
    """说不清 + 不是音视频 → 照旧读全（行为与改前一致：文本一个字都不能少）。"""
    payload = ("教育智能体先检索再作答。" * 800).encode()
    assert len(payload) > MEDIA_SNIFF_BYTES
    streams: list[_CountingStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response, stream = _streamed(payload, content_type="application/octet-stream")
        streams.append(stream)
        return response

    result = _client(handler).fetch("https://files.example.com/notes", peek_media=True)

    assert result.body_skipped is False
    assert result.content == payload
    assert streams[0].consumed == len(payload), "文本必须读到最后一段"


def test_text_starting_with_ts_sync_byte_is_not_mistaken_for_video() -> None:
    """首字节恰好是 `0x47`（`G`）的**文本**不许被当成 MPEG-TS：连看两包才算。"""
    payload = b"Given the paper reports 42% -> 58%, see appendix G." * 500

    def handler(request: httpx.Request) -> httpx.Response:
        response, _stream = _streamed(payload, content_type="application/octet-stream")
        return response

    result = _client(handler).fetch("https://files.example.com/notes.txt", peek_media=True)
    assert result.body_skipped is False
    assert result.content == payload


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (MP4_HEAD, True),
        (b"ID3\x03\x00\x00\x00", True),
        (b"\xff\xfb\x90\x00", True),  # MP3 帧同步
        (b"\x1aE\xdf\xa3\x01\x00", True),  # Matroska / WebM
        (b"RIFF\x24\x08\x00\x00WAVEfmt ", True),
        (b"OggS\x00\x02\x00\x00", True),
        (b"FLV\x01\x05\x00\x00", True),
        (b"\x47" + b"\x00" * 187 + b"\x47" + b"\x00" * 187, True),  # MPEG-TS 两包
        (b"%PDF-1.7\n", False),
        (b"PK\x03\x04", False),  # docx/xlsx/pptx
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", False),  # 老 Office
        (b"<html><body>hi</body></html>", False),
        (b"", False),
    ],
)
def test_magic_table(head: bytes, expected: bool) -> None:
    assert looks_like_media(head) is expected
