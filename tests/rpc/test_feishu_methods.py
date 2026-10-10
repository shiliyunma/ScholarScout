"""`feishu.*` RPC 契约（docs/01 §3.8）。"""

from __future__ import annotations

import json
import os

import pytest

from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import pairing

from ..conftest import redirect_home

_ENV_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_CONNECTION_MODE",
    "FEISHU_DM_POLICY",
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """`.env` 重定向到 tmp；进出都清干净渠道层环境变量（RPC 会直接写 os.environ）。"""
    redirect_home(monkeypatch, tmp_path)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in _ENV_KEYS:
        os.environ.pop(key, None)


def test_status_shape_and_no_secret_value(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    status = dispatch(ctx, "feishu.status", {})
    assert status["configured"] is True
    assert status["secret_set"] is True
    assert status["bot_name"] is None  # P0 不联网探测 → 明确 None
    assert status["allowlist"] == []
    assert "sec_x" not in json.dumps(status, ensure_ascii=False)


def test_create_claim_verify_flow(ctx: AppContext) -> None:
    created = dispatch(ctx, "feishu.pairing.create", {})
    code = created["code"]
    assert len(code) == pairing.CODE_LENGTH
    assert created["temp_open"] is True
    assert (
        dispatch(ctx, "feishu.pairing.claim", {"code": code, "open_id": "ou_a"})["claimed"] is True
    )
    result = dispatch(ctx, "feishu.pairing.verify", {"code": code})
    assert result["ok"] is True and result["user_id"] == "ou_a"
    assert result["temp_open"] is False
    assert [row["user_id"] for row in result["allowlist"]] == ["ou_a"]


def test_verify_wrong_code_is_invalid_params(ctx: AppContext) -> None:
    dispatch(ctx, "feishu.pairing.create", {})
    with pytest.raises(RpcError) as info:
        dispatch(ctx, "feishu.pairing.verify", {"code": "ZZZZZZZZ"})
    assert info.value.code == -32602


def test_verify_unclaimed_hints_about_openid(ctx: AppContext) -> None:
    code = dispatch(ctx, "feishu.pairing.create", {})["code"]
    with pytest.raises(RpcError) as info:
        dispatch(ctx, "feishu.pairing.verify", {"code": code})
    assert "open_id" in str(info.value)


def test_create_refuses_when_pending_full(ctx: AppContext) -> None:
    for _ in range(pairing.MAX_PENDING):
        dispatch(ctx, "feishu.pairing.create", {})
    with pytest.raises(RpcError):
        dispatch(ctx, "feishu.pairing.create", {})


def test_claim_requires_both_params(ctx: AppContext) -> None:
    with pytest.raises(RpcError):
        dispatch(ctx, "feishu.pairing.claim", {"code": "ABCD2345"})


def test_claim_wrong_code_returns_false_not_error(ctx: AppContext) -> None:
    dispatch(ctx, "feishu.pairing.create", {})
    assert (
        dispatch(ctx, "feishu.pairing.claim", {"code": "ZZZZZZZZ", "open_id": "ou_a"})["claimed"]
        is False
    )


def test_list_and_revoke(ctx: AppContext) -> None:
    code = dispatch(ctx, "feishu.pairing.create", {})["code"]
    dispatch(ctx, "feishu.pairing.claim", {"code": code, "open_id": "ou_b"})
    dispatch(ctx, "feishu.pairing.verify", {"code": code})
    listing = dispatch(ctx, "feishu.pairing.list", {})
    assert [row["user_id"] for row in listing["approved"]] == ["ou_b"]
    assert listing["pending"] == []
    assert dispatch(ctx, "feishu.pairing.revoke", {"user_id": "ou_b"})["removed"] is True
    assert dispatch(ctx, "feishu.status", {})["allowlist"] == []


def test_dm_policy_validates_and_persists(ctx: AppContext) -> None:
    with pytest.raises(RpcError):
        dispatch(ctx, "feishu.dm_policy", {"policy": "nope"})
    assert dispatch(ctx, "feishu.dm_policy", {"policy": "all"})["policy"] == "all"
    assert dispatch(ctx, "feishu.status", {})["dm_policy"] == "all"


# ------------------------------------------------------------------ 联网自动建应用（docs/18 §③）


def _stub_http(handler) -> object:
    import httpx

    from scholarscout.fetch.http import HttpClient

    return HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))


def test_onboard_begin_requires_an_http_client(ctx: AppContext) -> None:
    """没有出网客户端就如实报「未配置」，而不是偷偷直连 httpx。"""
    with pytest.raises(RpcError) as info:
        dispatch(ctx, "feishu.onboard.begin", {})
    assert info.value.code == -32000  # NOT_CONFIGURED


def test_onboard_begin_rejects_unknown_domain(ctx: AppContext) -> None:
    ctx.http = _stub_http(lambda request: None)  # type: ignore[assignment]
    with pytest.raises(RpcError):
        dispatch(ctx, "feishu.onboard.begin", {"domain": "mars"})


def test_onboard_begin_returns_server_issued_url(ctx: AppContext) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "device_code": "dc",
                "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=Z2BC-3GAP",
                "user_code": "Z2BC-3GAP",
                "interval": 5,
                "expire_in": 600,
            },
        )

    ctx.http = _stub_http(handler)  # type: ignore[assignment]
    begin = dispatch(ctx, "feishu.onboard.begin", {})
    assert begin["device_code"] == "dc"
    assert begin["qr_url"].endswith("&from=scholarscout&tp=scholarscout")


def test_onboard_poll_writes_credentials_but_never_returns_the_secret(
    ctx: AppContext, tmp_path
) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "client_id": "cli_auto",
                "client_secret": "sec_auto_should_not_leak",
                "user_info": {"open_id": "ou_auto"},
            },
        )

    ctx.http = _stub_http(handler)  # type: ignore[assignment]
    result = dispatch(ctx, "feishu.onboard.poll", {"device_code": "dc"})
    assert result["ok"] is True and result["app_id"] == "cli_auto"
    # 密钥只写 .env、绝不回传
    blob = json.dumps(result, ensure_ascii=False)
    assert "sec_auto_should_not_leak" not in blob
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "FEISHU_APP_ID=cli_auto" in env
    assert "FEISHU_APP_SECRET=sec_auto_should_not_leak" in env


def test_onboard_poll_requires_device_code(ctx: AppContext) -> None:
    ctx.http = _stub_http(lambda request: None)  # type: ignore[assignment]
    with pytest.raises(RpcError):
        dispatch(ctx, "feishu.onboard.poll", {})


# ------------------------------------------------------------------ Bot 名探测（feishu.probe，docs/01 §3.8）

_TOKEN_OK = {"code": 0, "msg": "ok", "tenant_access_token": "tok-" + "a" * 38, "expire": 7200}
_BOT_OK = {"code": 0, "msg": "ok", "bot": {"app_name": "大浩的AI毕设", "open_id": "ou_54eabc"}}


def test_probe_without_credentials_is_non_blocking(ctx: AppContext) -> None:
    """凭据没配也要回结构完整的 unknown，而不是抛异常（自检/向导不该被中断）。"""
    result = dispatch(ctx, "feishu.probe", {})
    assert result["ok"] is False
    assert result["bot_name"] == "unknown"
    assert result["token_expire"] == 0
    assert "凭据未配置" in result["reason"]


def test_probe_without_http_client_is_non_blocking(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    result = dispatch(ctx, "feishu.probe", {})
    assert result["ok"] is False and result["bot_name"] == "unknown"
    assert "出网客户端未装配" in result["reason"]


def test_probe_returns_bot_name_and_never_leaks_the_token(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json=_TOKEN_OK)
        return httpx.Response(200, json=_BOT_OK)

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    ctx.http = _stub_http(handler)  # type: ignore[assignment]
    result = dispatch(ctx, "feishu.probe", {})
    assert result["ok"] is True
    assert result["bot_name"] == "大浩的AI毕设"  # 名取 bot.app_name
    assert result["bot_open_id"] == "ou_54eabc"  # 自身 id 取 bot.open_id
    assert result["token_expire"] == 7200
    blob = json.dumps(result, ensure_ascii=False)
    assert str(_TOKEN_OK["tenant_access_token"]) not in blob, "token 值被回传了"
    assert "sec_x" not in blob


def test_probe_treats_nonzero_code_as_unknown(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 99991, "msg": "bad"})

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    ctx.http = _stub_http(handler)  # type: ignore[assignment]
    result = dispatch(ctx, "feishu.probe", {})
    assert result["ok"] is False and result["bot_name"] == "unknown"


def test_probe_swallows_transport_errors(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setenv("FEISHU_APP_ID", "cli_x")
    monkeypatch.setenv("FEISHU_APP_SECRET", "sec_x")
    ctx.http = _stub_http(handler)  # type: ignore[assignment]
    result = dispatch(ctx, "feishu.probe", {})
    assert result["ok"] is False and result["bot_name"] == "unknown"


def test_probe_rejects_unknown_domain(ctx: AppContext) -> None:
    with pytest.raises(RpcError):
        dispatch(ctx, "feishu.probe", {"domain": "mars"})
