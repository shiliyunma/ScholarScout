"""飞书设备码注册的桩测：**不真联网**（在 `fetch/http.py` 那一层用 MockTransport 打桩）。

覆盖 docs/18 §③「联网自动建应用 + 轮询」：
- 发起注册的参数与端点正确（表单、动作、from/tp 可配置）；
- 轮询：未就绪 → 退避后重试 → 成功拿到凭据；用户取消 → 放弃；超时 → 返回 None（**不死循环**）。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from scholarscout.fetch import feishu_onboard
from scholarscout.fetch.http import HttpClient

REGISTRATION_URL = "https://accounts.feishu.cn/oauth/v1/app/registration"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    clock: FakeClock,
    slept: list[float],
) -> HttpClient:
    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    return HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=sleep,
    )


def _advancing_sleep(clock: FakeClock, slept: list[float]) -> Callable[[float], None]:
    """注入给 `poll_registration` 的 sleep：记录 + 推进假时钟（测试不真等）。"""

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    return sleep


# ------------------------------------------------------------------ begin / init


def test_begin_posts_exact_form_and_builds_qr_url() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={
                "device_code": "dc_123",
                "verification_uri_complete": (
                    "https://open.feishu.cn/page/launcher?user_code=Z2BC-3GAP"
                ),
                "user_code": "Z2BC-3GAP",
                "interval": 5,
                "expire_in": 600,
            },
        )

    clock = FakeClock()
    begin = feishu_onboard.begin_registration(make_client(handler, clock=clock, slept=[]))

    assert seen["url"] == REGISTRATION_URL
    assert seen["method"] == "POST"
    assert str(seen["content_type"]).startswith("application/x-www-form-urlencoded")
    # 表单参数逐条对齐 Hermes `_begin_registration`（adapter.py:3931-3933）
    assert seen["body"] == (
        "action=begin&archetype=PersonalAgent&auth_method=client_secret&request_user_info=open_id"
    )
    assert begin.device_code == "dc_123"
    assert begin.user_code == "Z2BC-3GAP"
    assert begin.interval == 5 and begin.expire_in == 600
    # 追加 from/tp（照 adapter.py:3937-3938），默认 scholarscout
    assert begin.qr_url.endswith("&from=scholarscout&tp=scholarscout")


def test_begin_from_tp_are_configurable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "device_code": "dc",
                "verification_uri_complete": "https://open.larksuite.com/x?user_code=AB-12",
            },
        )

    clock = FakeClock()
    begin = feishu_onboard.begin_registration(
        make_client(handler, clock=clock, slept=[]), domain="lark", from_="me", tp="you"
    )
    assert begin.qr_url == "https://open.larksuite.com/x?user_code=AB-12&from=me&tp=you"


def test_begin_without_device_code_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"verification_uri_complete": "https://x"})

    clock = FakeClock()
    with pytest.raises(feishu_onboard.OnboardError):
        feishu_onboard.begin_registration(make_client(handler, clock=clock, slept=[]))


def test_init_returns_supported_auth_methods() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content.decode() == "action=init"
        return httpx.Response(200, json={"supported_auth_methods": ["client_secret", "pkce"]})

    clock = FakeClock()
    methods = feishu_onboard.init_registration(make_client(handler, clock=clock, slept=[]))
    assert methods == ["client_secret", "pkce"]


# ------------------------------------------------------------------ poll


def test_poll_backs_off_on_pending_then_succeeds() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        # 飞书把"还没授权"当 400 返回（body 带 JSON），照此合造
        if len(calls) < 3:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={"client_id": "cli_x", "client_secret": "sec_x", "user_info": {"open_id": "ou_1"}},
        )

    clock = FakeClock()
    slept: list[float] = []
    result = feishu_onboard.poll_registration(
        make_client(handler, clock=clock, slept=slept),
        device_code="dc",
        interval=2,
        expire_in=60,
        clock=clock,
        sleep=_advancing_sleep(clock, slept),
    )
    assert result is not None
    assert (result.app_id, result.app_secret, result.open_id) == ("cli_x", "sec_x", "ou_1")
    assert len(calls) == 3
    # 退避：首次 interval=2，第二次翻倍 4
    assert [round(x) for x in slept] == [2, 4]


def test_poll_switches_domain_to_lark() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "client_id": "cli_l",
                "client_secret": "sec_l",
                "user_info": {"tenant_brand": "lark", "open_id": "ou_l"},
            },
        )

    clock = FakeClock()
    result = feishu_onboard.poll_registration(
        make_client(handler, clock=clock, slept=[]),
        device_code="dc",
        clock=clock,
        sleep=lambda _seconds: None,
    )
    assert result is not None and result.domain == "lark"


def test_poll_returns_none_on_access_denied() -> None:
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "access_denied"})

    clock = FakeClock()
    result = feishu_onboard.poll_registration(
        make_client(handler, clock=clock, slept=slept),
        device_code="dc",
        clock=clock,
        sleep=slept.append,
    )
    assert result is None
    assert slept == []  # 直接放弃，不再等


def test_poll_times_out_without_infinite_loop() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": "authorization_pending"})

    clock = FakeClock()
    slept: list[float] = []
    result = feishu_onboard.poll_registration(
        make_client(handler, clock=clock, slept=slept),
        device_code="dc",
        interval=3,
        expire_in=10,
        max_interval_s=3.0,
        clock=clock,
        sleep=_advancing_sleep(clock, slept),
    )
    assert result is None
    assert len(calls) == 4  # 有界：每次 sleep 前进 3，10 秒内最多 4 次
    assert clock() >= 10


def test_poll_keeps_pending_on_400_body_without_error_key() -> None:
    """400 但 body 没写 error 字段也当\"还没好\"（4xx 不解析 body 的取舍，见模块顶注）。"""
    seq = ["pending", "ok"]

    def handler(request: httpx.Request) -> httpx.Response:
        if seq.pop(0) == "pending":
            return httpx.Response(400, text="not json")
        return httpx.Response(200, json={"client_id": "c", "client_secret": "s"})

    clock = FakeClock()
    slept: list[float] = []
    result = feishu_onboard.poll_registration(
        make_client(handler, clock=clock, slept=slept),
        device_code="dc",
        interval=1,
        expire_in=60,
        clock=clock,
        sleep=_advancing_sleep(clock, slept),
    )
    assert result is not None and result.app_id == "c"


def test_registration_endpoint_per_domain() -> None:
    assert feishu_onboard.registration_endpoint("feishu") == REGISTRATION_URL
    assert (
        feishu_onboard.registration_endpoint("lark")
        == "https://accounts.larksuite.com/oauth/v1/app/registration"
    )
    # 未知域名回退到 feishu（不抛，避免向导因一个错字整个挂掉）
    assert feishu_onboard.registration_endpoint("nope") == REGISTRATION_URL


# ------------------------------------------------------------------ Bot 名探测（已实测，docs/18 §④）

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
BOT_URL = "https://open.feishu.cn/open-apis/bot/v3/info"
#: 实测回包形状（用户在本机真机跑出来的）：换 token 回 tenant_access_token + expire
_TOKEN_OK = {"code": 0, "msg": "ok", "tenant_access_token": "t-" + "a" * 40, "expire": 7200}
_BOT_OK = {"code": 0, "msg": "ok", "bot": {"app_name": "大浩的AI毕设", "open_id": "ou_54eabc"}}


def _probe_client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    return make_client(handler, clock=FakeClock(), slept=[])


def test_probe_bot_sends_exact_requests_and_parses_the_real_shape() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "method": request.method,
                "auth": request.headers.get("authorization", ""),
                "body": request.content.decode("utf-8"),
                "content_type": request.headers.get("content-type", ""),
            }
        )
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=_TOKEN_OK)
        return httpx.Response(200, json=_BOT_OK)

    probe = feishu_onboard.probe_bot(_probe_client(handler), app_id="cli_x", app_secret="sec_x")
    assert probe is not None
    assert probe.app_name == "大浩的AI毕设"  # 名字取 bot.app_name
    assert probe.open_id == "ou_54eabc"  # bot 自身 id 取 bot.open_id
    assert probe.token_expire == 7200  # 有效期来自换 token 的 expire

    token_call, bot_call = seen  # 先换 token 再打 bot/v3/info
    assert token_call["url"] == TOKEN_URL and token_call["method"] == "POST"
    assert str(token_call["content_type"]).startswith("application/json")
    assert json.loads(str(token_call["body"])) == {"app_id": "cli_x", "app_secret": "sec_x"}
    assert bot_call["url"] == BOT_URL and bot_call["method"] == "GET"
    assert bot_call["auth"] == "Bearer " + str(_TOKEN_OK["tenant_access_token"])


def test_probe_bot_caps_retries_when_token_code_is_nonzero() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"code": 99991, "msg": "bad credentials"})

    probe = feishu_onboard.probe_bot(
        _probe_client(handler), app_id="cli", app_secret="sec", retries=2
    )
    assert probe is None
    # code!=0 算失败：重试 ≤2 => 至多 3 次换 token；且**不**去打 bot/v3/info
    assert calls == [TOKEN_URL] * feishu_onboard.PROBE_MAX_ATTEMPTS


def test_probe_bot_nonzero_code_on_bot_info_is_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json=_TOKEN_OK)
        return httpx.Response(200, json={"code": 99991, "msg": "no bot"})

    probe = feishu_onboard.probe_bot(_probe_client(handler), app_id="a", app_secret="s", retries=0)
    assert probe is None


def test_probe_bot_returns_none_when_token_missing_in_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "expire": 7200})

    probe = feishu_onboard.probe_bot(_probe_client(handler), app_id="a", app_secret="s", retries=0)
    assert probe is None


def test_probe_bot_swallows_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    # 非阻塞：连接错误不外抛，只回 None
    probe = feishu_onboard.probe_bot(_probe_client(handler), app_id="a", app_secret="s", retries=1)
    assert probe is None


def test_bot_endpoints_per_domain() -> None:
    assert feishu_onboard.tenant_token_endpoint("feishu") == TOKEN_URL
    assert feishu_onboard.bot_info_endpoint("feishu") == BOT_URL
    assert feishu_onboard.tenant_token_endpoint("lark").startswith("https://open.larksuite.com/")
    assert feishu_onboard.bot_info_endpoint("lark").startswith("https://open.larksuite.com/")
    # 未知域名回退 feishu（与 registration_endpoint 同取舍）
    assert feishu_onboard.bot_info_endpoint("nope") == BOT_URL
