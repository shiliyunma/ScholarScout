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


# ------------------------------------------------- 设置里的两个新可编辑项（docs/20 §5.1 / §6.3）
#
# 为什么单独一组：这是用户在**设置面板**里拨开关/填数字的两个键，最容易出的错是
# "界面能改、后端不认"或"非法值被静默当成别的意思"。这里把往返、折算与拒绝全钉住。


def test_update_notify_flag_roundtrip(ctx: AppContext, config_file: Path) -> None:
    """跑完提醒开关：true / false 都能写盘，改完 `load_config` 读到同一个值。"""
    for value in (True, False):
        payload = dispatch(
            ctx, "settings.update", {"updates": {"runtime": {"notify_turn_done": value}}}
        )
        assert payload["ok"] is True
        assert payload["settings"]["values"]["runtime.notify_turn_done"] is value
        assert ctx.config.runtime.notify_turn_done is value, "改完要立即生效（不必重启）"
        assert load_config(config_file).runtime.notify_turn_done is value


def test_update_daily_token_budget_roundtrip(ctx: AppContext, config_file: Path) -> None:
    """每日 token 上限：正整数原样落盘（int，不是字符串、不是浮点）。"""
    payload = dispatch(
        ctx, "settings.update", {"updates": {"runtime": {"daily_token_budget": 500_000}}}
    )
    assert payload["settings"]["values"]["runtime.daily_token_budget"] == 500_000
    reloaded = load_config(config_file)
    assert reloaded.runtime.daily_token_budget == 500_000
    assert isinstance(reloaded.runtime.daily_token_budget, int)
    # 守卫据此才能真的封顶（0/None 都不建守卫）
    from scholarscout.runtime import budget as daily_budget

    assert daily_budget.from_runtime(reloaded.runtime) is not None


@pytest.mark.parametrize("value", [None, "", "   ", 0])
def test_update_daily_token_budget_blank_or_zero_means_unlimited(
    ctx: AppContext, config_file: Path, value: object
) -> None:
    """留空 / 空串 / 0 / null → **null（不限制）**，不是 0，也不是报错。"""
    # 先写一个上限，再清掉：这样"清空真的生效"不是靠初始值碰巧为 None
    dispatch(ctx, "settings.update", {"updates": {"runtime": {"daily_token_budget": 1234}}})
    payload = dispatch(
        ctx, "settings.update", {"updates": {"runtime": {"daily_token_budget": value}}}
    )
    assert payload["settings"]["values"]["runtime.daily_token_budget"] is None
    reloaded = load_config(config_file)
    assert reloaded.runtime.daily_token_budget is None, "0/空串必须折算成不限（None）"
    from scholarscout.runtime import budget as daily_budget

    assert daily_budget.from_runtime(reloaded.runtime) is None, "不限时守卫不该被创建"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (-1, "不能为负"),
        (-1_000_000, "不能为负"),
        ("abc", "需要数字"),
        ("1e", "需要数字"),
        (1.5, "需要整数"),
        (True, "需要数字"),
        ([], "需要数字"),
    ],
)
def test_update_rejects_bad_daily_budget_without_writing(
    ctx: AppContext, config_file: Path, value: object, expected: str
) -> None:
    """非法值 → `INVALID_PARAMS` 且**盘上一个字节都不变**（不能写一半再报错）。"""
    before = config_file.read_bytes()
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "settings.update", {"updates": {"runtime": {"daily_token_budget": value}}})
    assert exc.value.code == -32602, "非法值必须是参数错误，不能是内部错误"
    assert expected in str(exc.value)
    assert config_file.read_bytes() == before, "校验失败却已经把配置写了一半"


def test_update_rejects_non_bool_notify_flag(ctx: AppContext, config_file: Path) -> None:
    """开关只收真 bool：字符串 \"true\" / 数字 1 都不算（否则界面显示与磁盘会不一致）。"""
    before = config_file.read_bytes()
    for value in ("true", 1, None, "yes"):
        with pytest.raises(RpcError) as exc:
            dispatch(ctx, "settings.update", {"updates": {"runtime": {"notify_turn_done": value}}})
        assert exc.value.code == -32602
        assert "true/false" in str(exc.value)
    assert config_file.read_bytes() == before


def test_update_still_rejects_unknown_keys(ctx: AppContext, config_file: Path) -> None:
    """白名单之外的键仍然报错（新增两个键不许把"拒绝未知键"这道门弄松）。"""
    before = config_file.read_bytes()
    for key in ("daily_token_limit", "notify_turn_done_platform", "nope"):
        with pytest.raises(RpcError) as exc:
            dispatch(ctx, "settings.update", {"updates": {"runtime": {key: 1}}})
        assert exc.value.code == -32602
        assert "不认识的设置项" in str(exc.value)
    assert config_file.read_bytes() == before


def test_settings_get_exposes_the_two_new_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认配置下 `settings.get` 要能读出这两个键：开关 null→默认 false，上限 null→不限。

    界面靠 `values["runtime.daily_token_budget"]` 判"显示不限"，键缺失会渲染成空字符串
    而说不清是"不限"还是"读不到"。
    """
    config = load_config(tmp_path / "nonexistent.yaml")
    values = settings_io.read_settings(config)["values"]
    assert values["runtime.notify_turn_done"] is False
    assert values["runtime.daily_token_budget"] is None
    assert settings_io.FIELDS["runtime.daily_token_budget"].nullable is True


def test_turning_notify_off_restores_disabled(ctx: AppContext, config_file: Path) -> None:
    """开关从设置里拨开 → 拨回关：`from_config` 不再造提醒器，`notify` 回 `disabled`。

    为什么这条要单独测：开关现在由**用户随时可改**（`settings.update` 会换掉 `ctx.config`
    里的 runtime），"关着不发"不能只靠类默认值成立 —— 得从改配置的那条真实路径走一遍。
    """
    from scholarscout.runtime import notify

    dispatch(ctx, "settings.update", {"updates": {"runtime": {"notify_turn_done": True}}})
    assert notify.from_config(ctx.config.runtime, db=ctx.db, http=None) is not None

    dispatch(ctx, "settings.update", {"updates": {"runtime": {"notify_turn_done": False}}})
    assert ctx.config.runtime.notify_turn_done is False
    assert notify.from_config(ctx.config.runtime, db=ctx.db, http=None) is None
    # 回归：即使真造出一个关着的提醒器，第一步也必须是 disabled（不查库、不出网）
    outcome = notify.TurnDoneNotifier(enabled=False).notify(
        turn_id="trn_x", session_id="ses_a", active_session_id="ses_b"
    )
    assert outcome.fired is False
    assert outcome.reason == "disabled"


# ------------------------------------------- 允许按需下载（`features.enable_download`）
#
# 为什么单独一组：用户反馈「设置→功能→允许下载这条链路并没有做好」—— 开关原本只活在
# config.yaml 里，`settings.get` 读不到、面板没有入口、地图的「去设置」是死路。
# 这一组钉住三件事：**读得到**（默认 false，不是缺失）、**写进去立即生效**（不必重启）、
# **白名单没有被新增键弄松**（非法类型与拼错的键照样报错，且盘上一个字节都不变）。


def test_settings_get_exposes_download_flag_defaulting_to_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认配置（没有 ``features.enable_download`` 这个键）→ `settings.get` 读出 **False**。

    缺失与 false 在界面上长得一样，但语义不同：`read_settings` 一旦漏掉这个键，
    面板上的复选框就永远显示未勾选、保存时又不发这个字段（"改了没生效"）。
    """
    config = load_config(tmp_path / "nonexistent.yaml")
    values = settings_io.read_settings(config)["values"]
    assert "features.enable_download" in values, "settings.get 没把这个键给界面"
    assert values["features.enable_download"] is False
    assert settings_io.FIELDS["features.enable_download"].kind == "bool"


def test_update_download_flag_roundtrip_and_takes_effect_immediately(
    ctx: AppContext, config_file: Path
) -> None:
    """开关写 true 后：回包/`ctx.config`/盘上的 config.yaml 三处都是 true，且**立即生效**。

    "立即生效"不是看 `ctx.config` 就够 —— 真正决定要不要出网的是 `archive.download`。
    这里用一条**不存在的 source_id** 走真实 RPC：开关关着时它只会回"已关闭"（不动网），
    打开之后才会走到"库里没有这条资料"。两种情况都不出网，所以能在离线测试里跑。
    """
    disabled = dispatch(ctx, "archive.download", {"source_id": "src_missing"})
    assert disabled["ok"] is False and "enable_download" in disabled["reason"]

    payload = dispatch(ctx, "settings.update", {"updates": {"features": {"enable_download": True}}})
    assert payload["ok"] is True
    assert payload["settings"]["values"]["features.enable_download"] is True
    assert ctx.config.features.enable_download is True, "改完没换掉 ctx.config（没热生效）"
    assert load_config(config_file).features.enable_download is True, "没写进 config.yaml"

    after = dispatch(ctx, "archive.download", {"source_id": "src_missing"})
    assert "enable_download" not in after["reason"], "开关打开了，archive.download 还当它是关的"
    assert "库里没有这条资料" in after["reason"], "打开后没走到真正的下载通路"

    # 拨回去也要真的关掉（不能只认一次"开"）
    dispatch(ctx, "settings.update", {"updates": {"features": {"enable_download": False}}})
    assert ctx.config.features.enable_download is False
    again = dispatch(ctx, "archive.download", {"source_id": "src_missing"})
    assert "enable_download" in again["reason"]


@pytest.mark.parametrize("value", ["true", 1, 0, None, "yes", []])
def test_update_rejects_non_bool_download_flag(
    ctx: AppContext, config_file: Path, value: object
) -> None:
    """开关只收真 bool：字符串 `"true"` / 数字 1 / null 一律 `-32602`，且**不写盘**。"""
    before = config_file.read_bytes()
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "settings.update", {"updates": {"features": {"enable_download": value}}})
    assert exc.value.code == -32602, "非法值必须是参数错误，不能是内部错误"
    assert "true/false" in str(exc.value)
    assert config_file.read_bytes() == before, "校验失败却已经把配置写了一半"


@pytest.mark.parametrize("key", ["enable_dowload", "enable_download_any", "download"])
def test_update_still_rejects_unknown_download_keys(
    ctx: AppContext, config_file: Path, key: str
) -> None:
    """新增这个键**不许**把"拒绝未知键"这道门弄松（拼错的键要报错，不能静默不生效）。"""
    before = config_file.read_bytes()
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "settings.update", {"updates": {"features": {key: True}}})
    assert exc.value.code == -32602
    assert "不认识的设置项" in str(exc.value)
    assert config_file.read_bytes() == before
    assert not hasattr(ctx.config.features, key), f"config.Features 里冒出个 {key}"


def test_download_flag_is_listed_next_to_the_other_features(ctx: AppContext) -> None:
    """`config.get` 的 features 里与另外三个开关并列（缺一个就有界面读不到状态）。"""
    features = dispatch(ctx, "config.get", {})["features"]
    assert features["enable_download"] is ctx.config.features.enable_download
    assert {"enable_mineru", "enable_topic_map", "enable_route_plan"} <= set(features)
