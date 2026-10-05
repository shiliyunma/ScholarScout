"""把一张正方形 PNG 做成 Windows 多尺寸 ``.ico``（16/24/32/48/64/128/256）。

**为什么不用 Pillow**：它不是本项目的运行期依赖，为一个构建期小工具装一个图像库不划算
（规则：新增依赖要审批）。``pymupdf`` 已在白名单里且本来就能光栅化 ——
把 PNG 当"只有一页的文档"打开，用矩阵渲染成任意尺寸，再用 stdlib 拼 ICO 容器。

ICO 允许直接内嵌 PNG（Vista 起支持），所以不用手写 BMP/AND 掩码。

用法：
    python scripts/make_icon.py                      # assets/icon-source.png → assets/icon.ico
    python scripts/make_icon.py --src x.png --out y.ico
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = ROOT / "assets" / "icon-source.png"
DEFAULT_OUT = ROOT / "assets" / "icon.ico"

#: Windows 会按场景挑最合适的一档：16 任务栏小图标 / 32 桌面 / 48 资源管理器 / 256 大图标视图
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_png_sizes(src: Path, sizes: tuple[int, ...] = SIZES) -> dict[int, bytes]:
    """把源图渲染成 ``{边长: PNG 字节}``。"""
    import pymupdf  # 运行期依赖，已在 deps-allowlist

    images: dict[int, bytes] = {}
    with pymupdf.open(str(src)) as doc:
        page = doc[0]
        source_side = min(page.rect.width, page.rect.height)
        for size in sizes:
            scale = size / source_side
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            images[size] = pix.tobytes("png")
    return images


def build_ico(images: dict[int, bytes]) -> bytes:
    """按 ICO 格式拼容器：ICONDIR + N × ICONDIRENTRY + 各尺寸 PNG 数据。"""
    if not images:
        raise ValueError("至少要有一张图")
    entries = sorted(images.items())
    header = struct.pack("<HHH", 0, 1, len(entries))  # reserved / type=icon / count
    offset = len(header) + 16 * len(entries)
    directory = bytearray()
    payload = bytearray()
    for size, data in entries:
        # 宽高用一个字节表示，256 要写 0（ICO 规定）
        dimension = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII",
            dimension,  # 宽
            dimension,  # 高
            0,  # 调色板颜色数（PNG 内嵌时为 0）
            0,  # reserved
            1,  # 颜色平面
            32,  # 位深
            len(data),
            offset,
        )
        payload += data
        offset += len(data)
    return bytes(header + directory + payload)


def read_ico_sizes(data: bytes) -> list[tuple[int, int]]:
    """把 ICO 读回来（测试用）：返回 ``[(边长, 数据长度), ...]``。"""
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1:
        raise ValueError("不是 ICO 容器")
    out: list[tuple[int, int]] = []
    for index in range(count):
        width, _height, _colors, _res, _planes, _bits, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + index * 16
        )
        size = 256 if width == 0 else width
        if data[offset : offset + 8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"{size}px 那一档不是 PNG 数据")
        out.append((size, length))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成多尺寸 .ico")
    parser.add_argument("--src", default=str(DEFAULT_SRC), help="正方形 PNG 源图")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出的 .ico")
    args = parser.parse_args(argv)

    src = Path(args.src)
    out = Path(args.out)
    if not src.is_file():
        print(f"[FAIL] 找不到源图：{src}", file=sys.stderr)
        return 1
    images = render_png_sizes(src)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build_ico(images))
    sizes = ", ".join(f"{size}px/{len(data) // 1024}KB" for size, data in sorted(images.items()))
    print(f"[OK] 写出 {out}（{out.stat().st_size // 1024} KB）：{sizes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
