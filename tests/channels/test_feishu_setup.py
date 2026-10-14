"""飞书接入向导（`channels/feishu_setup`）的桩验证：**无凭据、无网络**跑完整条流程。

对应 docs/18 §③/§⑤：官网入口真实可打开、launcher 只打印**模板**（不伪造设备码）、
密钥只写 `.env` 且不回显/不回读、配对成功写白名单并收回临时放行、自检清单打印。

**连接方式固定为 websocket 长连接**（2026-10-14 用户拍板改走 A 方案）：向导里那条"连接模式二选一"
已随 webhook 路线删除，所以这里既不喂也不断言模式选项，改而钉住"打印长连接说明 + 不需要隧道"
与"**不再问** verification token / encrypt key"（后者曾是 webhook 的必填项）。
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from scholarscout.channels import feishu, feishu_setup, feishu_ws
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import pairing
from scholarscout.tools import register_tools

from ..conftest import redirect_home

#: 向导会直接写 `os.environ`（不经 monkeypatch），所以每个用例进出都要手动清干净。
#: 注意这里**没有** `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY` —— 那两个键随 webhook 一起删了，
#: 留着会让"向导不再问它们"这条断言失去意义。
FEISHU_ENV_KEYS = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_DOMAIN",
    "FEISHU_CONNECTION_MODE",
    "FEISHU_GROUP_POLICY",
    "FEISHU_HOME_CHANNEL",
    "FEISHU_DM_POLICY",
)

#: 走默认分支（回车）时的 ask 脚本：方式/域名/开浏览器/AppID/DM/openid/群聊/home
#: openid 用**实测的形状**（`ou_` + 32 位小写字母数字，中间没有下划线）
OWNER_OPEN_ID = "ou_aa206fb174402ae72345a7fe39a51ae2"
_DEFAULT_ASK = ["", "", "", "cli_test_app", "", OWNER_OPEN_ID, "", ""]
#: 只剩 App Secret 一处密文输入（verification token / encrypt key 已删）
_DEFAULT_SECRET = ["sec_test_123"]


@pytest.fixture(autouse=True)
def _clean_feishu_env(monkeypatch: pytest.MonkeyPatch):
    for key in FEISHU_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in FEISHU_ENV_KEYS:
        os.environ.pop(key, None)


@pytest.fixture
def wizard_ctx(ctx: AppContext, tmp_path, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    """把落盘根重定向到 tmp（`.env` 不写进仓库）。"""
    redirect_home(monkeypatch, tmp_path)
    register_tools()
    return ctx


def _scripted(answers: list[str]):
    seq = list(answers)

    def ask(prompt: str) -> str:
        if not seq:
            raise AssertionError(f"输入脚本用完了，却又问：{prompt}")
        return seq.pop(0)

    return ask


def test_wizard_happy_path_writes_env_and_pairs(wizard_ctx: AppContext, tmp_path) -> None:
    opened: list[str] = []
    out: list[str] = []
    rc = feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted(_DEFAULT_ASK),
        ask_secret=_scripted(_DEFAULT_SECRET),
        say=out.append,
        open_url=lambda url: (opened.append(url), True)[1],
    )
    text = "\n".join(out)
    assert rc == 0

    # 官网入口真实存在、且被（可选）打开；launcher 只给模板
    assert "https://open.feishu.cn" in text
    assert opened == ["https://open.feishu.cn"]
    assert "page/launcher?user_code=<USER_CODE>" in text

    # 密钥只写 .env、不回显；连接方式固定 websocket 长连接（不再有模式选择）
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "FEISHU_APP_ID=cli_test_app" in env
    assert "FEISHU_APP_SECRET=sec_test_123" in env
    assert "FEISHU_CONNECTION_MODE=websocket" in env
    assert "FEISHU_VERIFICATION_TOKEN" not in env and "FEISHU_ENCRYPT_KEY" not in env
    assert "FEISHU_DM_POLICY=pairing" in env
    assert "FEISHU_GROUP_POLICY=mention" in env
    assert "sec_test_123" not in text, "App Secret 被回显了"

    # 长连接那一段：明说不需要公网地址/回调/隧道，且给出 SDK 自检结论
    assert "websocket 长连接" in text
    assert "不需要" in text and "隧道" in text
    assert "长连接 SDK" in text

    # 配对成功：写白名单 + 收回临时放行
    assert pairing.is_approved(wizard_ctx.db, OWNER_OPEN_ID) is True
    assert pairing.temp_open(wizard_ctx.db) is False
    assert f"✓ 配对成功：{OWNER_OPEN_ID}" in text

    # 完成摘要 + 自检清单，且摘要里没有 Secret
    assert "✓ 飞书接入已配置！" in text
    assert "App ID: cli_test_app" in text
    assert "—— 自检 ——" in text
    assert "✓ 凭据已写入 .env" in text


def test_status_never_reads_the_secret_back(wizard_ctx: AppContext) -> None:
    out: list[str] = []
    feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted(_DEFAULT_ASK),
        ask_secret=_scripted(_DEFAULT_SECRET),
        say=out.append,
        open_url=lambda url: True,
    )
    status = dispatch(wizard_ctx, "feishu.status", {})
    assert status["app_id_set"] is True and status["secret_set"] is True
    blob = json.dumps(status, ensure_ascii=False)
    assert "sec_test_123" not in blob
    assert "cli_test_app" not in blob, "接口只报布尔，不回传 App ID/Secret 的值"


def test_existing_config_can_decline_reconfigure(
    wizard_ctx: AppContext, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("FEISHU_APP_ID", "cli_existing")
    out: list[str] = []
    rc = feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted([""]),  # 回车 = 不重新配置
        ask_secret=_scripted([]),
        say=out.append,
        open_url=lambda url: True,
    )
    assert rc == 0
    assert "保留现有配置" in "\n".join(out)
    assert not (tmp_path / ".env").exists()


def test_missing_app_id_aborts_with_code_2(wizard_ctx: AppContext) -> None:
    out: list[str] = []
    rc = feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted(["", "", "", ""]),  # 方式/域名/开浏览器/AppID=空
        ask_secret=_scripted([]),
        say=out.append,
        open_url=lambda url: True,
    )
    assert rc == 2
    assert "没有 App ID" in "\n".join(out)


def test_auto_claim_uses_channel_captured_openid_without_asking(wizard_ctx: AppContext) -> None:
    """渠道层先捕获到 openid（调 claim）时，向导不该再问用户。"""
    code = dispatch(wizard_ctx, "feishu.pairing.create", {})["code"]
    assert (
        dispatch(wizard_ctx, "feishu.pairing.claim", {"code": code, "open_id": "ou_auto"})[
            "claimed"
        ]
        is True
    )
    out: list[str] = []

    def ask(prompt: str) -> str:
        raise AssertionError("渠道层已自动捕获 openid，不该再问用户")

    open_id = feishu_setup._obtain_openid(
        ask=ask, say=out.append, ctx=wizard_ctx, dispatch_fn=dispatch, code=code
    )
    assert open_id == "ou_auto"
    assert any("自动捕获" in line for line in out)


def test_openid_with_underscore_is_not_truncated(wizard_ctx: AppContext) -> None:
    """openid 若含下划线，正则**不许在下划线处截断**（截断 = 把错的 id 写进白名单）。

    实测形状（`ou_` + 32 位小写字母数字）不含下划线；此处故意造含下划线的 id，
    钉住 `feishu_setup._OPEN_ID_RE` 的取舍：宁可多认一个下划线，也不静默绑定截短的错身份。
    """
    code = dispatch(wizard_ctx, "feishu.pairing.create", {})["code"]
    full = "ou_ab_2024_xyz"
    out: list[str] = []
    open_id = feishu_setup._obtain_openid(
        ask=_scripted([f"机器人说你的 open_id 是 {full} ，请查收"]),
        say=out.append,
        ctx=wizard_ctx,
        dispatch_fn=dispatch,
        code=code,
    )
    assert open_id == full  # 整串，而不是被截断的 "ou_ab"
    pending = dispatch(wizard_ctx, "feishu.pairing.list", {})["pending"]
    assert [row["user_id"] for row in pending] == [full]  # 绑上的是完整 id


# ------------------------------------------------------------------ 纯函数/常量


def test_launcher_url_template_and_entry_urls() -> None:
    url = feishu.launcher_url("Z2BC-3GAP")
    assert (
        url
        == "https://open.feishu.cn/page/launcher?user_code=Z2BC-3GAP&from=scholarscout&tp=scholarscout"
    )
    assert feishu.launcher_url("X", from_="me", tp="me") == (
        "https://open.feishu.cn/page/launcher?user_code=X&from=me&tp=me"
    )
    assert feishu.launcher_url("X", domain="lark").startswith("https://open.larksuite.com/")
    assert feishu.open_entry_url() == "https://open.feishu.cn"
    assert feishu.open_entry_url("lark") == "https://open.larksuite.com"
    # 参数名与参照体验一致（from / tp），值可配置
    assert "&from=" in feishu.LAUNCHER_URL_TEMPLATE and "&tp=" in feishu.LAUNCHER_URL_TEMPLATE


def test_wizard_never_asks_for_webhook_only_secrets(wizard_ctx: AppContext) -> None:
    """**不再问** verification token / encrypt key。

    那两个键是 webhook 路线的必填项，随 webhook 一起删了。这里只喂一个密文值：
    向导若还问第二处就该当场炸（`ask_secret` 脚本用完即报错），而**不是**静默多问。
    """
    seen: list[str] = []
    secrets = list(_DEFAULT_SECRET)
    out: list[str] = []

    def ask_secret(prompt: str) -> str:
        seen.append(prompt)
        if not secrets:
            raise AssertionError(f"又问了第二处密文（webhook 的键应已删除）：{prompt}")
        return secrets.pop(0)

    rc = feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted(_DEFAULT_ASK),
        ask_secret=ask_secret,
        say=out.append,
        open_url=lambda url: True,
    )
    text = "\n".join(out)
    assert rc == 0
    assert len(seen) == 1 and "App Secret" in seen[0], seen
    assert not [p for p in seen if "Verification" in p or "Encrypt" in p]
    assert "Verification Token" not in text and "Encrypt Key" not in text


def test_wizard_flags_missing_sdk_with_next_step(
    wizard_ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SDK 缺装时**不假装能跑**：向导标 ✗ 并给出下一步命令，其余流程照常跑完。

    桩掉 `feishu_ws.probe_sdk` 是为了让这条断言与"本机装没装 SDK"无关（门禁机器上它装着）。
    """
    note = "未安装 lark_oapi（PyPI 包名 lark-oapi）：先跑 `python scripts/install_deps.py`"
    monkeypatch.setattr(feishu_ws, "probe_sdk", lambda **kwargs: (False, note))
    out: list[str] = []
    rc = feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted(_DEFAULT_ASK),
        ask_secret=_scripted(_DEFAULT_SECRET),
        say=out.append,
        open_url=lambda url: True,
    )
    text = "\n".join(out)
    assert rc == 0  # 缺 SDK 不阻塞向导（凭据/白名单该落的照落）
    assert "✗ 长连接 SDK" in text and "lark-oapi" in text
    assert "install_deps.py" in text
    assert "✓ 长连接 SDK" not in text


def test_connection_mode_and_group_policy_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEISHU_CONNECTION_MODE", "websocket")
    assert feishu.connection_mode_from_env() == "websocket"
    monkeypatch.setenv("FEISHU_CONNECTION_MODE", "nonsense")
    assert feishu.connection_mode_from_env() == feishu.DEFAULT_CONNECTION_MODE
    monkeypatch.setenv("FEISHU_GROUP_POLICY", "disabled")
    assert feishu.group_policy_from_env() == "disabled"


def test_registration_from_tp_configurable_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(feishu.REGISTRATION_FROM_ENV, raising=False)
    monkeypatch.delenv(feishu.REGISTRATION_TP_ENV, raising=False)
    assert feishu.registration_from_env() == feishu.DEFAULT_REGISTRATION_FROM
    assert feishu.registration_tp_from_env() == feishu.DEFAULT_REGISTRATION_TP
    monkeypatch.setenv(feishu.REGISTRATION_FROM_ENV, "myapp")
    monkeypatch.setenv(feishu.REGISTRATION_TP_ENV, "mytp")
    assert feishu.registration_from_env() == "myapp"
    assert feishu.registration_tp_from_env() == "mytp"


# ------------------------------------------------------------------ 联网自动建应用（docs/18 §③）


def _wrap_dispatch(overrides: dict[str, Any]):
    """把指定的 RPC 换成桩，其余照走真 dispatch。"""

    def call(ctx: AppContext, name: str, params: dict[str, Any] | None = None):
        if name in overrides:
            return overrides[name]
        return dispatch(ctx, name, params)

    return call


def test_auto_create_success_skips_manual_credential_prompts(
    wizard_ctx: AppContext, tmp_path
) -> None:
    """联网建应用成功时：不再问 App ID / App Secret，摘要直接给出 App ID。"""
    overrides = {
        "feishu.onboard.begin": {
            "device_code": "dc",
            "qr_url": "https://open.feishu.cn/page/launcher?user_code=Z2BC-3GAP&from=scholarscout&tp=scholarscout",
            "user_code": "Z2BC-3GAP",
            "interval": 5,
            "expire_in": 600,
        },
        "feishu.onboard.poll": {
            "ok": True,
            "app_id": "cli_auto",
            "domain": "feishu",
            "open_id": "ou_x",
        },
    }
    out: list[str] = []

    def ask_secret(prompt: str) -> str:
        raise AssertionError(f"自动建应用成功后不该再问密钥：{prompt}")

    rc = feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted(
            ["", "", "", OWNER_OPEN_ID, "", ""]
        ),  # 方式/域名/DM/openid/群聊/home（自动分支不问浏览器、不问 AppID）
        ask_secret=ask_secret,
        say=out.append,
        open_url=lambda url: True,
        dispatch_fn=_wrap_dispatch(overrides),
    )
    text = "\n".join(out)
    assert rc == 0
    assert "✓ 已自动创建并写入凭据（App ID: cli_auto" in text
    assert "现在用浏览器打开官网入口？" not in text  # 走了自动分支，没有手工入口提示
    assert "App ID: cli_auto" in text
    # 向导把非密钥字段落盘（密钥由 RPC 写）
    assert "FEISHU_APP_ID=cli_auto" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_auto_create_failure_falls_back_to_manual(wizard_ctx: AppContext, tmp_path) -> None:
    """begin 失败（离线/无凭据）时：打印原因并转手工，向导照常跑完。"""
    from scholarscout.rpc.errors import Code, RpcError

    def boom(ctx, name, params=None):
        if name == "feishu.onboard.begin":
            raise RpcError(Code.SERVICE_UNAVAILABLE, "网络不通")
        return dispatch(ctx, name, params)

    out: list[str] = []
    rc = feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted(_DEFAULT_ASK),
        ask_secret=_scripted(_DEFAULT_SECRET),
        say=out.append,
        open_url=lambda url: True,
        dispatch_fn=boom,
    )
    text = "\n".join(out)
    assert rc == 0
    assert "转手工填凭据" in text
    assert "page/launcher?user_code=<USER_CODE>" in text  # 手工分支仍打印模板
    assert "FEISHU_APP_ID=cli_test_app" in (tmp_path / ".env").read_text(encoding="utf-8")


# ------------------------------------------------------------------ Bot 名探测（完成摘要，docs/18 §③④）


def test_bot_name_is_probed_and_shown_at_the_end(wizard_ctx: AppContext) -> None:
    """完成摘要里的 Bot 名来自联网探测（此处把 feishu.probe 打桩成真名）。"""
    out: list[str] = []
    rc = feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted(_DEFAULT_ASK),
        ask_secret=_scripted(_DEFAULT_SECRET),
        say=out.append,
        open_url=lambda url: True,
        dispatch_fn=_wrap_dispatch(
            {
                "feishu.probe": {
                    "ok": True,
                    "bot_name": "大浩的AI毕设",
                    "bot_open_id": "ou_54e",
                    "token_expire": 7200,
                    "domain": "feishu",
                    "reason": "ok",
                }
            }
        ),
    )
    text = "\n".join(out)
    assert rc == 0
    assert "Bot: 大浩的AI毕设" in text
    assert "✓ 机器人名 大浩的AI毕设" in text  # 自检里也带机器人名


def test_bot_name_falls_back_to_unknown_without_blocking(wizard_ctx: AppContext) -> None:
    """探测抛异常也不能中断向导：回退 unknown 且照常完成（**非阻塞**）。"""

    def boom(ctx, name, params=None):
        if name == "feishu.probe":
            raise RuntimeError("network down")
        return dispatch(ctx, name, params)

    out: list[str] = []
    rc = feishu_setup.run_setup(
        wizard_ctx,
        ask=_scripted(_DEFAULT_ASK),
        ask_secret=_scripted(_DEFAULT_SECRET),
        say=out.append,
        open_url=lambda url: True,
        dispatch_fn=boom,
    )
    text = "\n".join(out)
    assert rc == 0
    assert "Bot: unknown" in text
    assert "✓ 飞书接入已配置！" in text
