"""自检脚本本身的回归测试。

为什么值得单独测：`scripts/check_search_sources.py` 是"真机验证"的执行者，
它一旦**静默空转**（比如把 `providers: ["auto"]` 当成一个叫 auto 的来源，查不到 key → 跳过 → 报成功），
就会给出"全绿"的假结论，而实际一个来源都没打。真机上就这样骗过一次：
默认路径输出了 `[skip] auto: missing_api_key` + `退出码=0`，arxiv 根本没跑。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_search_sources.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_search_sources", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load_script()
    # 不读真实 .env，也不让环境里残留的 key 影响 auto 解析
    monkeypatch.setattr(module, "load_dotenv", lambda *_a, **_k: {})
    for env in ("TAVILY_API_KEY", "SERPER_API_KEY", "BRAVE_API_KEY", "GOOGLE_CSE_KEY"):
        monkeypatch.delenv(env, raising=False)
    # 也不读本机的 config.yaml：测试不该依赖开发者本地配置（有人配了 SearxNG，
    # auto 就会多出一个来源，断言"只有 arxiv"会平白失败）→ 指向一个不存在的路径拿默认值
    from scholarscout.config import load_config as real_load_config

    monkeypatch.setattr(
        module,
        "load_config",
        lambda *_a, **_k: real_load_config(Path("__no_such_config__.yaml"), load_env=False),
    )
    return module


def test_default_path_resolves_auto_to_real_sources(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """默认配置（providers: ["auto"]）必须真的去查 arxiv，而不是查一个叫 auto 的"来源"。"""
    called: list[str] = []

    class FakeProvider:
        def search(self, query: object) -> list[object]:
            from scholarscout.providers.base import SourceCard

            called.append("arxiv")
            return [SourceCard(url="https://arxiv.org/abs/1", title="真结果", source_type="arxiv")]

    monkeypatch.setattr(script, "build", lambda name, http, base_url: FakeProvider())
    monkeypatch.setattr(sys, "argv", ["check_search_sources.py"])

    code = script.main()

    out = capsys.readouterr().out
    assert "解析后的来源：arxiv" in out
    assert called == ["arxiv"], "auto 没被展开，脚本在空转"
    assert code == 0
    assert "命中 1 条" in out


def test_unknown_source_name_is_a_hard_failure(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """写错的来源名必须让脚本失败 —— 静默跳过等于假装验证过了。"""
    monkeypatch.setattr(sys, "argv", ["check_search_sources.py", "--only", "bingg"])
    code = script.main()
    assert code == 1
    assert "bingg" in capsys.readouterr().out


def test_unconfigured_source_is_reported_but_not_fatal(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺 key / 缺配置要如实打印，且不算失败（它的预期就是"没配"）。"""
    monkeypatch.setattr(sys, "argv", ["check_search_sources.py", "--only", "tavily"])
    code = script.main()
    assert code == 0
    assert "missing_api_key" in capsys.readouterr().out
