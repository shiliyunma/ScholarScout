"""窗口几何的记住/恢复（`desktop/window_state.py`）。

这类功能的错法都很安静：把窗口开到已拔掉的显示器上、存出比最小尺寸还小的窗口、
配置里被手改成字符串之后再也不记住 —— 用户看到的是"程序好像没启动"或"窗口每次都变回去"，
日志里什么都没有。所以这里逐条钉住。

**几何存在 `config.yaml` 的 `ui.window`**（原来的 `run/window.json` 已废弃）：
用户要在设置里"看到/恢复默认"，而设置改的就是 `config.yaml`；两处记录必然漂。
所以本文件的每个用例都围绕这一个键：读写它、别的键不许丢、`clear()` 之后回默认。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scholarscout.desktop import window_state as ws

#: 一块 1920x1080 的屏（`(x, y, w, h)`，与 pywebview `screens` 同形）
SCREEN = [(0, 0, 1920, 1080)]


def cfg(tmp_path: Path) -> Path:
    """配置文件路径（**文件**，不是目录 —— `config_path` 收的是文件路径）。"""
    return tmp_path / "config.yaml"


def write(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ── 读：缺文件 / 坏文件 / 手改成怪值 ───────────────────────────────────────


def test_missing_file_falls_back_to_default_size(tmp_path: Path) -> None:
    assert ws.load(cfg(tmp_path)) == {"width": 1440, "height": 900, "maximized": False}
    assert (ws.DEFAULT_SIZE, ws.MIN_SIZE) == ((1440, 900), (1280, 800))


def test_corrupt_yaml_is_ignored(tmp_path: Path) -> None:
    """手改坏了配置文件不该连窗口都开不出来。"""
    cfg(tmp_path).write_text("ui: [不是\n  - 字典", encoding="utf-8")
    assert ws.load(cfg(tmp_path))["width"] == ws.DEFAULT_SIZE[0]


def test_no_ui_section_means_default(tmp_path: Path) -> None:
    write(cfg(tmp_path), {"llm": {"model": "x"}})
    assert ws.load(cfg(tmp_path)) == {"width": 1440, "height": 900, "maximized": False}


def test_ui_window_null_means_default(tmp_path: Path) -> None:
    """`config.example.yaml` 里的默认值就是 `null`：不能崩，也不能当成 0x0。"""
    write(cfg(tmp_path), {"ui": {"window": None}})
    assert ws.load(cfg(tmp_path)) == {"width": 1440, "height": 900, "maximized": False}


def test_round_trip_reads_back_ui_window(tmp_path: Path) -> None:
    write(
        cfg(tmp_path),
        {"ui": {"window": {"width": 1600, "height": 1000, "x": 120, "y": 64, "maximized": False}}},
    )
    assert ws.load(cfg(tmp_path), screens=SCREEN) == {
        "width": 1600,
        "height": 1000,
        "x": 120,
        "y": 64,
        "maximized": False,
    }


# ── 宽高校验：过小 / 超大 / 负数 / 字符串 / 缺字段 ─────────────────────────


def test_size_smaller_than_minimum_returns_default_not_minimum(tmp_path: Path) -> None:
    """比最小尺寸还小 → **回默认**，不是"抬到最小"。

    抬到 1280x800 会让用户停在一个他从没选过的尺寸上；回默认至少是可预期的。
    """
    write(cfg(tmp_path), {"ui": {"window": {"width": 200, "height": 100}}})
    state = ws.load(cfg(tmp_path))
    assert (state["width"], state["height"]) == ws.DEFAULT_SIZE


def test_size_larger_than_screen_is_clamped(tmp_path: Path) -> None:
    """旧值可能是别的大屏机器存的；窗口绝不能比屏幕大。"""
    write(cfg(tmp_path), {"ui": {"window": {"width": 4000, "height": 3000}}})
    state = ws.load(cfg(tmp_path), screens=SCREEN)
    assert (state["width"], state["height"]) == (1920, 1080)


def test_negative_size_is_rejected(tmp_path: Path) -> None:
    write(cfg(tmp_path), {"ui": {"window": {"width": -1600, "height": -900}}})
    assert (ws.load(cfg(tmp_path))["width"], ws.load(cfg(tmp_path))["height"]) == ws.DEFAULT_SIZE


@pytest.mark.parametrize(
    "value",
    [
        {"width": "1600", "height": "1000"},
        {"width": "abc", "height": None},
        {"width": True, "height": False},
        {"width": 1600.5, "height": 1000},
        {"width": 1600},
        {"height": 1000},
        {},
    ],
)
def test_non_positive_integer_size_is_rejected(tmp_path: Path, value: dict) -> None:
    """字符串 / 浮点 / bool / 缺一半 —— 一律整对丢弃（只认正整数）。"""
    write(cfg(tmp_path), {"ui": {"window": value}})
    state = ws.load(cfg(tmp_path))
    assert (state["width"], state["height"]) == ws.DEFAULT_SIZE


def test_non_dict_window_is_rejected(tmp_path: Path) -> None:
    write(cfg(tmp_path), {"ui": {"window": "1600x1000"}})
    assert ws.load(cfg(tmp_path))["width"] == ws.DEFAULT_SIZE[0]


# ── 位置：拔掉外接屏 / 无屏幕信息 / 贴边 ─────────────────────────────────


def test_position_off_all_screens_is_dropped(tmp_path: Path) -> None:
    """拔掉外接屏之后，上次的坐标可能落在不存在的区域 —— 窗口会开到看不见的地方。"""
    write(cfg(tmp_path), {"ui": {"window": {"width": 1400, "height": 900, "x": 3000, "y": 2000}}})
    assert "x" not in ws.load(cfg(tmp_path), screens=SCREEN)
    assert ws.load(cfg(tmp_path), screens=[(0, 0, 3840, 2160)])["x"] == 3000


def test_position_without_screen_info_is_kept(tmp_path: Path) -> None:
    """拿不到屏幕信息时不该自作主张丢掉位置（多屏环境下 pywebview 可能报不出来）。"""
    write(cfg(tmp_path), {"ui": {"window": {"width": 1400, "height": 900, "x": 2400, "y": 200}}})
    assert ws.load(cfg(tmp_path))["x"] == 2400


def test_position_must_be_a_pair(tmp_path: Path) -> None:
    write(cfg(tmp_path), {"ui": {"window": {"width": 1400, "height": 900, "x": 100}}})
    assert "x" not in ws.load(cfg(tmp_path))
    write(cfg(tmp_path), {"ui": {"window": {"width": 1400, "height": 900, "x": "100", "y": 80}}})
    assert "x" not in ws.load(cfg(tmp_path))


def test_negative_position_on_left_monitor_is_kept(tmp_path: Path) -> None:
    """左侧副屏的 x 就是负的 —— 不能用"必须为正"把合法位置丢掉。"""
    screens = [(-1920, 0, 1920, 1080), (0, 0, 1920, 1080)]
    write(cfg(tmp_path), {"ui": {"window": {"width": 1400, "height": 900, "x": -1800, "y": 100}}})
    state = ws.load(cfg(tmp_path), screens=screens)
    assert (state["x"], state["y"]) == (-1800, 100)


def test_position_is_clamped_so_the_title_bar_stays_reachable(tmp_path: Path) -> None:
    """略微出屏（多屏拼接处）是正常状态，但标题行与左缘必须抓得住。"""
    write(cfg(tmp_path), {"ui": {"window": {"width": 1400, "height": 900, "x": 1870, "y": -30}}})
    state = ws.load(cfg(tmp_path), screens=SCREEN)
    assert state["x"] == 1920 - ws.KEEP_VISIBLE
    assert state["y"] == 0


# ── maximized：只透传 True，别猜 ─────────────────────────────────────────


def test_maximized_only_when_explicitly_true(tmp_path: Path) -> None:
    """**不要用"窗口尺寸≈屏幕尺寸"猜最大化**（screens 与窗口尺寸单位不一致会误判）。"""
    write(cfg(tmp_path), {"ui": {"window": {"width": 1400, "height": 900, "maximized": "yes"}}})
    assert ws.load(cfg(tmp_path))["maximized"] is False
    write(cfg(tmp_path), {"ui": {"window": {"width": 1400, "height": 900, "maximized": True}}})
    assert ws.load(cfg(tmp_path))["maximized"] is True


# ── same()：没动窗口就不许重写 config.yaml（重写会丢注释） ────────────────


def test_same_is_true_for_identical_geometry() -> None:
    left = {"width": 1400, "height": 900, "x": 10, "y": 20, "maximized": False}
    assert ws.same(left, dict(left)) is True


@pytest.mark.parametrize("key", ["width", "height", "x", "y", "maximized"])
def test_same_sees_every_saved_key(key: str) -> None:
    left = {"width": 1400, "height": 900, "x": 10, "y": 20, "maximized": False}
    right = dict(left)
    right[key] = (not right[key]) if key == "maximized" else right[key] + 1
    assert ws.same(left, right) is False, f"{key} 变了却没被判成「变了」（关窗会漏写）"


def test_same_ignores_keys_that_are_not_saved() -> None:
    """只有会被写进文件的键参与比较；多出来的字段（例如 screens）不影响判定。"""
    left = {"width": 1400, "height": 900, "x": 10, "y": 20, "maximized": False}
    assert ws.same(left, {**left, "screens": [(0, 0, 1920, 1080)], "screen": 1}) is True


# ── 写：进 config.yaml、别的键不许丢、clear 后回默认 ─────────────────────


def test_save_writes_ui_window_and_keeps_other_keys(tmp_path: Path) -> None:
    path = cfg(tmp_path)
    write(path, {"llm": {"model": "deepseek-chat"}, "ui": {"theme": "dark"}})

    payload = ws.save(width=1520, height=960, x=40, y=30, path=path)

    assert payload == {"width": 1520, "height": 960, "maximized": False, "x": 40, "y": 30}
    data = read(path)
    assert data["ui"]["window"] == {
        "width": 1520,
        "height": 960,
        "maximized": False,
        "x": 40,
        "y": 30,
    }
    # 深合并不是整体替换：别的段与同段的其他键都不许丢
    assert data["llm"]["model"] == "deepseek-chat"
    assert data["ui"]["theme"] == "dark"


def test_save_without_position_leaves_x_y_out(tmp_path: Path) -> None:
    path = cfg(tmp_path)
    ws.save(width=1520, height=960, path=path)
    window = read(path)["ui"]["window"]
    assert "x" not in window and "y" not in window
    assert ws.load(path) == {"width": 1520, "height": 960, "maximized": False}


def test_save_is_atomic_and_leaves_no_tmp_file(tmp_path: Path) -> None:
    path = cfg(tmp_path)
    ws.save(width=1520, height=960, path=path)
    assert not list(tmp_path.glob("*.tmp")), "原子写没有留下 .tmp（崩在中间会把配置截断成半截）"


# ── 写前 normalize：脏值不许落盘（"几何每夜静默失效"的根因） ──────────────
#
# 事故形状：save 只做 max(1, int()) → 最小化后的 0x0、DPI 换算出的 158x26、
# 拔掉外接屏后的 x: -21333 全被写进 config.yaml；读端 normalize 见到非法值整对丢弃
# → 回默认 → 关窗又把脏值写回去。用户看到"窗口大小每夜变回默认"，日志里什么都没有。


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (1, 26),  # DPI 换算后的残值（真实出现过：width: 158 / height: 26）
        (159, 27),
        (1279, 799),  # 刚好差一像素
    ],
)
def test_save_never_writes_a_size_below_the_minimum(
    tmp_path: Path, width: int, height: int
) -> None:
    """比最小尺寸小的"尺寸"不是用户选的几何 → 落盘的只能是读端认可的默认尺寸。"""
    path = cfg(tmp_path)
    ws.save(width=width, height=height, path=path)

    window = read(path)["ui"]["window"]
    assert (window["width"], window["height"]) == ws.DEFAULT_SIZE
    assert window["width"] >= ws.MIN_SIZE[0] and window["height"] >= ws.MIN_SIZE[1]


def test_save_clamps_a_size_larger_than_the_screen(tmp_path: Path) -> None:
    path = cfg(tmp_path)
    ws.save(width=4000, height=3000, path=path, screens=SCREEN)
    window = read(path)["ui"]["window"]
    assert (window["width"], window["height"]) == (1920, 1080)


def test_save_drops_a_position_that_is_off_every_screen(tmp_path: Path) -> None:
    """`x: -21333`（外接屏拔掉后的残值）不许落盘：位置整对不写，只记尺寸。"""
    path = cfg(tmp_path)
    ws.save(width=1520, height=960, x=-21333, y=-21333, path=path, screens=SCREEN)

    window = read(path)["ui"]["window"]
    assert "x" not in window and "y" not in window
    assert (window["width"], window["height"]) == (1520, 960)


def test_save_keeps_a_legal_position_on_a_left_monitor(tmp_path: Path) -> None:
    """写端也不能把左侧副屏的负坐标误杀（读端同样的规则）。"""
    screens = [(-1920, 0, 1920, 1080), (0, 0, 1920, 1080)]
    path = cfg(tmp_path)
    ws.save(width=1520, height=960, x=-1800, y=100, path=path, screens=screens)
    window = read(path)["ui"]["window"]
    assert (window["x"], window["y"]) == (-1800, 100)


def test_save_keeps_legal_geometry_verbatim(tmp_path: Path) -> None:
    """合法值原样保留 —— normalize 只该纠正非法值，不该顺手改用户的选择。"""
    path = cfg(tmp_path)
    ws.save(width=1520, height=960, x=40, y=30, path=path, screens=SCREEN)
    assert read(path)["ui"]["window"] == {
        "width": 1520,
        "height": 960,
        "maximized": False,
        "x": 40,
        "y": 30,
    }


@pytest.mark.parametrize("bad", [(0, 0), (0, 900), (1440, 0), (-1, -1)])
def test_minimized_or_hidden_window_is_not_written(tmp_path: Path, bad: tuple[int, int]) -> None:
    """最小化/隐藏时 window.width/height 报 0（或负）—— 那不是几何，一个字节都不该动。"""
    path = cfg(tmp_path)
    write(path, {"ui": {"theme": "dark", "window": {"width": 1520, "height": 960}}})
    before_text = path.read_text(encoding="utf-8")
    before_mtime = path.stat().st_mtime_ns

    ws.save(width=bad[0], height=bad[1], path=path)

    assert path.read_text(encoding="utf-8") == before_text
    assert path.stat().st_mtime_ns == before_mtime, "文件被重写过（最小化不该写）"
    assert read(path)["ui"]["window"] == {"width": 1520, "height": 960}


def test_save_ignores_a_non_numeric_size(tmp_path: Path) -> None:
    """窗口属性拿不到时可能是字符串/None：不许崩，也不许把垃圾写进配置。"""
    path = cfg(tmp_path)
    write(path, {"ui": {"window": {"width": 1440, "height": 900}}})
    before_text = path.read_text(encoding="utf-8")

    ws.save(width="abc", height=None, path=path)

    assert path.read_text(encoding="utf-8") == before_text
    assert (ws.load(path)["width"], ws.load(path)["height"]) == (1440, 900)


def test_unchanged_geometry_does_not_rewrite_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关窗时几何没变就不许重写 `config.yaml`（重写会丢注释）——按写次数断言。"""
    path = cfg(tmp_path)
    ws.save(width=1520, height=960, x=40, y=30, path=path, screens=SCREEN)

    writes: list[Path] = []
    original = ws._atomic_write

    def counting_write(target: Path, payload: dict) -> None:
        writes.append(target)
        original(target, payload)

    monkeypatch.setattr(ws, "_atomic_write", counting_write)
    before_text = path.read_text(encoding="utf-8")

    ws.save(width=1520, height=960, x=40, y=30, path=path, screens=SCREEN)

    assert writes == [], "几何没变却重写了 config.yaml"
    assert path.read_text(encoding="utf-8") == before_text


def test_save_repairs_dirty_values_already_in_the_file(tmp_path: Path) -> None:
    """盘上已经躺着脏值（width: 158 / x: -21333）时，写一次就该把它修正成合法值。"""
    path = cfg(tmp_path)
    write(
        path,
        {
            "llm": {"model": "deepseek-chat"},
            "ui": {"window": {"width": 158, "height": 26, "x": -21333, "y": -21333}},
        },
    )

    ws.save(width=1520, height=960, path=path, screens=SCREEN)

    window = read(path)["ui"]["window"]
    assert (window["width"], window["height"]) == (1520, 960)
    assert "x" not in window and "y" not in window
    assert read(path)["llm"]["model"] == "deepseek-chat"
    # 修完之后读端读到的就是它自己写的值（不再"写脏值 → 读时丢弃 → 回默认"循环）
    assert ws.load(path, screens=SCREEN) == ws.normalize(window, screens=SCREEN)


def test_clear_restores_default_and_keeps_other_keys(tmp_path: Path) -> None:
    path = cfg(tmp_path)
    write(
        path, {"llm": {"model": "deepseek-chat"}, "ui": {"window": {"width": 1600, "height": 1000}}}
    )
    assert (ws.load(path)["width"], ws.load(path)["height"]) == (1600, 1000)

    ws.clear(path)

    assert read(path)["ui"]["window"] is None
    assert read(path)["llm"]["model"] == "deepseek-chat"
    assert ws.load(path) == {"width": 1440, "height": 900, "maximized": False}


def test_clear_on_missing_file_creates_ui_window_null(tmp_path: Path) -> None:
    """恢复默认是"能点得动"的操作：配置文件还没建时也必须成功。"""
    path = cfg(tmp_path)
    ws.clear(path)
    assert read(path) == {"ui": {"window": None}}


# ── 尺寸按屏幕收窄（1366x768 的笔记本） ──────────────────────────────────


def test_default_size_shrinks_to_small_screen() -> None:
    assert ws.default_size([(0, 0, 1366, 768)]) == (1366, 768)


def test_min_size_shrinks_to_small_screen() -> None:
    """800 高在 1366x768 上放不下；min_size 原样交给窗口管理器会让窗口一开就出屏。

    宽度 1280 在 1366 的屏上放得下，所以只收窄高度那一维 —— 不该顺手把宽度也改小。
    """
    assert ws.min_size([(0, 0, 1366, 768)]) == (1280, 768)
    assert ws.min_size(SCREEN) == ws.MIN_SIZE


def test_default_size_uses_the_largest_screen() -> None:
    assert ws.default_size([(0, 0, 1366, 768), (1366, 0, 2560, 1440)]) == ws.DEFAULT_SIZE


# ── 配置文件位置：与 settings_io 用同一个文件 ─────────────────────────────


def test_explicit_path_wins(tmp_path: Path) -> None:
    assert ws.config_path(tmp_path / "a.yaml") == tmp_path / "a.yaml"


def test_env_override_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHOLARSCOUT_CONFIG", str(tmp_path / "env.yaml"))
    assert ws.config_path() == tmp_path / "env.yaml"
