"""音视频判定：只看响应头与前 8KB 魔数 —— 让「不抽音视频正文」不必先把整包下下来。

为什么单独成模块：判定必须发生在**读 body 之前**（``fetch/http.py`` 的流式读取里），而落盘/抽正文
那一侧（``tools/source_fetch.py``）只要知道结论。放 ``fetch/``（最底层、只依赖标准库 + 兄弟模块）
两条路都能 import，不会打穿分层。

两条判据，顺序固定：

1. ``Content-Type`` 是 ``video/*`` / ``audio/*`` → 直接是媒体，**一个字节都不读**；
2. 头里说不清（``application/octet-stream``、缺失、``application/binary``…）→ 凑前
   ``MEDIA_SNIFF_BYTES`` 字节看魔数，认出来就**立即关流**。

魔数表（都是容器/码流自己的定长签名，翻不了车）：

| 魔数 | 格式 |
|---|---|
| ``ftyp`` @4..8 | MP4 / MOV / M4A / 3GP（ISO-BMFF） |
| ``ID3`` @0 | MP3（带 ID3v2 标签） |
| ``0xFF`` + ``0xE0`` 掩码 @0..2 | MP3 / ADTS AAC 的帧同步字 |
| ``1A 45 DF A3`` @0 | Matroska / WebM（EBML） |
| ``RIFF`` + ``WAVE``/``AVI `` @8..12 | WAV / AVI |
| ``OggS`` @0 | Ogg（Vorbis/Opus） |
| ``FLV`` @0 | FLV |
| ``0x47`` @0 且 @188 | MPEG-TS（188 字节一包，连看两包才算） |

**拿不准一律当"不是媒体"**：把一段真视频当文本，代价只是抽不出正文（会标 ``empty``）；
把一份纯文本源码当视频，代价是**静默丢掉正文**。后者更糟（``formats.resolve_ambiguous_ts``
对 `.ts` 也是同一个取舍），所以这里只在签名明确命中时才下结论。
"""

from __future__ import annotations

from scholarscout.fetch.formats import TS_PACKET_BYTES, TS_SYNC_BYTE

__all__ = ["MEDIA_SNIFF_BYTES", "is_media_type", "looks_like_media"]

#: 判魔数最多往下读多少字节。TS 的第二包同步字在 188，MP4 的 ``ftyp`` 在 4，8KB 足够宽松。
MEDIA_SNIFF_BYTES = 8192

#: ISO-BMFF（MP4/MOV/M4A/3GP）的 box 类型在偏移 4 处。
_FTYP = b"ftyp"


def is_media_type(media_type: str) -> bool:
    """``Content-Type`` → 是不是音视频（按 RFC 6838 的顶层类型判，忽略 ``; charset=`` 之类参数）。"""
    lowered = (media_type or "").strip().lower()
    return lowered.startswith(("video/", "audio/"))


def looks_like_media(head: bytes) -> bool:
    """魔数判定：``head`` 是正文最前面的若干个字节（不足也没关系，短到判不出就返回 ``False``）。"""
    if len(head) < 4:
        return False
    if head[4:8] == _FTYP:
        return True
    if head[:3] == b"ID3":
        return True
    # MP3 / ADTS AAC 帧同步：11 个 1 位。文本文件不会以 0xFF 开头（不是合法 UTF-8）。
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    if head[:4] == b"\x1aE\xdf\xa3":  # EBML：Matroska / WebM
        return True
    if head[:4] == b"RIFF" and head[8:12] in (b"WAVE", b"AVI "):
        return True
    if head[:4] == b"OggS":
        return True
    if head[:3] == b"FLV":
        return True
    # MPEG-TS：与 `formats._looks_like_mpeg_ts` 同一口径 —— 前两包同步字节都在才算。
    if len(head) >= TS_PACKET_BYTES * 2:
        return head[0] == TS_SYNC_BYTE and head[TS_PACKET_BYTES] == TS_SYNC_BYTE
    return False
