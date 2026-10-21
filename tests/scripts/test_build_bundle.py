"""打包脚本（`scripts/build_bundle.py`）的**分发安全边界**。

为什么值得测：打包是"一次性、跑完就发出去"的动作，它错了不会让任何东西变红 ——
只会让一个**装着真 API 密钥的目录**看起来跟正常交付包一模一样。上一轮就是这么出的：
`--with-config` 把 `.env` 连密钥一起拷进了 `ScholarScout-Portable/`。

这里用桩替掉 PyInstaller（不真打包），只验三件事：
1. 默认只带 `.env.example`，真 `.env` 不进包；
2. `--with-config` 也不再拷 `.env`；
3. 包里一旦有非空 `.env`，打完的自检直接失败（非 0 退出），`.env.example` 不触发。
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


REAL_ENV = "# 我的密钥\nLLM_API_KEY=sk-really-secret-value\nFEISHU_APP_SECRET=fake-but-non-empty\n"
EMPTY_ENV_TEMPLATE = "# 模板：值留空\nLLM_API_KEY=\nFEISHU_APP_SECRET=''\n"


def make_project(tmp_path: Path) -> Path:
    """一个最小可打包的"项目根"（含真 `.env`：这正是要拦住的东西）。"""
    root = tmp_path / "project"
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "summary.md").write_text("提示词", encoding="utf-8")
    for name in ("README.md", "AGENTS.md", "deps-allowlist.txt"):
        (root / name).write_text(f"# {name}", encoding="utf-8")
    (root / "config.example.yaml").write_text("ui:\n  window: null\n", encoding="utf-8")
    (root / "config.yaml").write_text("llm:\n  model: deepseek-flash\n", encoding="utf-8")
    (root / ".env.example").write_text(EMPTY_ENV_TEMPLATE, encoding="utf-8")
    (root / ".env").write_text(REAL_ENV, encoding="utf-8")
    return root


def stub_pyinstaller(monkeypatch: pytest.MonkeyPatch, bundle: ModuleType) -> None:
    """替掉 PyInstaller：只按 `--distpath/--name` 造出它本该产出的 exe。"""

    def fake_run(command: list[str], cwd: Path | None = None, check: bool = False):
        dist = Path(command[command.index("--distpath") + 1])
        name = command[command.index("--name") + 1]
        built = dist / name
        built.mkdir(parents=True, exist_ok=True)
        (built / f"{name}.exe").write_bytes(b"MZ")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(bundle.subprocess, "run", fake_run)


# ── 默认：只带 .env.example ─────────────────────────────────────────────


def test_default_build_ships_env_example_but_never_the_real_env(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bundle, "ROOT", make_project(tmp_path))
    stub_pyinstaller(monkeypatch, bundle)
    out = tmp_path / "out"

    bundle.build("ScholarScout", out, with_config=False)

    assert (out / ".env.example").is_file(), "别人拿到包要照着模板填密钥"
    assert not (out / ".env").exists(), "默认打包不许把真 .env 带出去（含 API 密钥）"
    assert (out / "config.yaml").is_file(), "没有本机 config.yaml 时该放模板"
    assert (out / "ScholarScout.exe").is_file()


def test_with_config_still_does_not_copy_the_env(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--with-config` 只答应拷 config.yaml：上一轮它顺手把 .env 也拷了，那是泄密。"""
    monkeypatch.setattr(bundle, "ROOT", make_project(tmp_path))
    stub_pyinstaller(monkeypatch, bundle)
    out = tmp_path / "out"

    bundle.build("ScholarScout", out, with_config=True)

    assert not (out / ".env").exists()
    assert (out / "config.yaml").read_text(encoding="utf-8") == "llm:\n  model: deepseek-flash\n"


# ── 自检：包里有非空 .env 就失败 ────────────────────────────────────────


def test_with_secrets_is_refused_by_the_post_build_check(
    bundle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真密钥进包 = 自检失败（非 0 退出），并且提示里**不能把密钥值打出来**。"""
    monkeypatch.setattr(bundle, "ROOT", make_project(tmp_path))
    stub_pyinstaller(monkeypatch, bundle)
    out = tmp_path / "out"

    with pytest.raises(SystemExit) as excinfo:
        bundle.build("ScholarScout", out, with_config=False, with_secrets=True)

    message = str(excinfo.value)
    assert "打包自检失败" in message
    assert ".env" in message
    assert "LLM_API_KEY" in message, "要指出是哪几个键（否则用户不知道该删什么）"
    assert "sk-really-secret-value" not in message, "提示里绝不能回显密钥值"


def test_env_example_does_not_trip_the_check(bundle: ModuleType, tmp_path: Path) -> None:
    """`.env.example` 的值本来就是空的：它不许被当成密钥。"""
    target = tmp_path / "bundle"
    target.mkdir()
    (target / ".env.example").write_text(EMPTY_ENV_TEMPLATE, encoding="utf-8")

    bundle.assert_bundle_no_secrets(target)  # 不抛即通过


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
    assert "sk-really-secret-value" not in repr(keys)


def test_check_passes_when_the_bundle_has_no_env_at_all(bundle: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "bundle"
    target.mkdir()
    bundle.assert_bundle_no_secrets(target)
