"""一键发版：构建自包含包 → 打 zip → 算 SHA256 → 落到 ``releases/v<版本>/``。

## 为什么要有它

v0.1.0 发完之后，产物被留在**仓库根目录**（`ScholarScout-Portable-v0.1.x.zip` + 旁边的
`.sha256`）：既没进 `.gitignore`（`git status` 永远多两行未跟踪），也没有归处 ——
下一版一覆盖，上一版的哈希与说明就再也找不出来（v0.1.0 的包就是这么丢的，只剩
`RELEASE_NOTES-v0.1.0.md` 里那串哈希）。所以产物按**版本**落盘，一个版本一个目录，
目录里四件套固定：

```
releases/
  v0.1.1/
    ScholarScout-Portable-v0.1.1.zip   产物本体
    SHA256SUMS                         校验（`sha256sum -c SHA256SUMS` 直接能用）
    RELEASE_NOTES.md                   这一版发什么（「本版变更」正文取自 CHANGELOG.md）
    MANIFEST.json                      机器可读：版本 / commit / 构建时间 / 字节 / 哈希 / 技能数
  v0.1.0/
    MISSING.md                         没有本地副本时**如实写缺失**，不伪造哈希与体积
```

`releases/` 与 `*.zip` 都在 `.gitignore` 里：**产物不进 git**，只在本机落盘；
对外分发走 GitHub Releases。给人看的「哪个版本在哪、旧版本怎么找」记在
`docs/29-发布产物管理.md`（含台账），不放这里 —— 那些要随仓库走。

## 版本号从哪来

只从 `pyproject.toml` 的 `[project].version` 读，**不接受命令行传入**。包里的
`scholarscout.__version__`、exe 旁边的 `_VERSION`、界面「关于」、这个脚本 —— 全部是
同一个值的下游，写死第二个版本号就又回到"发版时改一处忘一处"的老路。

## 用法

    python scripts/release.py                 # 全流程（构建约 1-3 分钟）
    python scripts/release.py --skip-build    # 复用已有构建目录（只重打 zip 与哈希）
    python scripts/release.py --force         # 该版本目录已有产物时覆盖重打

每次都在**空目录**里组装（默认 `build/release/v<版本>/bundle`，先整个删掉；转调
`build_bundle.py --clean`），所以 zip 的来源里不可能有上一轮运行留下的 `run/`、`data/`、
`.env`、`config.yaml`、`*.db`、`*.log`。项目根的 `ScholarScout-Portable/`（日常跑的那份，
里面可能有你自己的资料）发版**不碰**。

依赖：只用标准库（本机没有 `zip` 命令，压缩走 `zipfile`）+ 项目已有的 PyInstaller。

退出码：0 = 完成；非 0 = 任一步失败（构建失败 / 产物带密钥或数据 / exe 版号对不上 /
CHANGELOG 缺本节 / 已存在同名产物且没给 `--force`）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
BUNDLE_SCRIPT = ROOT / "scripts" / "build_bundle.py"
RELEASES_DIR = ROOT / "releases"
#: 发版用的构建目录：**每个版本一个，每次先删干净**。为什么不复用项目根的
#: `ScholarScout-Portable/`：那是日常跑的目录，双击过一次就会在里面长出 `run/`、
#: `data/`、`.env`、`config.yaml` —— v0.1.1 第一次发版就是被这些东西挡在自检上
#: （zip 里带着 `run/logs/scout.log`）。把"干净"交给构建目录本身，而不是发版前的记忆。
BUILD_ROOT = ROOT / "build" / "release"
BUNDLE_ROOT_NAME = "ScholarScout-Portable"
EXE_NAME = "ScholarScout.exe"

#: 版本目录里的三件说明文件（**每个都要拼到目录下的文件名**：往目录上 `write_text`
#: 会得到 `PermissionError: [Errno 13] Permission denied: 'releases\v0.1.1'` —— v0.1.1
#: 第一次跑就是这么挂的，压在最后一步，前面 3 分钟的构建全白跑）。
SUMS_NAME = "SHA256SUMS"
NOTES_NAME = "RELEASE_NOTES.md"
MANIFEST_NAME = "MANIFEST.json"

REPO_URL = "https://github.com/shiliyunma/ScholarScout"

#: 包里**一律不许出现**的东西（分发包带这些 = 把某台机器的数据/密钥发出去）
FORBIDDEN_NAMES: frozenset[str] = frozenset({".env", "config.yaml"})
FORBIDDEN_SUFFIXES: tuple[str, ...] = (".db", ".db-wal", ".db-shm", ".log")
FORBIDDEN_DIRS: tuple[str, ...] = ("data", "run")

INSTALL_TEXT = """\
## 安装

### ① 桌面版（Windows，解压即用）

下载下面的 `ScholarScout-Portable-v{version}.zip`，解压到**不含中文、不含空格的路径**
（如 `D:\\apps\\ScholarScout`）后双击 `ScholarScout.exe`。
首次启动会生成 `config.yaml` 与 `.env.example`，复制成 `.env` 填 `LLM_API_KEY`，重开即用。

### ② 源码跑（Windows / macOS / Linux）

需要 Python 3.11 与 [uv](https://docs.astral.sh/uv/)：`uv venv --python 3.11` →
`scripts/install_deps.py` → `cp .env.example .env` 填 key。
再 `scout.py --initdb` 建库（幂等），最后 `scout.py --desktop` 开窗口，没有图形界面就用 `--chat`。
**不要 `pip install -e .`** —— 装了会连 `print(1)` 都跑不了，原因见 [QUICKSTART]({repo}/blob/main/QUICKSTART.md) 的 FAQ。

### ③ Docker（无界面：只有 CLI / JSON-RPC / 飞书机器人）

`cp .env.example .env` 填 key 后，`docker compose run --rm scout --initdb` 建库，
`--chat` 终端对话，`docker compose up -d` 后台常驻。数据落在命名卷 `scout-data` 里。
容器内没有 GUI 也没有 WebView2，桌面窗口只能在宿主机跑。
"""

LIMITS_TEXT = """\
## 已知限制

完整清单与逐条说明见 [CHANGELOG.md 的 Known Limitations 一节]({repo}/blob/main/CHANGELOG.md#known-limitations)，要点：

- **没有 JS 引擎** —— SPA / 动态渲染页抓不到正文，只拿得到空壳 HTML。
- **旧版 Office 格式需先转换** —— `.doc` / `.xls` / `.ppt` 不解析，请先另存为 OOXML。
- **单文件上限 20 MB** —— 超过即拒，不会先下完再算。
- **扫描件依赖 MinerU** —— 无文字层 PDF 走在线接口，免 token 接口按 IP 限频且可能整段不可用。
- **Docker 镜像无 GUI** —— 容器里只有 CLI / JSON-RPC / 飞书机器人。
- **微信 / QQ 未实现** —— 目前只有飞书一个渠道。
"""

TAIL_TEXT = """\
## 许可证

[MIT]({repo}/blob/main/LICENSE) © 2026 shiliyunma

## 反馈

- 提 Bug / 提需求：<{repo}/issues>
- 安全问题请**不要**公开提 issue —— 走仓库 Security 页的「Report a vulnerability」：
  <{repo}/security/advisories/new>

## 旧版本在哪找

本机只保留当前这一版的产物目录（`releases/<版本>/`，不进 git）。**历史版本一律在
GitHub Releases 页**：<{repo}/releases>。那边每个版本都挂着 zip 与它的 SHA256，
v0.1.0 只有那边有。本仓库的 `docs/29-发布产物管理.md` 有一张台账（版本 / 日期 / commit /
SHA256），用来对账"哪个版本是哪次提交打出来的"。
"""


def _fail(message: str) -> None:
    """出错就带着原因退出 —— 发版脚本不许"打个警告继续走"。"""
    raise SystemExit(f"发版失败：{message}")


def read_version() -> str:
    """版本号唯一事实源：`pyproject.toml` 的 `[project].version`。"""
    try:
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"读不了 {PYPROJECT}：{exc}")
    version = str(data.get("project", {}).get("version", "")).strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        _fail(f"pyproject.toml 的 version={version!r} 不是三段数字 —— 先修版本号")
    return version


def changelog_section(version: str) -> str:
    """取 `CHANGELOG.md` 里 `## [<version>]` 到下一个 `## ` 之间的正文。

    「本版变更」直接来自 CHANGELOG 而不是另写一份：同一件事写两处，迟早一处是真的、
    一处是旧的。取不到就失败 —— 宁可发不出去，也不拿上一版的说明凑。
    """
    text = CHANGELOG.read_text(encoding="utf-8")
    pattern = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)", re.M | re.S)
    match = pattern.search(text)
    if not match or not match.group(1).strip():
        _fail(
            f"{CHANGELOG.name} 里没有 `## [{version}]` 一节（或正文为空）——"
            " 先在 CHANGELOG 写清这一版改了什么，再发版"
        )
    return match.group(1).strip()


def git(*args: str) -> str:
    """跑一条 git 命令并返回去尾空白的 stdout（失败即退出）。"""
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        _fail(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


def build(bundle_dir: Path) -> None:
    """在**空目录**里构建自包含包（转调 `scripts/build_bundle.py --clean`）。"""
    if not BUNDLE_SCRIPT.is_file():
        _fail(f"找不到 {BUNDLE_SCRIPT}")
    if bundle_dir.exists():
        print(f"     清空构建目录（上次的产物与它留下的 run/、data/ 都不带走）：{bundle_dir}")
        shutil.rmtree(bundle_dir)
    print(f"[2/7] 构建自包含包 → {bundle_dir}（PyInstaller，首次约 1-3 分钟）…")
    result = subprocess.run(
        [sys.executable, str(BUNDLE_SCRIPT), "--dist-dir", str(bundle_dir), "--clean"],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        _fail(f"构建失败（scripts/build_bundle.py 退出码 {result.returncode}）")


def assert_bundle_clean(bundle_dir: Path) -> None:
    """产物自检：不许带密钥/数据库/日志，也不许带用户数据目录。

    对着**要发出去的那个目录**查，而不是"我以为拷了什么"—— 拷贝路径有好几条，
    只盯住其中一条迟早漏（这条与 `build_bundle.assert_bundle_no_secrets` 互补：
    那边查".env.example 全空 / config.yaml 不漂"，这边查"数据与日志一个都不许有"）。
    """
    problems: list[str] = []
    for path in bundle_dir.rglob("*"):
        rel = path.relative_to(bundle_dir).as_posix()
        if path.is_dir():
            if rel in FORBIDDEN_DIRS:
                problems.append(f"多出目录 {rel}/（用户数据不该进发布包）")
            continue
        if path.name in FORBIDDEN_NAMES:
            problems.append(f"多出文件 {rel}")
        elif path.name.endswith(FORBIDDEN_SUFFIXES):
            problems.append(f"多出文件 {rel}（数据库/日志）")
    if problems:
        _fail("发布包不干净：\n  - " + "\n  - ".join(problems))
    print("     自检：无 .env / config.yaml / *.db / *.log，无 data/ 与 run/")


def count_skills(bundle_dir: Path) -> int:
    skills = bundle_dir / "skills"
    return sum(1 for _ in skills.rglob("SKILL.md")) if skills.is_dir() else 0


def verify_exe(bundle_dir: Path, version: str) -> tuple[int, str]:
    """跑一次 exe 的 `--version`：打包版报的版本必须与 `pyproject` 一致。

    这条是"版本号单一事实源"的**真验证** —— 只改 pyproject 而不重打包，
    exe 里那份冻结的旧代码仍会报上一个版本，界面上完全看不出来。
    """
    exe = bundle_dir / EXE_NAME
    if not exe.is_file():
        _fail(f"产物里没有 {exe}")
    result = subprocess.run(
        [str(exe), "--version"],
        cwd=bundle_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    if result.returncode != 0:
        _fail(f"{EXE_NAME} --version 退出码 {result.returncode}：{output}")
    if version not in output:
        _fail(f"{EXE_NAME} --version 报的是 {output!r}，不含 {version} —— 产物与 pyproject 不一致")
    print(f"     自检：{EXE_NAME} --version → {output}")
    return exe.stat().st_size, output


def make_zip(bundle_dir: Path, out: Path, version: str) -> None:
    """把产物目录压成 zip，顶层套一层 `ScholarScout-Portable-v<版本>/`。

    为什么套一层：v0.1.0 的包是"平铺"的（673 个条目直接落在解压目录），双击 zip 解压
    会把 `_internal/`、`skills/` 撒进用户当前目录 —— 现在解压只会得到**一个**文件夹。
    """
    top = f"{BUNDLE_ROOT_NAME}-v{version}"
    files = sorted(p for p in bundle_dir.rglob("*") if p.is_file())
    if not files:
        _fail(f"{bundle_dir} 里一个文件都没有 —— 构建没跑成功？")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                archive.write(path, f"{top}/{path.relative_to(bundle_dir).as_posix()}")
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink()
    print(f"[4/7] 打 zip：{out.name}（{len(files)} 个文件，{out.stat().st_size:,} 字节）")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_notes(out_dir: Path, *, version: str, artifact: str, sha256: str, size: int) -> Path:
    """写 `RELEASE_NOTES.md`：开头是本版事实（commit/哈希/体积），正文取 CHANGELOG。"""
    changes = changelog_section(version)
    commit = git("rev-parse", "--short", "HEAD")
    body = f"""\
# ScholarScout v{version}

> 构建：{datetime.now().astimezone().isoformat(timespec="seconds")} ｜ commit `{commit}`

## 校验下载完整性

```text
文件：{artifact}
大小：{size:,} 字节（{size / 1024 / 1024:.1f} MB）
SHA256：{sha256}
```

下载后自行算一遍，值一致才解压运行 —— 不一致说明下载不完整或被篡改，请重新下载。

Windows（PowerShell）：`Get-FileHash .\\{artifact} -Algorithm SHA256`
macOS / Linux：`sha256sum {artifact}`

---

## 本版变更

{changes}

---

{INSTALL_TEXT.format(version=version, repo=REPO_URL)}
---

{LIMITS_TEXT.format(repo=REPO_URL)}
---

{TAIL_TEXT.format(repo=REPO_URL)}
"""
    target = out_dir / NOTES_NAME
    target.write_text(body, encoding="utf-8")
    return target


def write_manifest(
    out_dir: Path,
    *,
    version: str,
    artifact: str,
    sha256: str,
    size: int,
    exe_bytes: int,
    skill_count: int,
    exe_output: str,
) -> dict[str, object]:
    """写 `MANIFEST.json`：机器可读的这一版身份（版本 / commit / 时间 / 体积 / 哈希）。"""
    manifest: dict[str, object] = {
        "schema": 1,
        "name": "ScholarScout",
        "version": version,
        "tag": f"v{version}",
        "artifact": artifact,
        "sha256": sha256,
        "bytes": size,
        "exe_bytes": exe_bytes,
        "exe_version_output": exe_output,
        "skill_count": skill_count,
        "commit": git("rev-parse", "HEAD"),
        "commit_subject": git("log", "-1", "--pretty=%s"),
        "dirty": bool(git("status", "--porcelain")),
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "builder": "scripts/release.py",
        "python": sys.version.split()[0],
    }
    target = out_dir / MANIFEST_NAME
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="一键发版：构建 → zip → SHA256 → releases/v<版本>/"
    )
    parser.add_argument("--skip-build", action="store_true", help="复用已有构建目录，不重新构建")
    parser.add_argument("--force", action="store_true", help="该版本目录已有产物时覆盖")
    parser.add_argument(
        "--dist-dir",
        default=None,
        help="构建目录（默认 build/release/v<版本>/bundle，每次先整个删干净）",
    )
    args = parser.parse_args(argv)

    version = read_version()
    bundle_dir = Path(args.dist_dir) if args.dist_dir else BUILD_ROOT / f"v{version}" / "bundle"
    out_dir = RELEASES_DIR / f"v{version}"
    artifact = f"{BUNDLE_ROOT_NAME}-v{version}.zip"
    out_zip = out_dir / artifact

    print(f"[1/7] 版本 {version}（取自 pyproject.toml），产物落 releases/v{version}/")
    if out_zip.exists() and not args.force:
        _fail(f"{out_zip} 已存在 —— 确认要重打就加 --force（覆盖后旧 SHA256 作废）")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_build:
        if not (bundle_dir / EXE_NAME).is_file():
            _fail(f"--skip-build 但 {bundle_dir / EXE_NAME} 不存在，先跑一次完整发版")
        print("[2/7] 跳过构建（--skip-build），复用现有产物")
    else:
        build(bundle_dir)

    print("[3/7] 产物自检（干净度 → 技能数 → exe 版本）…")
    assert_bundle_clean(bundle_dir)
    skill_count = count_skills(bundle_dir)
    if skill_count == 0:
        _fail("产物里一篇 SKILL.md 都没有 —— 技能菜单会是死的，不发")
    print(f"     自检：skills/ {skill_count} 篇 SKILL.md")
    exe_bytes, exe_output = verify_exe(bundle_dir, version)

    make_zip(bundle_dir, out_zip, version)

    print("[5/7] 算 SHA256…")
    sha256 = sha256_of(out_zip)
    size = out_zip.stat().st_size
    (out_dir / SUMS_NAME).write_text(f"{sha256}  {artifact}\n", encoding="utf-8")
    print(f"     {sha256}  {artifact}")

    print("[6/7] 写 RELEASE_NOTES.md / MANIFEST.json…")
    write_notes(out_dir, version=version, artifact=artifact, sha256=sha256, size=size)
    manifest = write_manifest(
        out_dir,
        version=version,
        artifact=artifact,
        sha256=sha256,
        size=size,
        exe_bytes=exe_bytes,
        skill_count=skill_count,
        exe_output=exe_output,
    )

    print("[7/7] 完成")
    print(f"     目录：{out_dir}")
    for name in sorted(p.name for p in out_dir.iterdir() if p.is_file()):
        print(f"       - {name}")
    print(f"     commit={str(manifest['commit'])[:12]} dirty={manifest['dirty']}")
    print(f"     SHA256={sha256}")
    print("     下一步：把 zip 传到 GitHub Releases（tag 用 v" + version + "），")
    print("             并在 docs/29-发布产物管理.md 的台账里补一行（版本/日期/commit/SHA256）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
