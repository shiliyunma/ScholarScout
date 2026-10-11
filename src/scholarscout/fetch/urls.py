"""抓取目标的 URL 判定：来源类型 +「落地页 → 真文件」改写。

放 `fetch/`（最底层、只依赖标准库）的理由：这两件事被 **工具层（`tools/source_fetch.py`）与
协议层（`rpc/archive.py` 的按需下载）共用**，放这里两边都能 import，不会打穿分层
（`tests/test_layering.py`：`rpc` 可以依赖 `fetch`，`fetch` 不许反过来）。

两件事：

1. ``guess_source_type`` —— 只按域名判**来源类型**（arxiv / github / official / web），
   用于 ``paths.doc_type_for`` 拼落盘目录、以及 ``sources.source_type``。
2. ``prefer_direct_file`` —— 有**明确文件入口**的来源，把落地页换成真文件
   （目前只有 arXiv：``/abs/<id>`` → ``/pdf/<id>``）。抓不到再回落到原 URL；
   抓的哪一种如实记在 ``source.fetch`` 的返回里（docs/19 §8.2）。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

#: 有明确文件入口的来源：把"落地页"换成"真文件"。目前只有 arXiv（`/abs/<id>` → `/pdf/<id>`）。
_ARXIV_ABS_RE = re.compile(r"^https?://(?:www\.)?arxiv\.org/abs/(?P<id>[^?#]+)", re.IGNORECASE)


def guess_source_type(url: str) -> str:
    """URL → 来源类型。只按域名判，认不出来回 ``web``（**不编类型**）。"""
    host = (urlparse(url).hostname or "").lower()
    if "arxiv.org" in host:
        return "arxiv"
    if "github.com" in host:
        return "github"
    if host.startswith("docs.") or "/docs/" in url:
        return "official"
    return "web"


def prefer_direct_file(url: str) -> str | None:
    """落地页 → 真文件的候选地址；没有候选返回 ``None``（那就照原 URL 抓）。

    为什么只做 arXiv：它的文件入口是稳定的公开约定（``/abs/<id>`` ↔ ``/pdf/<id>``）。
    别的站点没有这种可依赖的映射，硬猜只会把抓取变成"先撞一次 404 再回落"，得不偿失。
    """
    match = _ARXIV_ABS_RE.match(url)
    if match:
        return f"https://arxiv.org/pdf/{match.group('id')}"
    return None


__all__ = ["guess_source_type", "prefer_direct_file"]
