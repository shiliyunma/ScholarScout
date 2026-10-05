"""设置读写（`settings_io`）与它的 RPC 契约。

为什么值得这么测：这个模块会**改用户机器上的配置文件**，而且是"错了不报错、只是没用"的那种错：
- 拼错的键如果被静默忽略，用户以为改了模型、实际还在用旧的；
- 写配置如果不是原子的，崩在中间就留下半截 yaml，下次启动直接炸；
- 密钥如果被回传到前端，它会出现在聊天记录、截图、导出里。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scholarscout import settings_io
from scholarscout.config import load_config
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一份独立的 config.yaml（不碰仓库里那份）。

    **同时把 `paths.home()` 指到 tmp_path**：否则"摘掉 .env 覆盖"这条逻辑
    会去改用户真实的 `.env`，测试就成了破坏性操作。
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        "search:\n  searxng_base_url: http://localhost:8080\n  openalex_mailto: me@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCHOLARSCOUT_CONFIG", str(path))
    monkeypatch.setattr(settings_io.paths, "home", lambda *a, **k: tmp_path)
    return path


def test_read_settings_masks_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-value")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config = load_config(tmp_path / "nonexistent.yaml")
    payload = settings_io.read_settings(config)
    assert payload["secrets_configured"]["llm.api_key"] is True
    assert payload["secrets_configured"]["search.tavily_api_key"] is False
    text = json.dumps(payload, ensure_ascii=False)
    assert "sk-secret-value" not in text, "密钥泄漏到设置接口里了"


def test_update_writes_and_reloads(config_file: Path) -> None:
    config = load_config(config_file)
    updated = settings_io.apply_updates(config, {"llm": {"chat": {"model": "deepseek-reasoner"}}})
    assert updated.chat.model == "deepseek-reasoner"
    assert "deepseek-reasoner" in config_file.read_text(encoding="utf-8")


def test_changing_model_drops_the_dotenv_override(config_file: Path, tmp_path: Path) -> None:
    """`.env` 里的 LLM_MODEL 会盖掉 config.yaml —— 改模型时必须把它摘掉，否则"改了没生效"。"""
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=old-model\nLLM_API_KEY=sk-x\n", encoding="utf-8")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("LLM_MODEL", "old-model")
    try:
        notes: list[str] = []
        config = load_config(config_file)
        updated = settings_io.apply_updates(
            config, {"llm": {"chat": {"model": "new-model"}}}, env_path=env, notes=notes
        )
        assert updated.chat.model == "new-model"
        text = env.read_text(encoding="utf-8")
        assert "LLM_MODEL" not in text, ".env 里的覆盖必须被摘掉"
        assert "LLM_API_KEY=sk-x" in text, "别的行不能被误删"
        assert any("LLM_MODEL" in note for note in notes), "要如实告诉用户摘掉了什么"
    finally:
        monkeypatch.undo()


def test_auto_artifacts_defaults_to_off(config_file: Path) -> None:
    """产物默认**不自动生成**（用户要求：先问再生成）。

    这条曾经是错的：界面拿 `值 !== false` 当勾选，配置里没这个键时值缺失 → 复选框显示勾上，
    用户看到「每轮自动生成产物」是开的，跟他自己的决定正好相反。
    """
    config = load_config(config_file)
    payload = settings_io.read_settings(config)
    assert payload["values"]["generation.auto_artifacts"] is False


def test_update_preserves_keys_it_does_not_manage(config_file: Path) -> None:
    """用户自己加的键（searxng_base_url / openalex_mailto）不能被抹掉。"""
    config = load_config(config_file)
    settings_io.apply_updates(config, {"runtime": {"allow_clarify": False}})
    text = config_file.read_text(encoding="utf-8")
    assert "searxng_base_url" in text and "openalex_mailto" in text


def test_unknown_key_is_rejected(config_file: Path) -> None:
    """拼错键必须报错 —— 静默忽略会让人以为改生效了。"""
    config = load_config(config_file)
    with pytest.raises(settings_io.SettingsError, match="不认识的设置项"):
        settings_io.apply_updates(config, {"llm": {"chat": {"modle": "x"}}})


@pytest.mark.parametrize(
    ("updates", "pattern"),
    [
        ({"llm": {"chat": {"temperature": 9}}}, "不能大于"),
        ({"llm": {"chat": {"temperature": -1}}}, "不能小于"),
        ({"llm": {"chat": {"max_tokens": 10}}}, "不能小于"),
        ({"llm": {"chat": {"base_url": "ftp://x"}}}, "http"),
        ({"llm": {"chat": {"model": "  "}}}, "不能为空"),
        ({"runtime": {"allow_clarify": "yes"}}, "true/false"),
    ],
)
def test_invalid_values_are_rejected(config_file: Path, updates: dict, pattern: str) -> None:
    config = load_config(config_file)
    with pytest.raises(settings_io.SettingsError, match=pattern):
        settings_io.apply_updates(config, updates)


def test_config_write_is_atomic(config_file: Path) -> None:
    """写完不留 .tmp 残留，且内容能被 yaml 正常解析。"""
    config = load_config(config_file)
    settings_io.apply_updates(config, {"features": {"enable_mineru": False}})
    assert not list(config_file.parent.glob("*.tmp"))
    assert load_config(config_file).features.enable_mineru is False


def test_api_key_goes_to_env_not_yaml(config_file: Path, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# 注释要留着\nLLM_MODEL=deepseek-flash\n", encoding="utf-8")
    config = load_config(config_file)
    settings_io.apply_updates(config, {"llm": {"api_key": "sk-new"}}, env_path=env)
    text = env.read_text(encoding="utf-8")
    assert "LLM_API_KEY=sk-new" in text
    assert "# 注释要留着" in text and "LLM_MODEL=deepseek-flash" in text
    assert "sk-new" not in config_file.read_text(encoding="utf-8"), "密钥不该写进 config.yaml"


def test_env_key_is_replaced_in_place(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=old\nOTHER=1\n", encoding="utf-8")
    settings_io.write_env(env, {"LLM_API_KEY": "new"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["LLM_API_KEY=new", "OTHER=1"]


def test_empty_secret_is_rejected(config_file: Path, tmp_path: Path) -> None:
    config = load_config(config_file)
    with pytest.raises(settings_io.SettingsError, match="不能为空"):
        settings_io.apply_updates(config, {"llm": {"api_key": "   "}}, env_path=tmp_path / ".env")


def test_data_usage_reports_sizes_blobs(tmp_path: Path) -> None:
    (tmp_path / "data" / "cache" / "ab").mkdir(parents=True)
    (tmp_path / "data" / "cache" / "ab" / "x.bin").write_bytes(b"x" * 100)
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    (tmp_path / "run" / "state.db").write_bytes(b"y" * 50)
    usage = settings_io.data_usage(tmp_path)
    assert usage["fetch_cache"]["bytes"] == 100
    assert usage["fetch_cache"]["files"] == 1
    assert usage["database"]["bytes"] == 50


def test_cleanup_cache_but_never_the_materials(tmp_path: Path) -> None:
    for rel in (
        "data/cache/a.bin",
        "data/raw/ab/keep.pdf",
        "data/text/keep.txt",
        "data/notes/keep.md",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"z" * 10)
    removed = settings_io.cleanup(tmp_path, "fetch_cache")
    assert removed == {"files": 1, "bytes": 10}
    assert (tmp_path / "data" / "raw" / "ab" / "keep.pdf").is_file(), "资料本体不许被清掉"


def test_cleanup_rejects_unknown_target(tmp_path: Path) -> None:
    with pytest.raises(settings_io.SettingsError, match="不允许清理"):
        settings_io.cleanup(tmp_path, "data/raw")


def test_cleanup_never_deletes_the_database(tmp_path: Path) -> None:
    (tmp_path / "run").mkdir(parents=True)
    (tmp_path / "run" / "state.db").write_bytes(b"important")
    (tmp_path / "run" / "junk.log").write_bytes(b"junk")
    settings_io.cleanup(tmp_path, "run_tmp")
    assert (tmp_path / "run" / "state.db").is_file()
    assert not (tmp_path / "run" / "junk.log").exists()


# ------------------------------------------------------------------ RPC 契约


def test_rpc_settings_get_and_update(ctx: AppContext, config_file: Path) -> None:
    payload = dispatch(ctx, "settings.get", {})
    assert "values" in payload and "secrets_configured" in payload

    updated = dispatch(ctx, "settings.update", {"updates": {"llm": {"chat": {"max_tokens": 8192}}}})
    assert updated["ok"] is True
    assert updated["settings"]["values"]["llm.chat.max_tokens"] == 8192
    assert ctx.config.chat.max_tokens == 8192, "改完要立即生效"


def test_rpc_settings_update_rejects_bad_value(ctx: AppContext, config_file: Path) -> None:
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "settings.update", {"updates": {"llm": {"chat": {"temperature": 42}}}})
    assert exc.value.code == -32602


def test_rpc_settings_update_requires_object(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "settings.update", {"updates": ["nope"]})
    assert exc.value.code == -32602


def test_rpc_settings_usage_and_cleanup(ctx: AppContext) -> None:
    usage = dispatch(ctx, "settings.usage", {})
    assert "database" in usage["usage"]
    assert "fetch_cache" in usage["cleanable"]
    result = dispatch(ctx, "settings.cleanup", {"target": "fetch_cache"})
    assert result["ok"] is True and "removed" in result
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "settings.cleanup", {"target": "data/raw"})
    assert exc.value.code == -32602
