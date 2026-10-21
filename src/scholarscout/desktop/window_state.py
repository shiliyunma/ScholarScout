"""窗口几何的记住与恢复（配置键 `ui.window`：启动读、关窗写）。

为什么单独一个模块：读写配置这件事必须能**离线测**——直接写在 `app.py` 里就得起窗口才能验，
而"记住大小"这种需求恰恰错在细节（存了比最小尺寸还小的、存到已拔掉的显示器坐标、
配置里被手改成字符串）。

## 存在哪

`config.yaml` 的 `ui.window`（默认 `null` = 用默认尺寸）：

```yaml
ui:
  window: {width: 1440, height: 900, x: 120, y: 80, maximized: false}
```

为什么不放 `run/window.json`（本模块原来的做法）：用户要求的是"在设置里能看到/能恢复默认"，
而设置面板改的就是 `config.yaml`；两处记录必然漂（一个在设置里恢复、另一个文件又把窗口拉回去）。
代价：`config.yaml` 重写会丢注释（与 `settings_io` 写回时同一条已知代价），所以**只有几何真的变了
才写**（`same()` 判），开着关着没动窗口不会重写文件。

## 三条边界（都是真会遇到的）

1. **单位**：文件里一律存**逻辑像素**（`create_window(width=…)` 的单位）。`window.width` 报的是
   物理像素，由调用方除以 DPI 缩放后再存 —— 不换算出过事：存 1700x1050，下次开出 2539x1547。
   另注：**不要用"窗口尺寸≈屏幕尺寸"猜最大化**（screens 与窗口尺寸单位不一致会误判，
   误判的代价是尺寸干脆不记了，比"最大化的窗口下次以同尺寸打开"严重得多）。
2. **位置要能在当前显示器上落地**：笔记本外接屏拔掉后，上次的 x/y 可能指向不存在的区域，
   窗口会开在屏幕外（用户以为程序没启动）。落不进去就只恢复尺寸、位置交给系统。
3. **尺寸不得小于最小尺寸、也不得大于屏幕**：旧值可能是别的小屏机器存的；而最小尺寸在
   1366x768 这类小屏上本身就不该生效（`min_size()` 同样按屏幕收窄，否则窗口一开就超出屏幕）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from scholarscout import paths

#: 配置键（点分路径）。读写都只碰这一处。
KEY = "ui.window"

#: 默认窗口尺寸（逻辑像素）。1440x900：三栏（216 + 中栏 + 380）都能放开，
#: 又比 1920 屏窄 —— 直接铺满会让用户第一眼以为应用"占满整屏、没有窗口感"。
DEFAULT_SIZE = (1440, 900)

#: 最小窗口尺寸（逻辑像素）。**全项目只有这一处**：`bridge.py` 从这里取（窗控夹尺寸、
#: 自绘把手的下限），`app.py` 拿它建窗（屏幕更小时按 `min_size()` 收窄）。
MIN_SIZE = (1280, 800)

#: 位置允许"贴边/略微出屏"的余量（多屏拼接处窗口跨屏是正常状态）
POSITION_MARGIN = 40

#: 夹位置时窗口至少要留在屏内的宽度（标题行与边缘要抓得住）
KEEP_VISIBLE = 80


def config_path(path: Path | None = None) -> Path:
    """`config.yaml` 的位置：显式参数 > `SCHOLARSCOUT_CONFIG` > 项目根。

    与 `settings_io.config_path_of` 同一条规则（那边写设置、这边写几何，必须是同一个文件）。
    """
    if path is not None:
        return Path(path)
    override = os.environ.get("SCHOLARSCOUT_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return paths.home() / "config.yaml"


def default_size(screens: list[tuple[int, int, int, int]] | None = None) -> tuple[int, int]:
    """默认尺寸，**按屏幕可用尺寸收窄**（小屏不许一开就超出视口）。"""
    return _fit(DEFAULT_SIZE, screens)


def min_size(screens: list[tuple[int, int, int, int]] | None = None) -> tuple[int, int]:
    """最小尺寸，同样按屏幕收窄。

    1280x800 在 1366x768 的笔记本上根本放不下 —— 若把 min_size 原样交给窗口管理器，
    窗口会一开就超出屏幕（用户只能看一半，且拖不动）。所以下限也得跟着屏幕走。
    """
    return _fit(MIN_SIZE, screens)


def normalize(
    raw: object, *, screens: list[tuple[int, int, int, int]] | None = None
) -> dict[str, Any]:
    """把配置里的 `ui.window` 变成**可直接交给 `create_window`** 的几何。

    规则（每条都有测试）：

    - 缺失 / 不是字典 / 类型不对 → 默认尺寸，不定位（交给系统居中）；
    - 宽高必须是**正整数**且不小于最小尺寸，否则**整对丢弃**（用默认尺寸）——
      "抬到最小尺寸"会让用户停在一个他从没选过的尺寸上，还不如回默认；
    - 超出屏幕 → **夹回屏内**（窗口绝不比屏幕大）；
    - x/y 必须成对、都是整数、且落在某块屏上；否则不恢复位置。
    """
    data = raw if isinstance(raw, dict) else {}
    wide, tall = _positive_int(data.get("width")), _positive_int(data.get("height"))
    if wide is None or tall is None or wide < MIN_SIZE[0] or tall < MIN_SIZE[1]:
        wide, tall = default_size(screens)
    limit = _screen_size(screens)
    if limit is not None:
        wide, tall = min(wide, limit[0]), min(tall, limit[1])
    result: dict[str, Any] = {
        "width": wide,
        "height": tall,
        "maximized": data.get("maximized") is True,
    }
    position = _position(data.get("x"), data.get("y"), screens)
    if position is not None:
        result["x"], result["y"] = position
    return result


def same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """两份几何是不是"没变"（关窗时据此决定要不要重写 `config.yaml`）。

    只比会被保存的键：写文件会丢注释，没动窗口就不该碰它。
    """
    keys = ("width", "height", "x", "y", "maximized")
    return all(left.get(key) == right.get(key) for key in keys)


def load(
    path: Path | None = None, *, screens: list[tuple[int, int, int, int]] | None = None
) -> dict[str, Any]:
    """直接读 `config.yaml` 的 `ui.window` 并校验（不加载整份配置）。"""
    try:
        raw = yaml.safe_load(config_path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return normalize(None, screens=screens)
    section = raw.get("ui") if isinstance(raw, dict) else None
    value = section.get("window") if isinstance(section, dict) else None
    return normalize(value, screens=screens)


def save(
    *,
    width: int,
    height: int,
    x: int | None = None,
    y: int | None = None,
    maximized: bool = False,
    path: Path | None = None,
    screens: list[tuple[int, int, int, int]] | None = None,
) -> dict[str, Any]:
    """把窗口几何写进 `config.yaml` 的 `ui.window`（**写前必过 `normalize`** + 原子写）。

    **为什么写前必须 normalize**（这里踩过最安静的一个坑）：`save` 原来只做 `max(1, int())`，
    于是最小化的窗口（0x0 / 负值）、DPI 换算后的 158x26、拔掉外接屏后的 `x: -21333` 全都被
    原样写进文件；读端 `normalize` 见到非法值**整对丢弃** → 回默认尺寸 → 关窗又把同一批脏值
    写回去。用户看到的是"窗口大小每夜静默失效"，日志里一句都没有。所以写端和读端用**同一个**
    判定函数，落盘的值一定是读端认可的：

    - 宽高 < 最小尺寸 / 超出屏幕 → 由 `normalize` 收成默认尺寸或屏内尺寸；
    - x/y 落在屏外 → 位置整对不写（只记尺寸，位置交给系统居中）；
    - **最小化/隐藏不写**：宽高为 0 或负、或根本不是整数，不是用户选出来的几何，
      这时不碰文件，直接返回文件里现有的（已校验）几何；
    - **几何没变不写**：与文件里现有的值一致就跳过（重写会丢注释），比较的是文件里的
      **原始**值 —— 所以文件里躺着脏值时这里会判成"变了"，顺手把它修正掉。

    写失败会抛异常，由调用方决定只记日志 —— 关窗路径绝不能因为存不了几何而卡住。
    """
    if _positive_int(width) is None or _positive_int(height) is None:
        # 最小化/隐藏/脏值：不写。返回盘上现有的几何（调用方据此也能看出"没动"）。
        return load(path, screens=screens)
    raw: dict[str, Any] = {"width": width, "height": height, "maximized": maximized is True}
    if x is not None and y is not None:
        raw["x"], raw["y"] = x, y
    payload = normalize(raw, screens=screens)
    target = config_path(path)
    current = _read_dict(target)
    section = current.get("ui")
    section = dict(section) if isinstance(section, dict) else {}
    stored = section.get("window")
    if isinstance(stored, dict) and same(stored, payload):
        # 与盘上的**原值**一致才跳过：盘上是脏值时不能算"没变"（那正是要修的东西）
        return payload
    section["window"] = payload
    current["ui"] = section
    _atomic_write(target, current)
    return payload


def clear(path: Path | None = None) -> None:
    """恢复默认：把 `ui.window` 置空（下次启动按默认尺寸开窗）。"""
    target = config_path(path)
    raw = _read_dict(target)
    section = raw.get("ui")
    section = dict(section) if isinstance(section, dict) else {}
    section["window"] = None
    raw["ui"] = section
    _atomic_write(target, raw)


def _read_dict(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """先写 `.tmp` 再 `os.replace`：崩在中间也不能把配置截断成半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _fit(size: tuple[int, int], screens: list[tuple[int, int, int, int]] | None) -> tuple[int, int]:
    limit = _screen_size(screens)
    if limit is None:
        return size
    return max(1, min(size[0], limit[0])), max(1, min(size[1], limit[1]))


def _screen_size(
    screens: list[tuple[int, int, int, int]] | None,
) -> tuple[int, int] | None:
    """可用尺寸上限 = **最大那块屏**（窗口可以开在那一块上）。拿不到屏幕信息就返回 None。"""
    if not screens:
        return None
    best = max(screens, key=lambda rect: int(rect[2]) * int(rect[3]))
    return int(best[2]), int(best[3])


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    return None


def _position(
    raw_x: object,
    raw_y: object,
    screens: list[tuple[int, int, int, int]] | None,
) -> tuple[int, int] | None:
    """位置校验：必须成对、是整数、落在某块屏上；落在屏内但贴边时夹回屏内。"""
    # 位置允许 0 与负数（左侧副屏的 x 就是负的）—— 不能用 `_positive_int`
    x = _any_int(raw_x)
    y = _any_int(raw_y)
    if x is None or y is None:
        return None
    if not screens:
        return x, y
    for sx, sy, sw, sh in screens:
        sx, sy, sw, sh = int(sx), int(sy), int(sw), int(sh)
        if (
            sx - POSITION_MARGIN <= x <= sx + sw - POSITION_MARGIN
            and sy - POSITION_MARGIN <= y <= sy + sh - POSITION_MARGIN
        ):
            # 夹回屏内：标题行与左缘必须抓得住（贴边负坐标会把标题栏推到屏外）
            clamped_x = min(max(x, sx), max(sx, sx + sw - KEEP_VISIBLE))
            clamped_y = min(max(y, sy), max(sy, sy + sh - KEEP_VISIBLE))
            return clamped_x, clamped_y
    return None


def _any_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    return int(value) if isinstance(value, int) else None
