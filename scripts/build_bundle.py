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
- **技能库**：仓库 `skills/` 拷到 exe 旁边的 `skills/`（同上：`paths.skills_dir()` 冻结后指向
  exe 所在目录）。不放进去的表现是 `/` 技能菜单在发布包里**是死的** —— 索引扫不到任何 `SKILL.md`；
- `README.md`、`AGENTS.md`、`deps-allowlist.txt`、`QUICKSTART.md`、`config.example.yaml`
  （给人看"这是什么、依赖经过审批、怎么跑起来、配置长什么样"）。

## 特意**不**打进去的

- **真 `.env`**（含 API 密钥）：默认只拷 `.env.example` 模板。要连自己机器上的真 `.env`
  一起拷，必须显式加 `--with-secrets`，而且打完的**自检会直接失败**（见下）——
  这条不是多余的：上一轮用 `--with-config` 把真密钥打进了 `ScholarScout-Portable/`，
  这种包发给任何人就是泄密，而它在目录里看起来和别的东西一模一样。
- **`config.yaml`**：它是**本机运行状态**，不是模板。`load_config()` 读不到文件就全用默认值
  （`config.py`：`if target.is_file(): … else: raw = {}`），设置面板改任何一项会由
  `settings_io._atomic_write_yaml()` 在 exe 旁边自动建出来 —— 所以分发包里**不需要**它。
  这也是 `--with-config` 被删掉的原因：那个开关唯一的用途就是把这台机器的模型选择/窗口几何/
  飞书配对用户（`feishu.paired_users` 的值就是 open_id）一起发出去。
- `data/`、`run/`（用户的资料与账本）。首次运行会在 exe 旁边自动建出来。

## 打完自检（`assert_bundle_no_secrets`）

对着**产物目录**验三件事，任一不过就打印具体原因并以非 0 退出：

1. 包内 `.env` 若有**任一非空值** → 打印键名（**不打印值**）；
2. 包内 `.env.example` 的**每个键都必须是空值**（模板漏填 = 把某台机器的配置发出去）；
3. 包内 `config.yaml` 若存在，必须与 `config.example.yaml` **逐键同值**（只许差注释/顺序）——
   运行期状态键（`feishu.paired_users`、`ui.window`、`llm.chat.available_models` …）一旦漂出来就拦下。

所以"带真密钥/带本机配置的包"不可能被这个脚本静默产出来。

用法：
    python scripts/build_bundle.py                # → ScholarScout-Portable/（.env.example + 模板 + skills/）
    python scripts/build_bundle.py --with-secrets # 再带上真 .env —— 会被自检拦下，仅排障用
    python scripts/build_bundle.py --name MyBuild --dist-dir D:/out
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "scout.py"
ICON = ROOT / "assets" / "icon.ico"
UI_DIR = ROOT / "src" / "scholarscout" / "desktop" / "ui"
MIGRATIONS_DIR = ROOT / "src" / "scholarscout" / "state" / "migrations"

#: 打包 `skills/` 时跳过的名字 —— 与仓库 `.gitignore` 的 `skills/.*` + `__pycache__/` **同一份口径**
#: （`skills/index.py` 也按"路径里任一段以点开头就跳过"扫目录：三处一致，包里的技能库就是入库的那份）。
SKILL_EXCLUDED_NAMES: frozenset[str] = frozenset(
    {".archive", ".hub", ".curator_backups", "__pycache__"}
)
SKILL_FILE = "SKILL.md"
CONFIG_TEMPLATE = "config.example.yaml"
QUICKSTART_NAME = "QUICKSTART.md"

#: 打包后要**放在 exe 旁边**的东西（相对项目根 -> 目标文件名）
SIDE_FILES: tuple[str, ...] = (
    "README.md",
    "AGENTS.md",
    "deps-allowlist.txt",
    ".env.example",  # 别人拿到包时照着填密钥（**不含真密钥**）
    CONFIG_TEMPLATE,  # 只是模板；应用读不到 config.yaml 时全用默认值，不用它也能跑
)

#: 版本标记文件名：写在 exe 旁边的这一行文字，是打包版 `--version` 的唯一来源。
#: 为什么不在包里再抄一遍版本号：`scholarscout.__version__` 现在从 `pyproject.toml` 读
#: （源码树），冻结后源码树不在 —— 于是由本脚本在打包时把同一个值写成 `_VERSION`
#: 放在 exe 旁边，`__init__.py` 向上找得到它。三处（pyproject / 产物 / 界面）永远是同一个值。
VERSION_MARKER = "_VERSION"


def project_version() -> str:
    """读 `pyproject.toml` 的 `[project].version` —— 全项目唯一的事实源。"""
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = data["project"]["version"]
    if not isinstance(version, str) or not version.strip():
        raise SystemExit("pyproject.toml 的 [project].version 是空的 —— 先修版本号再打包")
    return version.strip()


def stage_version_marker(target: Path) -> str:
    """把版本号写进产物目录的 `_VERSION`（打包版 `--version` 读它）。"""
    version = project_version()
    (target / VERSION_MARKER).write_text(f"{version}\n", encoding="utf-8")
    print(f"     {VERSION_MARKER}：{version}（取自 pyproject.toml）")
    return version


QUICKSTART_TEXT = """\
# ScholarScout 便携包 —— 三步跑起来

1. **填密钥**：把 API Key 写进本目录的 `.env`（模板见 `.env.example`；密钥只放这一处，不进 config.yaml）。
2. **改配置（可选，不复制也能跑）**：本包**不带** `config.yaml`。启动时读不到它，全部用内置默认值；
   之后在设置面板里改任何一项，会自动在本目录生成 `config.yaml`。
   想对着注释逐条改，就先复制模板：`copy config.example.yaml config.yaml`。
3. **双击** `{exe}`（界面资源、提示词、`skills/` 都在这个目录里，整个目录一起拷走即可）。

## 关于本机数据

`data/`（抓到的资料）、`run/`（库与账本）、`config.yaml`（本机设置）、`.env`（密钥）
都是**运行时自己建**的，属于本机数据：备份要整个目录一起拷，分发给别人前请删掉它们。

## 关于 skills/

`skills/` 就在 exe 旁边，**可以自己增删**：放一个带 frontmatter 的 `SKILL.md` 进去，
`/` 技能菜单下次启动就能看到。删掉 `skills/` 也不影响主流程，只是技能菜单会空。
"""


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


def _flatten(node: object, prefix: str = "") -> dict[str, object]:
    """把 YAML 树摊成 `a.b.c -> 叶子值`，用来**逐键**比对（注释与顺序自然被忽略）。"""
    if isinstance(node, dict):
        flat: dict[str, object] = {}
        for key, value in node.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: node}


_MISSING = object()


def config_drift(target: Path) -> list[str]:
    """包内 `config.yaml` 与 `config.example.yaml` 的**键路径**差异（空列表 = 只差注释/顺序）。

    只回键路径、**不回值**：`feishu.paired_users` / `feishu.*open_id` 这类键的值本身就是
    用户的 open_id，打进日志等于把要藏的东西写出来。
    """
    cfg = target / "config.yaml"
    if not cfg.is_file():
        return []
    template = target / CONFIG_TEMPLATE
    if not template.is_file():
        template = ROOT / CONFIG_TEMPLATE
    if not template.is_file():
        return ["<找不到 config.example.yaml，无法核对>"]
    try:
        shipped = yaml.safe_load(cfg.read_text(encoding="utf-8", errors="replace"))
        expected = yaml.safe_load(template.read_text(encoding="utf-8", errors="replace"))
    except yaml.YAMLError:
        return ["<config.yaml 不是合法 YAML>"]
    if shipped is None:
        shipped = {}
    if expected is None:
        expected = {}
    if not isinstance(shipped, dict) or not isinstance(expected, dict):
        return ["<config.yaml 或 config.example.yaml 的顶层不是映射>"]
    here = _flatten(shipped)
    there = _flatten(expected)
    return sorted(
        key for key in set(here) | set(there) if here.get(key, _MISSING) != there.get(key, _MISSING)
    )


def assert_bundle_no_secrets(target: Path) -> None:
    """打包后自检：产物里不许有真密钥、不许有本机 `config.yaml`（任一违反就非 0 退出）。

    为什么是"打完再查"而不是"拷之前判断"：拷贝路径有好几条（`--with-secrets`、将来的顺手
    `copy2`、手工往目录里放文件），只盯住其中一条迟早漏；查产物目录才是对着"要送出去的那个
    东西"验收。三项检查各自独立收集、最后一起报，免得"改了一个再发现另一个"来回打三次包。
    """
    failures: list[str] = []

    env = target / ".env"
    if env.is_file():
        keys = leaked_env_keys(env)
        if keys:
            failures.append(
                f"包内 .env 有非空值（{', '.join(keys)}）—— 分发件不能带真密钥。\n"
                f"    处理：删掉 {env}（模板 .env.example 已随包），或去掉 --with-secrets 重新打包。"
            )

    example = target / ".env.example"
    if example.is_file():
        filled = leaked_env_keys(example)
        if filled:
            failures.append(
                f"包内 .env.example 有非空值（{', '.join(filled)}）—— 模板每个键都必须是空值。\n"
                "    真值只该进 .env；模板漏填空值，等于把某台机器的配置一起发出去。"
            )

    drift = config_drift(target)
    if drift:
        failures.append(
            f"包内 config.yaml 与 {CONFIG_TEMPLATE} 不一致（{'、'.join(drift)}）。\n"
            "    这些多是应用**运行时自己写回**的状态（模型选择、窗口几何、飞书配对用户…），\n"
            "    属于打包这台机器的本机状态，分发件只许带模板。\n"
            f"    处理：删掉 {target / 'config.yaml'}，保留 {CONFIG_TEMPLATE}；"
            "应用读不到 config.yaml 会全用默认值，不用它也能跑。"
        )

    if failures:
        raise SystemExit("打包自检失败：\n  - " + "\n  - ".join(failures))
    print("     自检：.env 无真密钥、.env.example 值全空、包内无本机 config.yaml")


def _warn_about_user_state(target: Path) -> None:
    """提示产物目录里混着**上一次运行留下的本机状态**（不是这一版打进去的）。

    默认 `--dist-dir` 就是 `ScholarScout-Portable/`，双击跑过一次就会在里面长出
    `run/`、`data/`、`.env`、`config.yaml`。它们留着不删（那是用户的东西），但**发版**
    不能带着走 —— `python scripts/release.py` 用 `--clean` 在空目录里组装，天然干净。
    """
    junk = [name for name in ("run", "data", ".env", "config.yaml") if (target / name).exists()]
    junk += sorted(path.name for path in target.glob("*.db")) + sorted(
        path.name for path in target.glob("*.log")
    )
    if junk:
        print(
            f"     ! 注意：{target.name}/ 里还有上次运行的 {'、'.join(junk)}（没被拷进包，但发版别用它）；\n"
            "       发版用 `python scripts/release.py`（空目录组装，不带你本机的数据与密钥）。"
        )


def _skill_copy_ignore(directory: str, names: list[str]) -> set[str]:
    """`copytree` 的排除名单：点号条目（`.archive`/`.hub`/`.curator_*`）+ `__pycache__`。"""
    del directory
    return {name for name in names if name.startswith(".") or name in SKILL_EXCLUDED_NAMES}


def skills_source() -> Path:
    """仓库技能库的位置。**用函数而不是常量**：`ROOT` 在测试里会被指到假项目，
    常量在 import 时就定死了（第一版就是这么漏的：假项目里的 `skills/` 根本没被读到）。"""
    return ROOT / "skills"


def count_skill_files(target: Path) -> int:
    """包里 `skills/` 下有多少篇 `SKILL.md`（`/` 技能菜单能不能用的唯一判据）。"""
    skills = target / "skills"
    return sum(1 for _ in skills.rglob(SKILL_FILE)) if skills.is_dir() else 0


def stage_skills(target: Path) -> int:
    """把仓库 `skills/` 拷到 **exe 旁边**的 `skills/`，返回拷进去的 `SKILL.md` 篇数。

    放 exe 同级（而不是塞进 PyInstaller 包内）有两个理由：`paths.skills_dir()` 冻结后指向
    exe 所在目录；技能是"可改的资产"，用户在包里自己增删一篇就能用（和提示词同一个道理）。
    一篇都没拷进去 = 发布包的技能菜单是死的，所以这里直接失败，不留给用户发现。
    """
    source = skills_source()
    if not source.is_dir():
        raise SystemExit(f"仓库里没有 {source} —— 发布包的 `/` 技能菜单会是空的，先确认技能库还在")
    shutil.copytree(source, target / "skills", dirs_exist_ok=True, ignore=_skill_copy_ignore)
    count = count_skill_files(target)
    if count == 0:
        raise SystemExit(
            f"拷完 skills/ 一篇 {SKILL_FILE} 都没有（排除名单把内容全滤掉了？）—— 发布包不能这么发"
        )
    print(f"     skills/：{count} 篇 {SKILL_FILE}（已排除点号目录与 __pycache__）")
    return count


def _write_quickstart(target: Path) -> None:
    """写 `QUICKSTART.md`：怎么填密钥、为什么包里没有 `config.yaml`、`skills/` 可以自己改。"""
    exes = sorted(path.name for path in target.glob("*.exe"))
    text = QUICKSTART_TEXT.replace("{exe}", exes[0] if exes else "ScholarScout.exe")
    (target / QUICKSTART_NAME).write_text(text, encoding="utf-8")


def stage_files(target: Path, *, with_secrets: bool) -> None:
    """把「exe 旁边该有的东西」放进产物目录（提示词、技能库、说明文件、配置模板）。

    分级很清楚：提示词/技能/说明/`config.example.yaml` **永远带**；真 `.env` 要
    `--with-secrets`；`config.yaml` **永远不带**（它是本机状态，由应用自己生成）。
    """
    prompts_src = ROOT / "prompts"
    if prompts_src.is_dir():
        shutil.copytree(prompts_src, target / "prompts", dirs_exist_ok=True)
    for name_only in SIDE_FILES:
        source = ROOT / name_only
        if source.exists():
            shutil.copy2(source, target / name_only)
    stage_skills(target)
    _write_quickstart(target)
    stage_version_marker(target)

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


def install_built(built: Path, target: Path, *, clean: bool = False) -> None:
    """把 PyInstaller 的产出铺进 `target`（**不**多套一层同名子目录，双击更好找）。

    为什么要显式覆盖而不是直接 `mv built/* target/`：`--dist-dir` 默认就指向**最终目录本身**，
    上一版的"抹平那层子目录"于是往已经存在的 `_internal/` 里又挪进一个 `_internal/`
    （真机表现：`ScholarScout-Portable/_internal/_internal` + 一个残留的 `ScholarScout/`，
    第二次打包直接 `shutil.Error: Destination path already exists`）。
    现在只覆盖**本包自己的产出**（exe 与 `_internal/`），用户放在目录里的
    `.env` / `data/` / 自己的 `skills/` 不动 —— 除非 `clean=True`：

    `clean=True` 用于**发版**（`scripts/release.py`）：整目录换成刚组装好的那一份。
    这条是 v0.1.1 踩出来的：旧产物目录里上一次运行留下的 `run/logs/scout.log` 被原样
    留在里面，发出去的 zip 就带着别人的运行日志。清理必须发生在**组装**这一侧，
    而不是"发版前手工删一遍"。
    """
    if clean:
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(built), str(target))
        return
    target.mkdir(parents=True, exist_ok=True)
    # 旧版"抹平"逻辑留下的同名子目录：扁平包里它本不该存在，顺手清掉（否则白白多占一倍体积）
    stale = target / built.name
    if stale.is_dir():
        shutil.rmtree(stale)
    for child in list(built.iterdir()):
        dest = target / child.name
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
        shutil.move(str(child), str(dest))


def build(name: str, dist_dir: Path, *, with_secrets: bool = False, clean: bool = False) -> Path:
    work_dir = ROOT / "build" / "bundle"
    spec_dir = ROOT / "build" / "spec"
    staging = work_dir / "dist"
    spec_dir.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        *_pyinstaller_args(name, staging, work_dir, spec_dir),
    ]
    print("[1/4] 打包（PyInstaller，首次约 1-3 分钟）…")
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {result.returncode}")
    built = staging / name
    exe = built / f"{name}.exe"
    if not exe.exists():
        raise SystemExit(f"没找到产物 {exe}")

    # **先在 PyInstaller 的空目录里把整包组装好，再整目录搬进 dist_dir**。
    # 顺序反了就会出 v0.1.1 那个事故：往已有目录里覆盖时，上一轮运行留下的 `run/`、
    # `data/`、`.env`、`config.yaml` 原样留在里面，自检报"包不干净"——而"干净"该由
    # 组装过程保证，不是靠发版前手工删一遍（忘了删就会发出去）。
    print("[2/4] 放上「旁边要有的东西」（提示词、技能库、说明文件、配置模板、_VERSION）…")
    stage_files(built, with_secrets=with_secrets)
    # 自检放在搬动之前：验收的是"要送出去的那个目录"，不是"我以为拷了什么"
    assert_bundle_no_secrets(built)

    # PyInstaller 的 onedir 产出永远多一层 `<name>/`：抹平它，产物就是
    # `ScholarScout-Portable/ScholarScout.exe`；同时保证重复打包不会越套越深
    print("[3/4] 铺进产物目录…")
    target = dist_dir
    install_built(built, target, clean=clean)
    shutil.rmtree(staging, ignore_errors=True)
    exe = target / f"{name}.exe"
    if not exe.exists():
        raise SystemExit(f"铺进 {target} 之后没找到 {exe}")
    if not clean:
        _warn_about_user_state(target)

    print("[4/4] 完成")
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"     目录：{target}")
    print(f"     入口：{exe}")
    print(f"     体积：{size / 1024 / 1024:.1f} MB")
    print(
        "     下一步：把 LLM 密钥写进该目录的 .env（模板见 .env.example）；"
        f"要改配置就 copy {CONFIG_TEMPLATE} config.yaml"
    )
    return exe


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打自包含 Windows 包（PyInstaller）")
    parser.add_argument("--name", default="ScholarScout", help="产物名（默认 ScholarScout）")
    parser.add_argument(
        "--dist-dir",
        default=str(ROOT / "ScholarScout-Portable"),
        help="产物目录（默认 项目根/ScholarScout-Portable，最外层最顺手）",
    )
    parser.add_argument(
        "--with-secrets",
        action="store_true",
        help="连真 .env（含 API 密钥）一起拷；打完自检会失败，仅自己排障用",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="先把产物目录整个删掉（发版用：目录里上次运行留下的 run/、data/、.env 不会混进包）",
    )
    args = parser.parse_args(None if argv is None else list(argv))
    build(
        args.name,
        Path(args.dist_dir),
        with_secrets=args.with_secrets,
        clean=args.clean,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
