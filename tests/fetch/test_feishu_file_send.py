"""飞书**发文件**（上传 → 私聊发送）的桩测：零凭据、零真网络。

官方口径（2026-10-18 核实的官方文档「上传文件」「发送消息」）：

- `POST /open-apis/im/v1/files`，multipart 字段 `file_type` / `file_name` / `file`；
  `file_type` ∈ opus / mp4 / pdf / doc / xls / ppt / **stream**（其余一律 stream）；
- 单文件 **≤ 30 MB**，且**不允许空文件**；
- 发送走 `POST /open-apis/im/v1/messages?receive_id_type=open_id`，
  `msg_type=file`、`content` 是 JSON **字符串** `{"file_key": "..."}`；
- 权限：上传要 `im:resource`（或 `im:resource:upload`），发送要 `im:message`（或 `im:message:send_as_bot`）。

本文件钉住七件事：上传→发送的**同一个 open_id**、两段请求的真实形状、超 30 MB 本地就拒、
空文件本地就拒、权限错回**可读中文**、缺凭据/缺 open_id 零请求、开关关着时零外网。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from scholarscout.fetch import feishu_send
from scholarscout.fetch.http import HttpClient

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/files"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
TOKEN_OK = {"code": 0, "msg": "ok", "tenant_access_token": "t-" + "a" * 40, "expire": 7200}
UPLOAD_OK = {"code": 0, "msg": "success", "data": {"file_key": "file_v3_01abc"}}
MESSAGE_OK = {"code": 0, "msg": "success", "data": {"message_id": "om_file_0001"}}

OPEN_ID = "ou_user_0001"


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpClient:
    return HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))


def router(
    seen: list[httpx.Request],
    *,
    upload: httpx.Response | None = None,
    message: httpx.Response | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """按 URL 分派：取 token / 上传 / 发消息（默认全成功）。"""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if "tenant_access_token" in url:
            return httpx.Response(200, json=TOKEN_OK)
        if url.endswith("/open-apis/im/v1/files"):
            return upload or httpx.Response(200, json=UPLOAD_OK)
        return message or httpx.Response(200, json=MESSAGE_OK)

    return handle


def _pdf(tmp_path: Path, name: str = "报告.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 fake body")
    return path


def _send(http: HttpClient, path: Path, **kwargs: object) -> feishu_send.FileOutcome:
    params: dict[str, object] = {
        "app_id": "cli_x",
        "app_secret": "sec_x",
        "open_id": OPEN_ID,
        "path": path,
        "domain": "feishu",
    }
    params.update(kwargs)
    return feishu_send.send_file(http, **params)  # type: ignore[arg-type]


# ---------------------------------------------------------- 1. 上传 → 发送（同一个 open_id）


def test_upload_then_send_file_uses_the_same_open_id(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    http = make_client(router(seen))
    path = _pdf(tmp_path)

    out = _send(http, path)

    assert out.ok is True
    assert out.message_id == "om_file_0001"
    assert out.file_key == "file_v3_01abc"
    assert out.file_name == "报告.pdf"
    assert out.size_bytes == path.stat().st_size == len(b"%PDF-1.4 fake body")
    # 只打三次：换 token → 上传 → 发送（不多打、不少打）
    assert [str(r.url) for r in seen] == [
        TOKEN_URL,
        UPLOAD_URL,
        MESSAGE_URL + "?receive_id_type=open_id",
    ]


def test_upload_request_is_multipart_with_official_fields(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    http = make_client(router(seen))
    path = _pdf(tmp_path)

    assert _send(http, path).ok is True

    upload = seen[1]
    assert upload.method == "POST"
    # Content-Type 必须由 httpx 写（带 boundary），手写一个就没有 boundary 了
    assert upload.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert upload.headers["authorization"] == "Bearer " + str(TOKEN_OK["tenant_access_token"])
    body = upload.content
    assert b'name="file_type"' in body and b"pdf" in body
    assert b'name="file_name"' in body and "报告.pdf".encode() in body
    assert b'name="file"' in body and b"%PDF-1.4 fake body" in body


def test_send_request_carries_file_key_as_json_string_to_open_id(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    http = make_client(router(seen))

    assert _send(http, _pdf(tmp_path)).ok is True

    send = seen[2]
    body = json.loads(send.content.decode("utf-8"))
    assert body["receive_id"] == OPEN_ID, "上传与发送必须是同一个 open_id"
    assert body["msg_type"] == "file"
    assert isinstance(body["content"], str), "`content` 是 JSON 字符串，不是对象"
    assert json.loads(body["content"]) == {"file_key": "file_v3_01abc"}
    assert body["uuid"], "缺幂等键：重试会重复投递"


# ---------------------------------------------------------- 2. `file_type` 映射


def test_file_type_mapping_falls_back_to_stream() -> None:
    assert feishu_send.file_type_for("a.pdf") == "pdf"
    assert feishu_send.file_type_for("A.PDF") == "pdf"  # 大小写不敏感
    assert feishu_send.file_type_for("a.doc") == "doc"
    assert feishu_send.file_type_for("a.xls") == "xls"
    assert feishu_send.file_type_for("a.ppt") == "ppt"
    assert feishu_send.file_type_for("a.mp4") == "mp4"
    assert feishu_send.file_type_for("a.opus") == "opus"
    # 枚举之外的（.md/.docx/.txt/无后缀）一律 stream —— 猜 `doc` 会被 230055 拒
    for name in ("笔记.md", "a.docx", "a.txt", "a.csv", "without_suffix"):
        assert feishu_send.file_type_for(name) == "stream"
    assert set(feishu_send._FILE_TYPE_BY_SUFFIX.values()) <= set(feishu_send.FILE_TYPE_ENUM)


# ---------------------------------------------------------- 3. 超 30 MB / 空文件：本地就拒


def test_file_over_30mb_is_rejected_before_any_request(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    http = make_client(router(seen))
    big = tmp_path / "大文件.pdf"
    with big.open("wb") as handle:
        handle.truncate(feishu_send.MAX_FILE_BYTES + 1)  # 稀疏文件，不真写 30 MB

    out = _send(http, big)

    assert out.ok is False
    assert "30 MB" in out.error and "大文件.pdf" in out.error
    assert seen == [], "超限必须在本地拒掉，不能先上传再吃 234006"


def test_empty_file_is_rejected_before_any_request(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    http = make_client(router(seen))
    empty = tmp_path / "空.md"
    empty.write_bytes(b"")

    out = _send(http, empty)

    assert out.ok is False
    assert "空文件" in out.error or "0 字节" in out.error
    assert seen == []


def test_missing_file_is_a_clear_failure_not_a_crash(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    out = _send(make_client(router(seen)), tmp_path / "没有这个.pdf")
    assert out.ok is False and "读不到文件" in out.error
    assert seen == []


def test_missing_open_id_or_credentials_never_touches_the_network(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    http = make_client(router(seen))
    path = _pdf(tmp_path)

    no_open_id = _send(http, path, open_id="   ")
    assert no_open_id.ok is False and "open_id" in no_open_id.error
    no_creds = _send(http, path, app_id="", app_secret="")
    assert no_creds.ok is False and "凭据" in no_creds.error
    assert seen == []


# ---------------------------------------------------------- 4. 平台错误码 → 人话


def test_missing_upload_permission_is_a_readable_chinese_error(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    denied = httpx.Response(
        200,
        json={"code": 99991672, "msg": "Access denied. One of the following scopes is required"},
    )
    http = make_client(router(seen, upload=denied))

    out = _send(http, _pdf(tmp_path))

    assert out.ok is False and out.code == 99991672
    assert "im:resource" in out.error, "权限没勾时要告诉用户去哪勾什么"
    assert "99991672" in out.error  # 原始码也留着，便于查官方文档
    # 上传失败就不该再发一条消息
    assert len(seen) == 2


def test_missing_send_permission_is_a_readable_chinese_error(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    denied = httpx.Response(200, json={"code": 230027, "msg": "no permission"})
    http = make_client(router(seen, message=denied))

    out = _send(http, _pdf(tmp_path))

    assert out.ok is False and out.code == 230027
    assert "im:message" in out.error
    assert out.file_key == "file_v3_01abc", "发送失败要留下 file_key，方便追这一份上传件"


def test_upload_response_without_file_key_is_a_failure(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    broken = httpx.Response(200, json={"code": 0, "msg": "success", "data": {}})
    out = _send(make_client(router(seen, upload=broken)), _pdf(tmp_path))
    assert out.ok is False and "file_key" in out.error
    assert len(seen) == 2


def test_transport_error_is_swallowed_into_ok_false(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json=TOKEN_OK)
        raise httpx.ConnectError("boom", request=request)

    out = _send(make_client(boom), _pdf(tmp_path))
    assert out.ok is False and "上传请求失败" in out.error


# ---------------------------------------------------------- 5. 工厂：开关关着零外网


def test_file_sender_disabled_never_uploads(tmp_path: Path, monkeypatch) -> None:
    seen: list[httpx.Request] = []
    http = make_client(router(seen))
    sender = feishu_send.build_file_sender(http, enabled=False, app_id="cli_x", app_secret="sec_x")

    out = sender(OPEN_ID, str(_pdf(tmp_path)))

    assert out["ok"] is False and "未开启出站发送" in out["error"]
    assert seen == []


def test_file_sender_without_credentials_never_uploads(tmp_path: Path, monkeypatch) -> None:
    seen: list[httpx.Request] = []
    http = make_client(router(seen))
    monkeypatch.delenv(feishu_send.APP_ID_ENV, raising=False)
    monkeypatch.delenv(feishu_send.APP_SECRET_ENV, raising=False)

    out = feishu_send.build_file_sender(http, enabled=True)(OPEN_ID, str(_pdf(tmp_path)))

    assert out["ok"] is False and "缺凭据" in out["error"]
    assert seen == []


def test_file_sender_with_credentials_really_uploads(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    http = make_client(router(seen))
    sender = feishu_send.build_file_sender(http, enabled=True, app_id="cli_x", app_secret="sec_x")

    out = sender(OPEN_ID, str(_pdf(tmp_path)))

    assert out["ok"] is True and out["message_id"] == "om_file_0001"
    assert out["file_key"] == "file_v3_01abc" and out["size_bytes"] > 0
    body = json.loads(seen[2].content.decode("utf-8"))
    assert body["receive_id"] == OPEN_ID


def test_file_sender_swallows_unexpected_exceptions(tmp_path: Path, monkeypatch) -> None:
    """发文件失败绝不能把会话/回调搞崩：任何异常都要变成 `ok=False`。"""

    def boom(*args: object, **kwargs: object) -> feishu_send.FileOutcome:
        raise RuntimeError("磁盘冒烟了")

    monkeypatch.setattr(feishu_send, "send_file", boom)
    sender = feishu_send.build_file_sender(
        make_client(router([])), enabled=True, app_id="cli_x", app_secret="sec_x"
    )

    out = sender(OPEN_ID, str(_pdf(tmp_path)))

    assert out["ok"] is False and "RuntimeError" in out["error"]
