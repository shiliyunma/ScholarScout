"""`scripts/searxng_up.py` 的回归测试（离线，不起 Docker）。

为什么要测：这个脚本是"通用检索挂了"时的**唯一自救入口**，它自己出错就没人救得了。
钉住两件真出过事的东西：
1. **Docker Hub 不可达**（国内常态）时不能只报失败 —— 得换镜像源把镜像拉下来再打成官方标签，
   否则"新机器第一次跑"会卡在 `failed to resolve reference "docker.io/searxng/searxng:latest"`。
2. 判断"是不是拉不到镜像"不能把别的问题（端口被占、配置写错）也当成镜像问题，
   否则会在错误的方向上重试很久。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "searxng_up.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("searxng_up", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script() -> ModuleType:
    return load_script()


#: 本机真机抓到的原文（docker compose up 在 Hub 不可达时打印的）
REAL_FAILURE = (
    "Image searxng/searxng:latest Pulling \n"
    "Image searxng/searxng:latest Error failed to resolve reference "
    '"docker.io/searxng/searxng:latest": failed to do request: '
    'Head "https://registry-1.docker.io/v2/searxng/searxng/manifests/latest": '
    "dialing registry-1.docker.io:443 container via direct connection because "
    "Docker Desktop has no HTTPS proxy: connecting to registry-1.docker.io:443: "
    "dial tcp 162.125.80.6:443: connectex: A connection attempt failed because "
    "the connected party did not properly respond after a period of time"
)


def test_real_hub_failure_is_detected(script: ModuleType) -> None:
    assert script._looks_like_registry_unreachable(REAL_FAILURE) is True


def test_other_failures_are_not_treated_as_registry_problems(script: ModuleType) -> None:
    """端口被占 / 配置写错 → 不该走"换镜像源"那条路（那会白等十几分钟）。"""
    assert script._looks_like_registry_unreachable("bind: address already in use") is False
    assert script._looks_like_registry_unreachable("no such file: settings.yml") is False
    assert script._looks_like_registry_unreachable("") is False


def test_mirror_pull_tags_official_name(script: ModuleType) -> None:
    """换源拉取之后**必须重打成官方标签** —— compose 文件里引用的是官方名。"""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, timeout: int = 120) -> tuple[int, str]:
        calls.append(cmd)
        return 0, "ok"

    monkey_runner = fake_run
    script.run = monkey_runner  # type: ignore[assignment]
    code, output = script._pull_via_mirror()

    assert code == 0 and output == "ok"
    assert calls == [
        ["docker", "pull", script.MIRROR_IMAGE],
        ["docker", "tag", script.MIRROR_IMAGE, script.IMAGE],
    ]
    assert script.IMAGE == "searxng/searxng:latest"


def test_mirror_pull_failure_stops_before_tagging(script: ModuleType) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, timeout: int = 120) -> tuple[int, str]:
        calls.append(cmd)
        return 1, "换源也拉不动"

    script.run = fake_run  # type: ignore[assignment]
    code, output = script._pull_via_mirror()

    assert code == 1 and "换源也拉不动" in output
    assert len(calls) == 1, "拉取失败就不该再去 tag"
