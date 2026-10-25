"""本地 PDF 抽正文（pymupdf）—— `source.fetch` 与 `file.read` 共用的**唯一实现**。

放 `fetch/` 的理由与 `formats.py` 一样：扩展名判定与本地抽取是最底层能力，
**工具层有两处**要用它（抓取落库的 `source.fetch`、只读打开的 `file.read`），
写在任一工具模块里就会变成「另一个工具 import 别人的私有函数」——
分层测试禁止 `fetch` 依赖 `tools`，反过来 `tools` 之间互相伸手也会把两个工具焊死。

口径不变：抽不到文字时如实返回 ``no_text_layer``（不返回空串冒充"提取成功"），
由调用方决定要不要走 MinerU。
"""

from __future__ import annotations

import time
from collections.abc import Callable


def pdf_text(
    content: bytes,
    *,
    budget_s: float,
    clock: Callable[[], float] | None = None,
) -> tuple[str, str, str, str]:
    """PDF 字节 → ``(正文, 状态, 引擎, 说明)``。状态 ∈ ``ok`` / ``no_text_layer`` / ``extract_timeout``。

    ``budget_s`` 是**抽取阶段的墙钟预算**（分段超时里的"抽取"那一段）：逐页循环里检查，
    超了就停下并标 ``extract_timeout`` —— 宁可这一条没摘要（如实标注），也不让整轮卡在本地解析上。
    实测 arXiv 800KB / 54k 字符的 PDF 只要 0.07–0.2s，所以正常路径永远碰不到它。

    ``clock`` 是**注入的单调时钟**（缺省 ``time.monotonic``）：``source.fetch`` 的调用点传
    自己模块里的 ``time.monotonic``，这样"抽到一半超预算"那条分支能在测试里用假时钟钉住
    （在 Windows 上把预算压到 0 也等不出超时 —— ``time.monotonic`` 粒度约 15.6ms，实测踩过）。

    ``file.read`` 传的是一个够大的预算（``fetch.extract_timeout_s``，默认 60s）：
    只读打开一个本地文件不该有"抽到一半"的中间态，但也不能让一份坏 PDF 把一轮对话挂死。
    """
    now = clock or time.monotonic
    try:
        import pymupdf
    except ImportError:
        return "", "no_text_layer", "none", "pymupdf 未安装"
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
        try:
            total = document.page_count
            deadline = now() + max(0.0, budget_s)
            pages: list[str] = []
            for index in range(total):
                # ``>=`` 而不是 ``>``：Windows 上 ``time.monotonic()`` 走 GetTickCount64，
                # 粒度约 15.6ms —— 同一刻度内两次读到**相等**的值，用 ``>`` 会让"预算已用尽"
                # 变成漏判（预算 0 也照样把整份 PDF 抽完）。
                if index and now() >= deadline:
                    return (
                        "",
                        "extract_timeout",
                        "local",
                        f"本地抽取超过 {budget_s:g}s（已抽 {index}/{total} 页）",
                    )
                pages.append(document.load_page(index).get_text())
        finally:
            document.close()
    except Exception as exc:
        return "", "no_text_layer", "local", f"pymupdf 打开失败：{type(exc).__name__}"
    text = "\n".join(pages).strip()
    if text:
        return text, "ok", "local", ""
    return "", "no_text_layer", "local", "没有文字层（多为扫描版）→ 交给 MinerU"


__all__ = ["pdf_text"]
