"""图标与启动器构建脚本的回归测试（离线；不依赖 csc 与网络）。

为什么值得测：这两个是"构建期一次性"的东西，它坏了不会让测试变红、只会让用户
拿到一个**图标没嵌进去的 exe** 或一个**打不开的图标**——都是"看起来没问题"的错。
"""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def icon_builder() -> ModuleType:
    return load_script("make_icon")


@pytest.fixture(scope="module")
def exe_builder() -> ModuleType:
    return load_script("build_exe")


# --------------------------------------------------------------------- 图标


def test_build_ico_layout_is_a_readable_container(icon_builder: ModuleType) -> None:
    images = {16: b"\x89PNG\r\n\x1a\n" + b"a" * 10, 256: b"\x89PNG\r\n\x1a\n" + b"b" * 20}
    data = icon_builder.build_ico(images)
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind, count) == (0, 1, 2)
    assert icon_builder.read_ico_sizes(data) == [(16, 18), (256, 28)]


def test_256_is_encoded_as_zero_byte(icon_builder: ModuleType) -> None:
    """ICO 规定：宽高一个字节，256 要写 0。写成 256 会溢出 → Windows 认不出这一档。"""
    data = icon_builder.build_ico({256: b"\x89PNG\r\n\x1a\n"})
    width, height = struct.unpack_from("<BB", data, 6)
    assert (width, height) == (0, 0)


def test_ico_entry_offsets_point_at_png_payloads(icon_builder: ModuleType) -> None:
    data = icon_builder.build_ico({16: b"\x89PNG\r\n\x1a\nA", 32: b"\x89PNG\r\n\x1a\nBB"})
    _reserved, _kind, count = struct.unpack_from("<HHH", data, 0)
    assert count == 2
    for index in range(count):
        _w, _h, _c, _r, _p, _b, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + index * 16
        )
        assert data[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + length <= len(data)


def test_empty_images_are_rejected(icon_builder: ModuleType) -> None:
    with pytest.raises(ValueError):
        icon_builder.build_ico({})


def test_default_sizes_cover_windows_needs(icon_builder: ModuleType) -> None:
    assert 16 in icon_builder.SIZES and 256 in icon_builder.SIZES


def test_committed_icon_matches_the_sizes_it_claims(icon_builder: ModuleType) -> None:
    """仓库里那份 assets/icon.ico 必须真的是多尺寸容器（被 csc 与 pywebview 共用）。"""
    icon = ROOT / "assets" / "icon.ico"
    if not icon.is_file():
        pytest.skip("assets/icon.ico 不在仓库里（未生成）")
    sizes = [size for size, _length in icon_builder.read_ico_sizes(icon.read_bytes())]
    assert sizes == sorted(icon_builder.SIZES)


def test_render_from_source_png_produces_all_sizes(icon_builder: ModuleType) -> None:
    """真渲染一次（用源图，pymupdf）：确认缩放在本机可用。"""
    source = ROOT / "assets" / "icon-source.png"
    if not source.is_file():
        pytest.skip("assets/icon-source.png 不在仓库里")
    images = icon_builder.render_png_sizes(source, sizes=(32, 256))
    assert set(images) == {32, 256}
    for data in images.values():
        assert data.startswith(b"\x89PNG\r\n\x1a\n")


# ----------------------------------------------------------------- 启动器构建


def test_csc_command_embeds_icon_and_pins_utf8(exe_builder: ModuleType, tmp_path: Path) -> None:
    icon = tmp_path / "icon.ico"
    icon.write_bytes(b"\x00\x00\x01\x00")
    cmd = exe_builder.csc_command(
        Path("csc.exe"), source=Path("a.cs"), output=Path("b.exe"), icon=icon
    )
    joined = " ".join(str(part) for part in cmd)
    assert "/target:winexe" in cmd, "GUI 子系统：双击不能闪黑框"
    assert "/codepage:65001" in cmd, "源码是 UTF-8（含中文提示），不指定会按本地代码页猜错"
    assert f"/win32icon:{icon}" in joined
    assert cmd[-1].endswith("a.cs")


def test_csc_command_skips_missing_icon_file(exe_builder: ModuleType, tmp_path: Path) -> None:
    """图标文件不存在时**不能**把路径塞给 csc（那样编译直接失败）。"""
    cmd = exe_builder.csc_command(
        Path("csc.exe"), source=Path("a.cs"), output=Path("b.exe"), icon=tmp_path / "nope.ico"
    )
    assert not any(str(part).startswith("/win32icon:") for part in cmd)


def test_csc_command_without_icon_still_compiles(exe_builder: ModuleType) -> None:
    cmd = exe_builder.csc_command(
        Path("csc.exe"), source=Path("a.cs"), output=Path("b.exe"), icon=None
    )
    assert not any(str(part).startswith("/win32icon:") for part in cmd)


def test_default_output_is_the_project_root(exe_builder: ModuleType) -> None:
    """启动器是"入口"，默认输出在项目根 —— 放最外层双击最顺手。"""
    assert Path(exe_builder.OUT_DIR) == ROOT


def test_config_file_points_at_project_root(exe_builder: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / exe_builder.CONFIG_NAME
    exe_builder.write_config(ROOT, target)
    lines = [
        line
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.startswith("project=")
    ]
    assert lines == [f"project={ROOT}"]


def test_launcher_source_stays_freeze_free() -> None:
    """启动器不许内嵌业务代码 —— 否则"版本更替不失效"这条就没了。

    只要它还在用 `scout.py --desktop` 拉起项目，就说明业务代码仍在项目里。
    """
    source = (ROOT / "packaging" / "launcher" / "ScholarScoutLauncher.cs").read_text(
        encoding="utf-8"
    )
    assert "scripts" in source and "scout.py" in source
    assert "--desktop" in source
