"""打包脚本（`scripts/build_bundle.py`）的**分发安全边界**。

为什么值得测：打包是"一次性、跑完就发出去"的动作，它错了不会让任何东西变红 ——
只会让一个**装着真 API 密钥或本机配置的目录**看起来跟正常交付包一模一样。上一轮就是这么出的：
`--with-config` 把 `.env` 连密钥一起拷进了 `ScholarScout-Portable/`，同一次还带进了一份本机
`config.yaml`；更早的一次则把 `skills/` 整个漏掉（`/` 技能菜单在发布包里是死的，没人看得出来）。

这里用桩替掉 PyInstaller（不真打包），只验四件事：
1. 默认只带 `.env.example` 与 `config.example.yaml`，真 `.env` 与本机 `config.yaml` 都不进包；
2. 仓库 `skills/` 会被拷进包（排除点号目录与 `__pycache__`），且真树里有 ≥100 篇 `SKILL.md`；
3. 自检三项：包内 `.env` 非空、`.env.example` 非空、`config.yaml` 与模板漂移 —— 各自都会失败；
4. 提示里只出现**键名/键路径**，绝不回显密钥值或 open_id。
"""

from __future__ import annotations

import importlib.util
import subprocess
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
def bundle() -> ModuleType:
    return load_script("build_bundle")


REAL_ENV = "# 我的密钥\nLLM_API_KEY=«redacted:sk-…»\nFEISHU_APP_SECRET=fake-but-non-empty\n"
EMPTY_ENV_TEMPLATE = "# 模板：值留空\nLLM_API_KEY=\nFEISHU_APP_SECRET=''\n"

#: 本机 config.yaml 的样子：模板里没有的键就是"运行时自己写回的状态"
USER_CONFIG = "llm:\n  chat:\n    model: «本机模型标记»\nui:\n  window:\n    width: 1425\n"
TEMPLATE_CONFIG = "runtime:\n  plan_by_model: false\nui:\n  window: null\n"
WHITELIST_CONFIG = (
    "runtime:\n  plan_by_model: false\n"
    "feishu:\n  allowed_users:\n  - ou_«fake-open-id»\n"  # 假 open_id：断言它不会出现在报错里
    "ui:\n  window: null\n"
)

SKILL_FRONTMATTER = "---\nname: {name}\ndescription: 测试技能\n---\n\n正文\n"


def make_project(tmp_path: Path) -> Path:
    """一个最小可打包的"项目根"（含真 `.env` + 本机 `config.yaml` + 一份技能库）。"""
    root = tmp_path / "project"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "summary.md").write_text("提示词", encoding="utf-8")
    for name in ("README.md", "AGENTS.md", "deps-allowlist.txt"):
        (root / name).write_text(f"# {name}", encoding="utf-8")
    (root / "config.example.yaml").write_text(TEMPLATE_CONFIG, encoding="utf-8")
    (root / "config.yaml").write_text(USER_CONFIG, encoding="utf-8")
    (root / ".env.example").write_text(EMPTY_ENV_TEMPLATE, encoding="utf-8")
    (root / ".env").write_text(REAL_ENV, encoding="utf-8")
    # 版本号的事实源：打包时 `_VERSION` 就是从这份 `[project].version` 写出来的
    (root / "pyproject.toml").write_text(
        '[project]\nname = "scholarscout"\nversion = "9.9.9"\n', encoding="utf-8"
    )

    skills = root / "skills"
    (skills / "creative" / "ascii-art").mkdir(parents=True)
    (skills / "creative" / "ascii-art" / "SKILL.md").write_text(
        SKILL_FRONTMATTER.format(name="ascii-art"), encoding="utf-8"
    )
    (skills / "top-level").mkdir()
    (skills / "top-level" / "SKILL.md").write_text(
        SKILL_FRONTMATTER.format(name="top-level"), encoding="utf-8"
    )
    # 这些**不该**进包（.gitignore 的 `skills/.*` + `__pycache__/`）
    for hidden in (".archive", ".hub", ".curator_backups", "__pycache__"):
        (skills / hidden).mkdir()
        (skills / hidden / "SKILL.md").write_text("不该被拷进去\n", encoding="utf-8")
    (skills / ".curator_state").write_text("{}", encoding="utf-8")
    return root


def stub_pyinstaller(monkeypatch: pytest.MonkeyPatch, bundle: ModuleType) -> None:
    """替掉 PyInstaller：只按 `--distpath/--name` 造出它本该产出的 onedir 产物。"""

    def fake_run(command: list[str], cwd: Path | None = None, check: bool = False):
        dist = Path(command[command.index("--distpath") + 1])
        name = command[command.index("--name") + 1]
        built = dist / name  # onedir：永远多一层同名子目录
        (built / "_internal").mkdir(parents=True, exist_ok=True)
        (built / "_internal" / "kernel.dll").write_bytes(b"MZ")
        (built / f"{name}.exe").write_bytes(b"MZ")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)


def build_project(bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bundle, "ROOT", make_project(tmp_path))
    stub_pyinstaller(monkeypatch, bundle)
    out = tmp_path / "out"
    bundle.build("ScholarScout", out)
    return out


# ── 配置：只带模板，绝带本机状态 ────────────────────────────────────────


def test_bundle_ships_the_config_template_but_not_a_live_config(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """包里只放 `config.example.yaml`：应用读不到 `config.yaml` 会全用默认值，不需要它。"""
    out = build_project(bundle, tmp_path, monkeypatch)

    assert (out / "config.example.yaml").is_file(), "要让人看得见有哪些键、怎么注释"
    assert not (out / "config.yaml").exists(), "本机 config.yaml 是本机状态，不能随包发出去"
    assert (out / "QUICKSTART.md").read_text(encoding="utf-8").count("config.example.yaml") >= 1
    assert (out / "ScholarScout.exe").is_file()


def test_user_config_content_never_reaches_the_bundle(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本机 config.yaml 里的窗口几何/模型选择不许以任何形式出现在产物里。"""
    out = build_project(bundle, tmp_path, monkeypatch)

    leftovers = [
        path.relative_to(out).as_posix()
        for path in out.rglob("*")
        if path.is_file() and "«本机模型标记»" in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert leftovers == [], f"本机配置的值漏进了 {leftovers}"


def test_with_config_flag_is_gone(bundle: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """`--with-config` 已删除：它唯一的用途就是把本机 config.yaml 一起发出去。"""
    with pytest.raises(SystemExit) as excinfo:
        bundle.main(["--with-config"])

    assert excinfo.value.code == 2, "不认识的参数该按 argparse 的用法错误退出"
    assert "--with-config" in capsys.readouterr().err


# ── 技能：拷进包里，且排除名单与 .gitignore 一致 ────────────────────────


def test_skills_are_copied_next_to_the_exe(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skills/` 落在 exe 同级（`paths.skills_dir()` 冻结后正指向这里），嵌套层级保持原样。"""
    out = build_project(bundle, tmp_path, monkeypatch)

    assert (out / "skills" / "creative" / "ascii-art" / "SKILL.md").is_file()
    assert (out / "skills" / "top-level" / "SKILL.md").is_file()
    assert bundle.count_skill_files(out) == 2


@pytest.mark.parametrize(
    "hidden", [".archive", ".hub", ".curator_backups", "__pycache__", ".curator_state"]
)
def test_skills_hidden_bookkeeping_is_not_copied(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hidden: str
) -> None:
    """技能管理工具的记账（归档/索引缓存/旧快照/pycache）不进包：`.hub` 一个就有 48MB。"""
    out = build_project(bundle, tmp_path, monkeypatch)

    assert not (out / "skills" / hidden).exists()
    assert all(
        not part.startswith(".") for path in (out / "skills").rglob("*") for part in path.parts[-1:]
    )


def test_real_repo_skills_pass_the_hundred_mark(bundle: ModuleType, tmp_path: Path) -> None:
    """真仓库的 `skills/`（不是假项目）拷出来必须 ≥100 篇 —— 发布包的技能菜单靠它。"""
    target = tmp_path / "bundle"
    target.mkdir()

    count = bundle.stage_skills(target)

    assert count >= 100, f"仓库技能库只剩 {count} 篇，发布包的 `/` 菜单会明显残缺"
    assert not (target / "skills" / ".hub").exists(), "索引缓存不该跟着技能一起发出去"


def test_build_refuses_when_the_skill_library_is_empty(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一篇技能都没有就打不出包：`/` 菜单是死的，而这种"死"没人会当场发现。"""
    monkeypatch.setattr(bundle, "ROOT", make_project(tmp_path))
    monkeypatch.setattr(bundle, "skills_source", lambda: tmp_path / "nope")
    stub_pyinstaller(monkeypatch, bundle)

    with pytest.raises(SystemExit) as excinfo:
        bundle.build("ScholarScout", tmp_path / "out")

    assert "技能菜单" in str(excinfo.value)


# ── 自检一：包里有非空 .env 就失败 ──────────────────────────────────────


def test_with_secrets_is_refused_by_the_post_build_check(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真密钥进包 = 自检失败（非 0 退出），并且提示里**不能把密钥值打出来**。"""
    monkeypatch.setattr(bundle, "ROOT", make_project(tmp_path))
    stub_pyinstaller(monkeypatch, bundle)
    out = tmp_path / "out"

    with pytest.raises(SystemExit) as excinfo:
        bundle.build("ScholarScout", out, with_secrets=True)

    message = str(excinfo.value)
    assert "打包自检失败" in message
    assert ".env" in message
    assert "LLM_API_KEY" in message, "要指出是哪几个键（否则用户不知道该删什么）"
    assert "«redacted:sk-…»" not in message, "提示里绝不能回显密钥值"


def test_env_example_does_not_trip_the_check(bundle: ModuleType, tmp_path: Path) -> None:
    """`.env.example` 的值本来就是空的：它不许被当成密钥。"""
    target = tmp_path / "bundle"
    target.mkdir()
    (target / ".env.example").write_text(EMPTY_ENV_TEMPLATE, encoding="utf-8")

    bundle.assert_bundle_no_secrets(target)  # 不抛即通过


def test_env_example_with_a_filled_value_fails_the_check(
    bundle: ModuleType, tmp_path: Path
) -> None:
    """模板漏填空值 = 把某台机器的配置一起发出去，必须拦（这条是新加的第二项检查）。"""
    target = tmp_path / "bundle"
    target.mkdir()
    (target / ".env.example").write_text(
        "LLM_API_KEY=\nLLM_BASE_URL=https://x/v1\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit) as excinfo:
        bundle.assert_bundle_no_secrets(target)

    message = str(excinfo.value)
    assert ".env.example" in message
    assert "LLM_BASE_URL" in message
    assert "https://x/v1" not in message, "只报键名，不回显值"


def test_empty_values_in_a_copied_env_pass_the_check(bundle: ModuleType, tmp_path: Path) -> None:
    """拷了 .env 但值全空（模板被重命名）—— 不算泄露。"""
    target = tmp_path / "bundle"
    target.mkdir()
    (target / ".env").write_text(EMPTY_ENV_TEMPLATE, encoding="utf-8")

    bundle.assert_bundle_no_secrets(target)


@pytest.mark.parametrize(
    "content",
    [
        "LLM_API_KEY=sk-abc\n",
        "export LLM_API_KEY=sk-abc\n",  # shell 风格
        'LLM_API_KEY="sk-abc"\n',  # 引号包着
        "# 注释\nOTHER=1\n",  # 只有别的键非空也要拦
    ],
)
def test_any_non_empty_value_fails_the_check(
    bundle: ModuleType, tmp_path: Path, content: str
) -> None:
    target = tmp_path / "bundle"
    target.mkdir()
    (target / ".env").write_text(content, encoding="utf-8")

    with pytest.raises(SystemExit):
        bundle.assert_bundle_no_secrets(target)


def test_leaked_env_keys_returns_names_never_values(bundle: ModuleType, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(REAL_ENV, encoding="utf-8")

    keys = bundle.leaked_env_keys(env)

    assert keys == ["LLM_API_KEY", "FEISHU_APP_SECRET"]
    assert "«redacted:sk-…»" not in repr(keys)


def test_check_passes_when_the_bundle_has_no_env_at_all(bundle: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    target.mkdir()
    bundle.assert_bundle_no_secrets(target)


# ── 自检二：包里的 config.yaml 必须是模板的等价物 ───────────────────────


def test_live_config_with_a_feishu_whitelist_fails_the_check(
    bundle: ModuleType, tmp_path: Path
) -> None:
    """快照式：往包里塞一份带 `feishu.allowed_users` 的 config.yaml → 自检必须报错。

    这就是上一轮真发出去的那个包：`skills/` 是空的、`config.yaml` 却是某台机器的真状态。
    """
    target = tmp_path / "bundle"
    target.mkdir()
    (target / "config.example.yaml").write_text(TEMPLATE_CONFIG, encoding="utf-8")
    (target / "config.yaml").write_text(WHITELIST_CONFIG, encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        bundle.assert_bundle_no_secrets(target)

    message = str(excinfo.value)
    assert "打包自检失败" in message
    assert "feishu.allowed_users" in message, "要指出是哪个键漂了"
    assert "«fake-open-id»" not in message, "open_id 是用户身份，提示里不许回显"


def test_config_equal_to_the_template_apart_from_comments_passes(
    bundle: ModuleType, tmp_path: Path
) -> None:
    """把模板原样复制成 config.yaml（只多了注释/换了缩进）—— 等价，不算泄露。"""
    target = tmp_path / "bundle"
    target.mkdir()
    (target / "config.example.yaml").write_text(TEMPLATE_CONFIG, encoding="utf-8")
    (target / "config.yaml").write_text("# 照模板抄的\n" + TEMPLATE_CONFIG, encoding="utf-8")

    assert bundle.config_drift(target) == []
    bundle.assert_bundle_no_secrets(target)


def test_config_drift_lists_key_paths_only(bundle: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    target.mkdir()
    (target / "config.example.yaml").write_text(TEMPLATE_CONFIG, encoding="utf-8")
    (target / "config.yaml").write_text(USER_CONFIG, encoding="utf-8")

    drift = bundle.config_drift(target)

    assert "ui.window" in drift and "ui.window.width" in drift
    assert not any("1425" in key for key in drift), "漂移清单只回键路径，不回值"


def test_missing_config_yaml_means_no_drift(bundle: ModuleType, tmp_path: Path) -> None:
    """包里没有 config.yaml 才是常态（应用会用默认值）—— 不该被自检当成问题。"""
    target = tmp_path / "bundle"
    target.mkdir()
    (target / "config.example.yaml").write_text(TEMPLATE_CONFIG, encoding="utf-8")

    assert bundle.config_drift(target) == []


# ── 重打包：产物目录本身是 --dist-dir，第二次不许越套越深 ───────────────


def test_rebuilding_does_not_nest_internal_or_eat_user_data(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真机踩过的坑：`--dist-dir` 默认就是最终目录，上一版第二次打包直接把
    `_internal/` 挪进 `_internal/`（`shutil.Error: Destination path already exists`）。
    同时用户放在目录里的 `data/`、`.env` 不该被清掉 —— 重打包不是"重置目录"。"""
    out = build_project(bundle, tmp_path, monkeypatch)
    (out / "data").mkdir()
    (out / "data" / "keep.txt").write_text("我的资料", encoding="utf-8")
    (out / ".env").write_text("LLM_API_KEY=\n", encoding="utf-8")
    stale = out / "ScholarScout"  # 旧版打包留下的同名子目录（约等于多一份 100MB+）
    stale.mkdir()
    (stale / "old.txt").write_text("旧产物", encoding="utf-8")

    bundle.build("ScholarScout", out)  # 第二次

    assert not (out / "_internal" / "_internal").exists(), "产出越套越深了"
    assert not (out / "ScholarScout").exists(), "不许留下那层同名子目录"
    assert (out / "_internal" / "kernel.dll").is_file(), "新包的 _internal 要覆盖旧的那份"
    assert (out / "ScholarScout.exe").is_file()
    assert (out / "data" / "keep.txt").read_text(encoding="utf-8") == "我的资料"
    assert (out / ".env").is_file(), "空值 .env 不算泄露，也不该被重打包抹掉"


# ── 发版：`--clean` 让产物目录变成"刚组装好的那一份" ────────────────────


def test_clean_build_drops_previous_runtime_state(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--clean`（发版用）：上一轮运行留下的 `run/`、`data/`、`.env`、`*.log` 不许留在产物里。

    这是 v0.1.1 第一次发版的真事：`build_bundle.py` 往已有目录里覆盖，旧
    `run/logs/scout.log` 原样留着，`scripts/release.py` 的干净度自检把它拦了下来 ——
    拦得对，但"干净"该由组装保证，不该靠发版前记得手工删。
    """
    out = build_project(bundle, tmp_path, monkeypatch)
    (out / "run" / "logs").mkdir(parents=True)
    (out / "run" / "logs" / "scout.log").write_text("旧日志", encoding="utf-8")
    (out / "data").mkdir()
    (out / "data" / "old.db").write_bytes(b"x")
    (out / ".env").write_text("LLM_API_KEY=sk-old\n", encoding="utf-8")

    bundle.build("ScholarScout", out, clean=True)

    assert not (out / "run").exists(), "运行日志目录不许跟着发出去"
    assert not (out / "data").exists(), "用户资料目录不许跟着发出去"
    assert not (out / ".env").exists(), "旧 .env 不许留着（模板 .env.example 才是随包的那份）"
    assert (out / "ScholarScout.exe").is_file()
    assert (out / "skills" / "top-level" / "SKILL.md").is_file()


def test_version_marker_is_written_from_pyproject(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """包里那行 `_VERSION` 取自 `pyproject.toml` —— 打包版 `--version` 的唯一来源。"""
    out = build_project(bundle, tmp_path, monkeypatch)

    assert (out / "_VERSION").read_text(encoding="utf-8").strip() == bundle.project_version()
    assert bundle.project_version() == "9.9.9"
