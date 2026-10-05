"""把 ScholarScout 启动器编译成 .exe（用 Windows 自带的 csc.exe，**不装任何新依赖**）。

## 为什么不是 PyInstaller

把整个应用打包进 exe，意味着**每改一次代码就要重新打包** —— 那正是"会因为版本更替失效"。
这里的 exe 只是个**启动器**：找项目根 → 找 `.venv` 的解释器 → 拉起 `scripts/scout.py --desktop`。
业务代码全部留在项目里，怎么更新都不用重编这个 exe。

## 为什么不装编译器

Windows 自带 .NET Framework 的 `csc.exe`（v4.0.30319）就够编一个纯 Win32 调用 + Process 的
小程序；不需要 MSVC / MinGW / pip install pyinstaller（规则：新增依赖要审批）。

用法：
    python scripts/build_exe.py            # → dist/ScholarScout.exe + 同目录的 launcher.ini
    python scripts/build_exe.py --diagnose # 顺便跑一次启动器自检
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "packaging" / "launcher" / "ScholarScoutLauncher.cs"
ICON = ROOT / "assets" / "icon.ico"
DIST = ROOT / "dist"
EXE_NAME = "ScholarScout.exe"
CONFIG_NAME = "ScholarScout.launcher.ini"

#: Windows 自带的 C# 编译器（按优先级试）
CSC_CANDIDATES = (
    Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"),
)


def find_csc() -> Path | None:
    for candidate in CSC_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def csc_command(csc: Path, *, source: Path, output: Path, icon: Path | None) -> list[str]:
    """编译参数。抽出来单独测：图标没嵌进去是"看起来没问题但实际不对"的那类错。

    - ``/target:winexe``：GUI 子系统 → 双击不闪黑框
    - ``/codepage:65001``：源码是 UTF-8（里面有中文提示语），不指定会按本地代码页猜错
    - ``/win32icon``：把多尺寸 .ico 嵌进去（资源管理器/任务栏认它）
    """
    cmd = [
        str(csc),
        "/nologo",
        "/optimize+",
        "/target:winexe",
        "/platform:anycpu",
        "/codepage:65001",
        "/utf8output",
        f"/out:{output}",
    ]
    if icon is not None and icon.is_file():
        cmd.append(f"/win32icon:{icon}")
    cmd.append(str(source))
    return cmd


def write_config(project_root: Path, target: Path) -> None:
    target.write_text(
        "# ScholarScout 启动器配置\n"
        "# project = 项目根目录（必须含 scripts/scout.py）。改了这一行就能把启动器放到任何地方。\n"
        "# 也可以删掉本文件，改用环境变量 SCHOLARSCOUT_HOME，或把 exe 放进项目目录。\n"
        f"project={project_root}\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="编译 ScholarScout 启动器 .exe")
    parser.add_argument("--diagnose", action="store_true", help="编完跑一次 --diagnose 自检")
    args = parser.parse_args(argv)

    csc = find_csc()
    if csc is None:
        print("[FAIL] 找不到 csc.exe（需要 .NET Framework 4.x）。")
        print("       它通常在 C:\\Windows\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe")
        return 1
    if not SOURCE.is_file():
        print(f"[FAIL] 找不到源码：{SOURCE}")
        return 1
    if not ICON.is_file():
        print(f"[提示] 没有 {ICON}，先跑 python scripts/make_icon.py（这次不带图标编译）")

    DIST.mkdir(parents=True, exist_ok=True)
    output = DIST / EXE_NAME
    cmd = csc_command(csc, source=SOURCE, output=output, icon=ICON)
    print("执行：" + " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not output.is_file():
        print("[FAIL] 编译失败：")
        print((result.stdout + result.stderr).strip())
        return 1

    write_config(ROOT, DIST / CONFIG_NAME)
    print(f"[OK] 写出 {output}（{output.stat().st_size // 1024} KB）")
    print(f"[OK] 写出 {DIST / CONFIG_NAME}（指向 {ROOT}）")
    print("     双击 exe 即可；排查用：ScholarScout.exe --diagnose")

    if args.diagnose:
        print("\n== 启动器自检 ==")
        probe = subprocess.run(
            [str(output), "--diagnose"],
            capture_output=True,
            text=True,
            # 启动器把输出固定成 UTF-8，但万一它没跑起来（比如缺 DLL），这行兜底别让解码再崩一次
            encoding="utf-8",
            errors="replace",
        )
        print((probe.stdout or "").strip() or (probe.stderr or "").strip())
        print(f"退出码={probe.returncode}")
        if probe.returncode != 0:
            return probe.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
