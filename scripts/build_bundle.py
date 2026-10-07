"""把 ScholarScout 打成**自包含 Windows 目录包**（PyInstaller）—— 拷到别的机器点开即用。

## 与 `scripts/build_exe.py` 的分工（两个都留着，不是替代）

| | 启动器（build_exe.py） | 自包含包（本脚本） |
|---|---|---|
| 依赖 | 本机 Python + `.venv` | 无（解释器与依赖全带上） |
| 拷到别的机器 | 跑不起来 | **能跑** |
| 改代码后 | 不用重打包 | **必须重新打包** |
| 用途 | 日常自用/开发 | 演示、交作业、给不装 Python 的人 |

启动器存在的理由正是"打包一次要重来一次"；自包含包存在的理由正是"别人的机器没有环境"。

## 打进去什么

- 代码 + 解释器 + 依赖（httpx / pyyaml / pywebview / pymupdf 等）；
- **界面资源**：`src/scholarscout/desktop/ui/*`（无构建前端，直接带上）；
- **提示词**：`prompts/*.md` 放到 exe 旁边 —— 提示词是"可改的资产"，放外面用户还能自己调
  （打包后 `paths.home()` 指向 exe 所在目录，`summary.prompts_dir` 默认就是 `<exe 旁>/prompts`）；
- `README.md`、`AGENTS.md`、`deps-allowlist.txt`（给人看"这是什么、依赖经过审批"）。

## 特意**不**打进去的

- `.env` 与 `config.yaml`（**密钥不该出现在分发件里**）。要连配置一起拷，显式加 `--with-config`。
- `data/`、`run/`（用户的资料与账本）。首次运行会在 exe 旁边自动建出来。

用法：
    python scripts/build_bundle.py                # → ScholarScout-Portable/
    python scripts/build_bundle.py --with-config  # 连 .env / config.yaml 一起拷（自己机器用）
    python scripts/build_bundle.py --name MyBuild --dist-dir D:/out
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "scout.py"
ICON = ROOT / "assets" / "icon.ico"
UI_DIR = ROOT / "src" / "scholarscout" / "desktop" / "ui"
MIGRATIONS_DIR = ROOT / "src" / "scholarscout" / "state" / "migrations"

#: 打包后要**放在 exe 旁边**的东西（相对项目根 -> 目标文件名）
SIDE_FILES: tuple[str, ...] = (
    "README.md",
    "AGENTS.md",
    "deps-allowlist.txt",
    ".env.example",  # 别人拿到包时照着填密钥（**不含真密钥**）
)


def _pyinstaller_args(name: str, dist_dir: Path, work_dir: Path, spec_dir: Path) -> list[str]:
    args = [
        str(ENTRY),
        "--noconfirm",
        "--clean",
        "--windowed",  # 桌面应用：不要黑框控制台
        "--name",
        name,
        "--paths",
        str(ROOT / "src"),
        # 界面资源跟着包走：代码里按 `desktop/ui` 相对包目录找，所以目标路径要对上
        "--add-data",
        f"{UI_DIR}{os.pathsep}scholarscout/desktop/ui",
        # 迁移脚本（*.sql）**必须显式打进去**：PyInstaller 默认只收 .py，
        # 漏了它的表现是"点开没窗口"（启动时建库就崩，而 windowed 应用连报错都看不见）
        "--add-data",
        f"{MIGRATIONS_DIR}{os.pathsep}scholarscout/state/migrations",
        # pywebview 的后端是运行时按平台选的，显式收全，免得漏掉 edgechromium 平台模块
        "--collect-submodules",
        "webview",
        "--hidden-import",
        "webview.platforms.edgechromium",
        "--hidden-import",
        "clr_loader",
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
    ]
    if ICON.exists():
        args += ["--icon", str(ICON)]
    return args


def build(name: str, dist_dir: Path, *, with_config: bool) -> Path:
    work_dir = ROOT / "build" / "bundle"
    spec_dir = ROOT / "build" / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    # 先清掉上次的产物目录，否则 --noconfirm 之外还可能有残留旧文件混在里面
    target = dist_dir / name
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        *_pyinstaller_args(name, dist_dir, work_dir, spec_dir),
    ]
    print("[1/3] 打包（PyInstaller，首次约 1-3 分钟）…")
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {result.returncode}")
    exe = target / f"{name}.exe"
    if not exe.exists():
        raise SystemExit(f"没找到产物 {exe}")

    # 入口要放在最显眼处：把 PyInstaller 多套的那层同名子目录抹平，
    # 于是产物就是 `ScholarScout-Portable/ScholarScout.exe`（少一层，双击更好找）
    if target != dist_dir:
        for child in list(target.iterdir()):
            shutil.move(str(child), str(dist_dir / child.name))
        target.rmdir()
        target = dist_dir
        exe = target / f"{name}.exe"

    print("[2/3] 放上「旁边要有的东西」（提示词、说明文件）…")
    prompts_src = ROOT / "prompts"
    if prompts_src.is_dir():
        shutil.copytree(prompts_src, target / "prompts", dirs_exist_ok=True)
    for name_only in SIDE_FILES:
        source = ROOT / name_only
        if source.exists():
            shutil.copy2(source, target / name_only)
    example = ROOT / "config.example.yaml"
    if example.exists() and not (target / "config.yaml").exists():
        shutil.copy2(example, target / "config.yaml")

    if with_config:
        print("     连 .env 与 config.yaml 一起拷（**含密钥**，只有自己机器才这么用）…")
        for name_only in (".env", "config.yaml"):
            source = ROOT / name_only
            if source.exists():
                shutil.copy2(source, target / name_only)

    print("[3/3] 完成")
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"     目录：{target}")
    print(f"     入口：{exe}")
    print(f"     体积：{size / 1024 / 1024:.1f} MB")
    if not with_config:
        print("     下一步：把 LLM 密钥写进该目录的 .env（模板见 .env.example）")
    return exe


def main() -> int:
    parser = argparse.ArgumentParser(description="打自包含 Windows 包（PyInstaller）")
    parser.add_argument("--name", default="ScholarScout", help="产物名（默认 ScholarScout）")
    parser.add_argument(
        "--dist-dir",
        default=str(ROOT / "ScholarScout-Portable"),
        help="产物目录（默认 项目根/ScholarScout-Portable，最外层最顺手）",
    )
    parser.add_argument("--with-config", action="store_true", help="连 .env / config.yaml 一起拷")
    args = parser.parse_args()
    build(args.name, Path(args.dist_dir), with_config=args.with_config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
