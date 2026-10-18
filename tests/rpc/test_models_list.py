"""`models.list` 的契约：界面两个下拉的**唯一数据源**。

为什么值得单测（而不是只靠界面探针）：
- 它是"档位真是服务端声明的"这件事的唯一出口 —— 回包里的 `supported_levels` 一旦编出来，
  界面就会显示一个发不出去的档（下一轮 400），而探针只看得到"下拉有几项"；
- 探测失败时它**不能抛错**（抛了输入区就空白），必须回落并在 `note` 里说清楚；
- `remember=true` 会动用户机器上的 `config.yaml` —— 写错就是把候选清单弄丢。

所以这里逐项钉住：回包形状 / 探测失败回落 / 降级标注 / 记住候选（并**重启后仍在**）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from scholarscout import paths, settings_io
from scholarscout.config import load_config
from scholarscout.providers import models
from scholarscout.providers.models import ModelInfo, ModelsProbeError
from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state.db import Database

FULL = ("low", "high", "max")
TWO = ("low", "high")


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """环境里的 LLM_MODEL / LLM_BASE_URL 会盖掉 config.yaml（那是另一条口径，别串进来）。"""
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    models.reset_cache()
    yield
    models.reset_cache()


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[AppContext, Path]]:
    """独立 config.yaml + 独立 home（绝不碰仓库里那份与真实 .env）。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        "llm:\n  chat:\n    model: m-now\n    base_url: https://a.test/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCHOLARSCOUT_CONFIG", str(path))
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setattr(paths, "home", lambda *a, **k: tmp_path)
    monkeypatch.setattr(settings_io.paths, "home", lambda *a, **k: tmp_path)
    db = Database(tmp_path / "state.db")
    try:
        yield AppContext(config=load_config(path, load_env=False), db=db), path
    finally:
        db.close()


def stub_probe(monkeypatch: pytest.MonkeyPatch, found: list[ModelInfo] | Exception) -> list[int]:
    """把探测换成桩，返回一个计数器（`calls[0]` = 真被调了几次）。"""
    calls = [0]

    def fake(**kwargs: Any) -> list[ModelInfo]:
        del kwargs
        calls[0] += 1
        if isinstance(found, Exception):
            raise found
        return found

    monkeypatch.setattr(models, "probe", fake)
    return calls


def test_reports_exactly_what_the_endpoint_declares(
    app: tuple[AppContext, Path], monkeypatch
) -> None:
    ctx, _ = app
    stub_probe(
        monkeypatch,
        [
            ModelInfo(id="m-now", label="现在这个", efforts=FULL, default_effort="high"),
            ModelInfo(id="m-fast", efforts=TWO, default_effort="low"),
        ],
    )
    out = dispatch(ctx, "models.list", {})

    assert out["available_models"] == ["m-now", "m-fast"]
    assert out["models"][0] == {
        "id": "m-now",
        "label": "现在这个",
        "supported_levels": ["low", "high", "max"],
        "default_level": "high",
    }
    assert out["models"][1]["supported_levels"] == ["low", "high"], "每个模型各自的档位不许合并"
    assert out["current"] == "m-now"
    assert out["source"] == "probe"
    assert out["supported_levels"] == ["low", "high", "max"]

    # 默认档 medium → 线上 high；文案由后端拼（界面直接落地）
    assert out["current_plan"] == {
        "level": "medium",
        "effort": "high",
        "exact": True,
        "label": "中（high）",
    }
    values = [item["value"] for item in out["level_options"]]
    assert values == ["minimal", "low", "medium", "high", "max", "none"]
    assert out["level_options"][2]["label"] == "中（high）"
    assert all(item["supported"] is True for item in out["level_options"])


def test_probe_failure_falls_back_without_raising(
    app: tuple[AppContext, Path], monkeypatch
) -> None:
    """③ 探测失败 → 回落配置、**不报错**，并且如实说明没探到。"""
    ctx, _ = app
    calls = stub_probe(monkeypatch, ModelsProbeError("503 Service Unavailable"))
    out = dispatch(ctx, "models.list", {})

    assert calls[0] == 1, "没真的尝试探测（那就永远不会更新候选）"
    assert out["source"] == "config"
    assert "探测失败" in out["note"] and "503" in out["note"], f"没如实说明原因：{out['note']!r}"
    assert "m-now" in out["available_models"], "回落后当前模型必须还在候选里"
    assert out["supported_levels"] == list(FULL), "探不到就按官方三档兜底，不编别的"
    assert next(item["label"] for item in out["level_options"]).startswith("最小（low）"), (
        "第一项就是「最小」档（界面按这个顺序画）"
    )


def test_without_a_key_it_does_not_probe_at_all(
    app: tuple[AppContext, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """没 key 就不探测：必然 401，白等一次网络 —— 直接回落配置并如实说明。"""
    ctx, _ = app
    calls = stub_probe(monkeypatch, [ModelInfo(id="m-now")])
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    out = dispatch(ctx, "models.list", {})

    assert calls[0] == 0, "没 key 还去打 /models（白等一次网络）"
    assert out["source"] == "config"
    assert "未配置 LLM_API_KEY" in out["note"], f"没如实说明为什么没探：{out['note']!r}"
    assert out["available_models"] == ["m-now"], "回落后当前模型必须还在候选里"
    assert out["supported_levels"] == list(FULL), "探不到就按官方三档兜底，不编别的"


def test_unsupported_level_is_flagged_as_degraded(
    app: tuple[AppContext, Path], monkeypatch
) -> None:
    """服务端只声明 low/high 时选了 max：就近降到 high，并**标出 exact=false**。"""
    ctx, path = app
    path.write_text(
        "llm:\n  chat:\n    model: m-now\n    base_url: https://a.test/v1\n    reasoning_effort: max\n",
        encoding="utf-8",
    )
    ctx.config = load_config(path, load_env=False)
    stub_probe(monkeypatch, [ModelInfo(id="m-now", efforts=TWO, default_effort="high")])
    out = dispatch(ctx, "models.list", {})

    assert out["current_plan"] == {
        "level": "max",
        "effort": "high",
        "exact": False,
        "label": "最高（high）",
    }, "降级了却没标注（界面会以为真的按最高档在跑）"
    by_value = {item["value"]: item for item in out["level_options"]}
    assert by_value["max"]["supported"] is False, "发不出去的档必须置灰"
    assert by_value["medium"]["supported"] is True
    assert by_value["none"]["supported"] is True, "「关」永远可选（它不发 effort）"


def test_reading_does_not_touch_the_config_file(app: tuple[AppContext, Path], monkeypatch) -> None:
    """读接口不许改用户配置：只有 remember=true 才写。"""
    ctx, path = app
    before = path.read_text(encoding="utf-8")
    stub_probe(monkeypatch, [ModelInfo(id="m-now"), ModelInfo(id="m-fast")])
    dispatch(ctx, "models.list", {})
    assert path.read_text(encoding="utf-8") == before, "只是打开下拉就把配置写了"


def test_remember_writes_the_candidates_and_survives_a_restart(
    app: tuple[AppContext, Path], monkeypatch
) -> None:
    """④ 记住候选 → 写进 config.yaml → **重新加载（重启）后仍在**。"""
    ctx, path = app
    stub_probe(monkeypatch, [ModelInfo(id="m-now"), ModelInfo(id="m-fast")])
    dispatch(ctx, "models.list", {"remember": True})

    assert "available_models" in path.read_text(encoding="utf-8")
    restarted = load_config(path, load_env=False)  # 模拟重启：从盘上重新读
    assert restarted.chat.available_models == ("m-now", "m-fast")
    assert restarted.chat.model == "m-now", "记住候选不许顺手改当前模型（选哪个是用户的事）"


def test_remember_is_a_no_op_when_the_probe_failed(
    app: tuple[AppContext, Path], monkeypatch
) -> None:
    """探测失败时 remember 不许把"回落出来的那一条"当成真实清单写进配置。"""
    ctx, path = app
    before = path.read_text(encoding="utf-8")
    stub_probe(monkeypatch, ModelsProbeError("timeout"))
    dispatch(ctx, "models.list", {"remember": True})
    assert path.read_text(encoding="utf-8") == before


def test_level_choice_persists_across_a_restart(app: tuple[AppContext, Path]) -> None:
    """④ 界面选档 → `settings.update` → 盘上真有 → 重启后 load_config 还是它。"""
    ctx, path = app
    dispatch(
        ctx, "settings.update", {"updates": {"llm": {"chat": {"reasoning_effort": "minimal"}}}}
    )

    assert "reasoning_effort: minimal" in path.read_text(encoding="utf-8")
    restarted = load_config(path, load_env=False)
    assert restarted.chat.reasoning_effort == "minimal"
    assert settings_io.read_settings(restarted)["values"]["llm.chat.reasoning_effort"] == "minimal"


def test_bad_level_is_rejected_instead_of_silently_ignored(app: tuple[AppContext, Path]) -> None:
    """拼错的档位必须报 INVALID_PARAMS（静默忽略＝用户以为改了、其实没改）。"""
    ctx, path = app
    before = path.read_text(encoding="utf-8")
    with pytest.raises(RpcError) as excinfo:
        dispatch(
            ctx, "settings.update", {"updates": {"llm": {"chat": {"reasoning_effort": "highest"}}}}
        )
    assert excinfo.value.code == Code.INVALID_PARAMS
    assert path.read_text(encoding="utf-8") == before
