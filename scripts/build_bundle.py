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

- **真 `.env`**（含 API 密钥）：默认只拷 `.env.example` 模板。要连自己机器上的真 `.env`
  一起拷，必须显式加 `--with-secrets`，而且打完的**自检会直接失败**（见下）——
  这条不是多余的：上一轮用 `--with-config` 把真密钥打进了 `ScholarScout-Portable/`，
  这种包发给任何人就是泄密，而它在目录里看起来和别的东西一模一样。
- `data/`、`run/`（用户的资料与账本）。首次运行会在 exe 旁边自动建出来。

## 打完自检（`assert_bundle_no_secrets`）

产物里只要有 `.env` 且其中**任一值非空** → 打印键名（**不打印值**）并以非 0 退出。
所以"带真密钥的包"不可能被这个脚本静默产出来；`.env.example`（值全空）不触发。

用法：
    python scripts/build_bundle.py                # → ScholarScout-Portable/（只带 .env.example）
    python scripts/build_bundle.py --with-config  # 再带上 config.yaml（**不含密钥**）
    python scripts/build_bundle.py --with-secrets # 再带上真 .env —— 会被自检拦下，仅排障用
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


def leaked_env_keys(env_path: Path) -> list[str]:
    """`.env` 里**值非空**的键名（只回键名，**绝不回值** —— 日志会进聊天记录与工单）。"""
    keys: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):  # shell 风格的 .env 也能读
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if value.strip().strip("'\""):
            keys.append(key.strip())
    return keys


def assert_bundle_no_secrets(target: Path) -> None:
    """打包后自检：产物里不许有**带非空值**的 `.env`（有就非 0 退出）。

    为什么是"打完再查"而不是"拷之前判断"：拷贝路径有三条（`--with-config` 的历史行为、
    `--with-secrets`、将来别的地方顺手 `copy2`），只盯住其中一条迟早漏；查产物目录才是
    对着"要送出去的那个东西"验收。`.env.example` 值全空，不触发。
    """
    env = target / ".env"
    if not env.is_file():
        return
    keys = leaked_env_keys(env)
    if keys:
        raise SystemExit(
            f"打包自检失败：{env} 里有非空值（{', '.join(keys)}）—— 分发件不能带真密钥。\n"
            f"  处理：删掉 {env}（模板 .env.example 已随包），或去掉 --with-secrets 重新打包。"
        )
    print("     自检：包内 .env 没有非空值（只有模板/空值）")


def stage_files(target: Path, *, with_config: bool, with_secrets: bool) -> None:
    """把「exe 旁边该有的东西」放进产物目录（提示词、说明文件、配置模板）。

    分级很清楚：模板永远带；`config.yaml` 要 `--with-config`；**真 `.env` 要 `--with-secrets`**。
    """
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
        print("     带上本机的 config.yaml（不含密钥，但可能含本地路径/模型名；默认不带）…")
        source = ROOT / "config.yaml"
        if source.exists():
            shutil.copy2(source, target / "config.yaml")

    if with_secrets:
        print("     " + "!" * 62)
        print("     !! --with-secrets：正在把真 .env（含 API 密钥）拷进包！")
        print("     !! 这个包**只能自己用**；发给任何人之前必须删掉包里的 .env。")
        print("     " + "!" * 62)
        source = ROOT / ".env"
        if source.exists():
            shutil.copy2(source, target / ".env")
        else:
            print("     （项目根没有 .env，没东西可拷）")


def build(name: str, dist_dir: Path, *, with_config: bool, with_secrets: bool = False) -> Path:
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

    print("[2/3] 放上「旁边要有的东西」（提示词、说明文件、配置模板）…")
    stage_files(target, with_config=with_config, with_secrets=with_secrets)
    # 自检放在拷完之后：验收的是"要送出去的那个目录"，不是"我以为拷了什么"
    assert_bundle_no_secrets(target)

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
    parser.add_argument(
        "--with-config", action="store_true", help="连 config.yaml 一起拷（不含密钥）"
    )
    parser.add_argument(
        "--with-secrets",
        action="store_true",
        help="连真 .env（含 API 密钥）一起拷；打完自检会失败，仅自己排障用",
    )
    args = parser.parse_args()
    build(
        args.name,
        Path(args.dist_dir),
        with_config=args.with_config,
        with_secrets=args.with_secrets,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
