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
