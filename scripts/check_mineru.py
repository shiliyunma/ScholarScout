"""MinerU 扫描版自检：本地造一份**真扫描版 PDF**（只有图像、无文字层）走一遍官方接口。

为什么要有它：论文里"扫描版能抽出正文"这条一直是**未验证**（老实现只认公开 URL，
而公网可达的扫描版样本在这台机器上拿不到）。现在官方有"签名上传"接口（免 Token 也行），
所以可以在本机直接验完：**造样本 → 上传 → 轮询 → 打印抽出的 Markdown 片段**。

用法：
    # 免 Token（走 Agent 轻量接口，≤10MB / ≤20 页）
    .venv/Scripts/python.exe scripts/check_mineru.py --make-sample run/scan-sample.pdf

    # 有 Token（走精准接口，≤200MB / 200 页）
    .venv/Scripts/python.exe scripts/check_mineru.py --pdf run/scan-sample.pdf

判读：`state=done` 且 `engine=agent|precise` 且有正文片段 ＝ 这条路通了。
`state=failed` 时看 detail（官方失败原因会原样带回来，比如限页数、服务暂不可用）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SAMPLE_TEXT = """ScholarScout 扫描版自检样本

本文档用于验证扫描件（无文字层）能否被 MinerU 正确解析。

第一节 为什么要测扫描版
很多老论文、影印教材只有图像，本地 PDF 库抽不出文字。
本样本故意做成"图像 + 无文字层"，与真实扫描件的差别只在分辨率。

第二节 判读标准
若抽取结果里能看到上面这两段标题，即说明 OCR 链路可用。

第三节 随机指纹
指纹编号：SC-2026-1007-ALPHA
"""


def make_sample(target: Path, *, dpi: int = 150) -> Path:
    """造一份"扫描版"：先把文字渲染成图，再把图放进新 PDF —— 因此**没有文字层**。"""
    import fitz  # pymupdf（已批准依赖）

    text_doc = fitz.open()
    page = text_doc.new_page(width=595, height=842)
    page.insert_textbox(
        fitz.Rect(60, 60, 535, 780),
        SAMPLE_TEXT,
        fontsize=13,
        fontname="china-s",  # 内置简体中文字体
        lineheight=1.5,
    )
    pixmap = page.get_pixmap(dpi=dpi)
    image = pixmap.tobytes("png")
    text_doc.close()

    scan_doc = fitz.open()
    scan_page = scan_doc.new_page(width=595, height=842)
    scan_page.insert_image(fitz.Rect(0, 0, 595, 842), stream=image)
    target.parent.mkdir(parents=True, exist_ok=True)
    scan_doc.save(str(target))
    scan_doc.close()

    # 自检：一定要确认"没有文字层"，否则这个验证本身是假的
    check = fitz.open(str(target))
    extracted = "".join(p.get_text() for p in check)
    check.close()
    if extracted.strip():
        raise SystemExit(f"样本不对：居然抽出了 {len(extracted)} 字文字层 —— 这不是扫描版")
    return target


def main() -> int:
    from scholarscout.config import load_config
    from scholarscout.fetch.http import HttpClient
    from scholarscout.fetch.mineru import parse_pdf_bytes
    from scholarscout.fetch.settings import mineru_token

    parser = argparse.ArgumentParser(description="MinerU 扫描版自检（本地上传）")
    parser.add_argument("--pdf", default=str(ROOT / "run" / "scan-sample.pdf"))
    parser.add_argument("--make-sample", default=None, help="先造一份扫描版样本到该路径")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--chars", type=int, default=600)
    args = parser.parse_args()

    load_config()  # 读 .env（token 在这里面）
    pdf = make_sample(Path(args.make_sample)) if args.make_sample else Path(args.pdf)
    if not pdf.exists():
        print(f"[FAIL] 找不到 PDF：{pdf}", file=sys.stderr)
        return 1
    content = pdf.read_bytes()
    token = mineru_token()
    print(f"样本：{pdf}（{len(content) / 1024:.0f} KB）")
    print(f"引擎：{'precise（有 Token）' if token else 'agent（无 Token，免登录）'}")
    started = time.monotonic()
    with HttpClient() as http:
        result = parse_pdf_bytes(
            http,
            content,
            filename=pdf.name,
            token=token,
            timeout_s=args.timeout,
            poll_interval_s=3.0,
        )
    spent = time.monotonic() - started
    print(f"耗时：{spent:.1f}s｜state={result.state}｜engine={result.engine}")
    if result.detail:
        print(f"detail：{result.detail}")
    if result.ok:
        print("---- 抽出正文的前几行 ----")
        print("\n".join(result.text.strip().splitlines()[:12]))
        print(f"---- 共 {len(result.text)} 字符 ----")
        return 0
    print("[FAIL] 没拿到正文", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
