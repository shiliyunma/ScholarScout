"""飞书渠道回调（入口层，docs/17 §3）。

## 它在哪一层

和 `desktop/` 同级：都是"外面来一条消息 → 汇进去跑一轮"的前门。所以它**只准走 `rpc/`**
（进程内 `dispatch`），不读库、不出网、不 import `runtime/tools/state/...`
—— 由 `tests/test_layering.py::RULES["channels"]` 钉死。

## 它做四件事

1. 用标准库 `http.server` 起一个回调监听（**默认关**，见 `config.runtime.feishu_listener_*`）；
2. 解析飞书事件回调 JSON、处理 URL 验证 `challenge`；
3. 两层校验（verification token + 签名），密钥只从 `.env` 读；**没有密钥时不校签、明确告警**；
4. 把消息变成一次会话请求（`session.ensure` + `chat.send`），把回复交给**注入的发送器**
   （真实部署注入走飞书 REST 的发送器；本地桩回调注入记录器）。

## 接口契约（来自 Hermes 适配器，逐条带 `文件:行号`，不凭印象）

`plugins/platforms/feishu/adapter.py`（本机副本，4308 行）：
- 只收 `application/json`（`:2679-2682`）；
- body 上限 1 MB（`:155 / :2685-2687`）；
- 限速：`app_id:path:remote_ip` 滑动窗口 60s/120 次（`:156-157 / :2776-2785`）；
- verification token：`payload["header"]["token"]` 或 `payload["token"]`，`hmac.compare_digest`（`:2708-2717`）；
- URL 验证：`type == "url_verification"` **先过 token** 再回 `{"challenge": ...}`（`:2719-2722`）；
- 签名：`x-lark-signature == sha256(timestamp + nonce + encrypt_key + body)`（`:2759-2771`）；
- 成功回包 `{"code": 0, "msg": "ok"}`（`:2746`）；拒绝回 401（`:2716-2717 / :2725-2726`）。

**回包格式上的唯一差异**（本模块有意为之）：Hermes 的拒绝回**纯文本**，这里统一回 JSON
（`{"code":<status>,"msg":...}`），便于校验与单测；**状态码与判据逐条对齐**。

## 本地自测

不联网、无凭据即可跑通整条链路：见 `tests/channels/test_feishu_stub.py`
（起真 `ThreadingHTTPServer` → POST 一份合造的飞书回调 → 断言进会话路径、回包格式、
非法签名 401、challenge 回显）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlparse

from scholarscout.rpc.methods import AppContext, dispatch

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 常量（对齐 Hermes）

#: 回调路径（Hermes 默认 `/feishu/webhook`，adapter.py:151）
DEFAULT_PATH = "/feishu/webhook"
#: body 上限 1 MB（Hermes `_FEISHU_WEBHOOK_MAX_BODY_BYTES`，adapter.py:155）
MAX_BODY_BYTES = 1 * 1024 * 1024
#: 限速滑动窗口：60s / 120 次（Hermes，adapter.py:156-157）
RATE_WINDOW_SECONDS = 60
RATE_LIMIT_MAX = 120
#: 成功回包（Hermes，adapter.py:2746）
SUCCESS_BODY: dict[str, Any] = {"code": 0, "msg": "ok"}
#: 事件类型
RECEIVE_EVENT = "im.message.receive_v1"
CHALLENGE_EVENT = "url_verification"

Sender = Callable[[str, str], dict[str, Any]]
Dispatcher = Callable[[AppContext, str, Any], Any]


@dataclass(frozen=True)
class FeishuCredentials:
    """飞书凭据（**只从 .env / 环境变量读**，绝不硬编码、绝不入库）。"""

    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""

    @property
    def has_webhook_secret(self) -> bool:
        """webhook 模式至少要一个密钥才能校签（照 Hermes adapter.py:1417-1419）。"""
        return bool(self.verification_token or self.encrypt_key)

    @property
    def can_send(self) -> bool:
        """能否走飞书 REST 发消息（要有 app_id + app_secret）。"""
        return bool(self.app_id and self.app_secret)


def credentials_from_env() -> FeishuCredentials:
    """从环境变量取凭据（`.env` 由 `config.load_dotenv` 加载进 `os.environ`）。"""
    return FeishuCredentials(
        app_id=os.environ.get("FEISHU_APP_ID", "").strip(),
        app_secret=os.environ.get("FEISHU_APP_SECRET", "").strip(),
        verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", "").strip(),
        encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY", "").strip(),
    )


@dataclass(frozen=True)
class FeishuMessage:
    """一条飞书文本消息（本模块只认文本，第一版够用）。"""

    chat_id: str
    text: str
    message_id: str
    sender_open_id: str


# ------------------------------------------------------------------ 纯函数（可单独测）


def compute_signature(timestamp: str, nonce: str, encrypt_key: str, body_bytes: bytes) -> str:
    """`sha256(timestamp + nonce + encrypt_key + body)`（照 Hermes adapter.py:2759-2771）。"""
    body_str = body_bytes.decode("utf-8", errors="replace")
    return hashlib.sha256(f"{timestamp}{nonce}{encrypt_key}{body_str}".encode()).hexdigest()


def signature_valid(headers: Mapping[str, str], body_bytes: bytes, encrypt_key: str) -> bool:
    """定时安全地比对 `x-lark-signature`（缺任一要素即 False，不抛异常；取头大小写无关）。"""
    lowered = {str(k).lower(): v for k, v in headers.items()}
    timestamp = str(lowered.get("x-lark-request-timestamp", "") or "")
    nonce = str(lowered.get("x-lark-request-nonce", "") or "")
    signature = str(lowered.get("x-lark-signature", "") or "")
    if not timestamp or not nonce or not signature:
        return False
    try:
        computed = compute_signature(timestamp, nonce, encrypt_key, body_bytes)
        return hmac.compare_digest(computed.encode(), signature.encode())
    except Exception:  # pragma: no cover - 防御性兜底
        return False


def token_valid(payload: Mapping[str, Any], verification_token: str) -> bool:
    """比对 body/header 里的 `token`（照 Hermes adapter.py:2708-2717）。"""
    header = payload.get("header")
    token = ""
    if isinstance(header, Mapping):
        token = str(header.get("token") or "")
    if not token:
        token = str(payload.get("token") or "")
    if not token:
        return False
    return hmac.compare_digest(token.encode(), verification_token.encode())


def is_challenge(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("type") or "") == CHALLENGE_EVENT


def challenge_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    """URL 验证的回包（照 Hermes adapter.py:2719-2722）。"""
    return {"challenge": payload.get("challenge", "")}


def _text_from_content(raw: Any) -> str:
    """飞书 `message.content` 是**一个 JSON 字符串**（`{"text": "..."}`）。照 Hermes 解包。"""
    if isinstance(raw, Mapping):
        parsed: Any = dict(raw)
    else:
        try:
            parsed = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return str(raw or "")
    if isinstance(parsed, Mapping):
        return str(parsed.get("text", "") or "")
    return str(parsed or "")


def parse_message_event(payload: Mapping[str, Any]) -> FeishuMessage | None:
    """把 `im.message.receive_v1` 事件解成文本消息；非文本/非消息事件回 None。"""
    header = payload.get("header")
    event_type = ""
    if isinstance(header, Mapping):
        event_type = str(header.get("event_type") or "")
    if event_type != RECEIVE_EVENT:
        return None
    event = payload.get("event")
    message = event.get("message") if isinstance(event, Mapping) else None
    if not isinstance(message, Mapping):
        return None
    if str(message.get("message_type") or "").strip().lower() != "text":
        return None
    text = _text_from_content(message.get("content"))
    if not text.strip():
        return None
    sender = event.get("sender") if isinstance(event, Mapping) else None
    open_id = ""
    if isinstance(sender, Mapping):
        sender_id = sender.get("sender_id")
        if isinstance(sender_id, Mapping):
            open_id = str(sender_id.get("open_id") or "")
    return FeishuMessage(
        chat_id=str(message.get("chat_id") or ""),
        text=text,
        message_id=str(message.get("message_id") or ""),
        sender_open_id=open_id,
    )


class RecordingSender:
    """无凭据/本地桩时的默认发送器：**不假装发成功**，只记录 + 告警。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, chat_id: str, text: str) -> dict[str, Any]:
        self.sent.append((chat_id, text))
        logger.warning("[Feishu] 未注入发送器：回复未真正发往飞书（只记录）chat_id=%s", chat_id)
        return {"code": 0, "recorded": True, "chat_id": chat_id}


# ------------------------------------------------------------------ 回调核心（与传输解耦）


class FeishuWebhook:
    """回调处理核心：**传输无关**（不碰 socket），所以能直接单测。"""

    def __init__(
        self,
        ctx: AppContext,
        *,
        credentials: FeishuCredentials | None = None,
        sender: Sender | None = None,
        dispatcher: Dispatcher = dispatch,
        source: str = "feishu",
        rate_limit_max: int = RATE_LIMIT_MAX,
        rate_window_s: int = RATE_WINDOW_SECONDS,
    ) -> None:
        self._ctx = ctx
        self._credentials = credentials or credentials_from_env()
        self._dispatcher = dispatcher
        self._source = source
        self._rate_limit_max = rate_limit_max
        self._rate_window_s = rate_window_s
        self._rate_counts: dict[str, list[float]] = {}
        self._rate_lock = threading.Lock()

        if sender is not None:
            self._sender: Sender = sender
        else:
            self._recorder = RecordingSender()
            self._sender = self._recorder

        if not self._credentials.has_webhook_secret:
            logger.warning(
                "[Feishu] 未配置 FEISHU_VERIFICATION_TOKEN / FEISHU_ENCRYPT_KEY："
                "回调**不校签**（仅适用于本地桩自测，勿用于公网）"
            )

    @property
    def credentials(self) -> FeishuCredentials:
        return self._credentials

    def _check_rate(self, rate_key: str) -> bool:
        """滑动窗口限速（键 `app_id:path:remote_ip`，照 Hermes adapter.py:2776-2785）。"""
        import time

        now = time.time()
        with self._rate_lock:
            hits = [t for t in self._rate_counts.get(rate_key, []) if now - t < self._rate_window_s]
            if len(hits) >= self._rate_limit_max:
                self._rate_counts[rate_key] = hits
                return False
            hits.append(now)
            self._rate_counts[rate_key] = hits
            return True

    def handle(
        self, body_bytes: bytes, headers: Mapping[str, str], *, remote_ip: str = "unknown"
    ) -> tuple[int, dict[str, Any]]:
        """处理一条回调请求，返回 `(http_status, body_dict)`。"""
        rate_key = f"{self._credentials.app_id}:{DEFAULT_PATH}:{remote_ip}"
        if not self._check_rate(rate_key):
            logger.warning("[Feishu] 回调被限速：%s", remote_ip)
            return 429, {"code": 429, "msg": "Too Many Requests"}

        if len(body_bytes) > MAX_BODY_BYTES:
            return 413, {"code": 413, "msg": "Request body too large"}

        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 400, {"code": 400, "msg": "invalid json"}
        if not isinstance(payload, dict):
            return 400, {"code": 400, "msg": "payload must be a JSON object"}

        # 第 1 层：verification token（有配才校）
        if self._credentials.verification_token and not token_valid(
            payload, self._credentials.verification_token
        ):
            logger.warning("[Feishu] 回调被拒：verification token 不对（%s）", remote_ip)
            return 401, {"code": 401, "msg": "Invalid verification token"}

        # URL 验证：**先过 token 再回 challenge**（防未授权者证明自己控制端点）
        if is_challenge(payload):
            return 200, challenge_body(payload)

        # 第 2 层：签名（有配才校）
        if self._credentials.encrypt_key and not signature_valid(
            headers, body_bytes, self._credentials.encrypt_key
        ):
            logger.warning("[Feishu] 回调被拒：签名不对（%s）", remote_ip)
            return 401, {"code": 401, "msg": "Invalid signature"}

        if payload.get("encrypt"):
            return 400, {"code": 400, "msg": "encrypted payloads not supported"}

        message = parse_message_event(payload)
        if message is not None:
            try:
                self._run_turn(message)
            except Exception as exc:  # 事件处理失败也要回 200（否则飞书会重试风暴）
                logger.warning("[Feishu] 处理消息事件失败（已回 200 结束重试）：%s", exc)
        return 200, dict(SUCCESS_BODY)

    def _run_turn(self, message: FeishuMessage) -> None:
        """一条消息 = 一次会话请求：幂等拿会话 → 跑一轮 → 把回复送回去。"""
        ensured = self._dispatcher(
            self._ctx,
            "session.ensure",
            {"session_key": message.chat_id, "source": self._source},
        )
        session_id = str(ensured["session_id"])
        result = self._dispatcher(
            self._ctx, "chat.send", {"session_id": session_id, "text": message.text}
        )
        reply_text = str((result or {}).get("text") or "")
        self._sender(message.chat_id, reply_text)
        logger.info(
            "[Feishu] chat_id=%s → session=%s（stop_reason=%s，回复 %d 字）",
            message.chat_id,
            session_id,
            (result or {}).get("stop_reason"),
            len(reply_text),
        )


# ------------------------------------------------------------------ stdlib HTTP 监听


class _FeishuHTTPServer(ThreadingHTTPServer):
    """把 webhook 与路径挂到 server 上，handler 用 `cast` 取（不用动态属性 + type: ignore）。"""

    daemon_threads = True
    webhook: FeishuWebhook
    webhook_path: str


class _FeishuHandler(BaseHTTPRequestHandler):
    server_version = "ScholarScoutFeishu/0.1"
    protocol_version = "HTTP/1.1"

    def _write(self, status: int, payload: Mapping[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # http.server 规定的名字
        server = cast(_FeishuHTTPServer, self.server)
        if urlparse(self.path).path != server.webhook_path:
            self._write(404, {"code": 404, "msg": "not found"})
            return
        content_type = str(self.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        if content_type and content_type != "application/json":
            self._write(415, {"code": 415, "msg": "Unsupported Media Type"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._write(413, {"code": 413, "msg": "Request body too large"})
            return
        body = self.rfile.read(length) if length else b""
        remote_ip = str(self.client_address[0]) if self.client_address else "unknown"
        status, payload = server.webhook.handle(
            body, {k.lower(): v for k, v in self.headers.items()}, remote_ip=remote_ip
        )
        self._write(status, payload)

    def log_message(self, *args: Any) -> None:  # 静音默认 stderr 请求日志
        return


class FeishuCallbackServer:
    """`ThreadingHTTPServer` 包装：只绑给定 host（默认 127.0.0.1），起后台线程。"""

    def __init__(
        self,
        webhook: FeishuWebhook,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        path: str = DEFAULT_PATH,
    ) -> None:
        self._httpd = _FeishuHTTPServer((host, port), _FeishuHandler)
        self._httpd.webhook = webhook
        self._httpd.webhook_path = path
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    @property
    def path(self) -> str:
        return self._httpd.webhook_path

    def start(self) -> None:
        """起后台线程 `serve_forever`（daemon，不挡进程退出）。"""
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="feishu-callback", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def build_server(
    ctx: AppContext,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = DEFAULT_PATH,
    credentials: FeishuCredentials | None = None,
    sender: Sender | None = None,
    dispatcher: Dispatcher = dispatch,
) -> FeishuCallbackServer:
    """装配一个回调服务（**不启动**）：`FeishuWebhook` + `FeishuCallbackServer`。"""
    webhook = FeishuWebhook(ctx, credentials=credentials, sender=sender, dispatcher=dispatcher)
    return FeishuCallbackServer(webhook, host=host, port=port, path=path)
