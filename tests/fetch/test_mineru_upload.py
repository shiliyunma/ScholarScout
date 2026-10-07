"""MinerU 本地直传（预签名上传）的单元测试。

为什么要单独测这一条：URL 提交时代码不用管"怎么把文件送上去"，而直传多了一次 PUT 和一次
签名约定 —— 其中最容易踩的是**PUT 不能带 `Content-Type`**（实测：带了就
`403 SignatureDoesNotMatch`，不带才 200）。这类约束只写在注释里迟早会丢，所以钉成断言。

另外两条口径也要钉住：
- 扫描件必须带 `is_ocr=true`（官方默认 false，不写就抽不出字）；
- 精准接口失败（如 401）要能**自动退回** Agent 轻量接口，且退回后轮询用的是 Agent 端点。
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest

from scholarscout.fetch import mineru
from scholarscout.fetch.http import FetchError
from scholarscout.fetch.mineru import parse_pdf_bytes


class FakeResponse:
    def __init__(
        self, payload: dict[str, Any] | None = None, text: str = "", content: bytes = b""
    ) -> None:
        self._payload = payload
        self.text = text
        self.content = content

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload


class FakeHttp:
    """按脚本回应的假出网层：记录每次调用，便于断言"发了什么头、带了什么参数"。"""

    def __init__(
        self, posts: list[FetchError | dict[str, Any]], polls: list[dict[str, Any]]
    ) -> None:
        self.posts = list(posts)
        self.polls = list(polls)
        self.post_calls: list[dict[str, Any]] = []
        self.puts: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.markdown = ""

    def post_json(self, url: str, payload: dict[str, Any], **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, "payload": payload, "headers": kwargs.get("headers")})
        nxt = self.posts.pop(0)
        if isinstance(nxt, FetchError):
            raise nxt
        return FakeResponse(nxt)

    def upload_bytes(self, url: str, body: bytes, **kwargs: Any) -> FakeResponse:
        self.puts.append({"url": url, "size": len(body), "kwargs": kwargs})
        return FakeResponse({"code": 0})

    def fetch(self, url: str, **kwargs: Any) -> FakeResponse:
        self.gets.append(url)
        if url.endswith(".md"):
            return FakeResponse(text=self.markdown or "扫描出来的正文")
        return FakeResponse(self.polls.pop(0))


def _zip_with_markdown(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("full.md", text)
    return buffer.getvalue()


def _run(http: FakeHttp, **kwargs: Any) -> mineru.MineruText:
    return parse_pdf_bytes(
        http,  # type: ignore[arg-type]
        b"%PDF-1.4 fake",
        filename="scan.pdf",
        poll_interval_s=0.0,
        sleep=lambda _seconds: None,
        **kwargs,
    )


# ------------------------------------------------------------------ Agent 直传


def test_agent_upload_happy_path_and_no_content_type_on_put() -> None:
    http = FakeHttp(
        posts=[{"code": 0, "data": {"task_id": "t-1", "file_url": "https://oss.example/put/1"}}],
        polls=[{"code": 0, "data": {"state": "done", "markdown_url": "https://cdn.example/a.md"}}],
    )
    result = _run(http)
    assert result.ok and result.engine == "agent"
    assert result.text == "扫描出来的正文"
    # 上传地址必须收到字节，且**不许多带 Content-Type**（预签名把它算进签名）
    assert len(http.puts) == 1
    assert http.puts[0]["url"] == "https://oss.example/put/1"
    assert http.puts[0]["kwargs"] == {"min_interval_s": 1.0}, "不许给 upload_bytes 传 content_type"


def test_agent_request_carries_is_ocr_and_language() -> None:
    """扫描件必须 is_ocr=true（官方默认 false）；语言默认 ch。"""
    http = FakeHttp(
        posts=[{"code": 0, "data": {"task_id": "t", "file_url": "https://oss.example/put"}}],
        polls=[{"data": {"state": "done", "markdown_url": "https://cdn.example/a.md"}}],
    )
    _run(http)
    payload = http.post_calls[0]["payload"]
    assert payload["file_name"] == "scan.pdf"
    assert payload["is_ocr"] is True
    assert payload["language"] == "ch"
    assert http.post_calls[0]["url"] == mineru.AGENT_FILE_SUBMIT_URL
    assert http.post_calls[0]["headers"] is None, "Agent 接口免登录，不该带 Authorization"


def test_agent_submit_error_is_reported_as_failed() -> None:
    http = FakeHttp(posts=[{"code": -1, "msg": "超出 IP 限频"}], polls=[])
    result = _run(http)
    assert not result.ok and result.state == "failed"
    assert "超出 IP 限频" in result.detail


def test_missing_file_url_is_a_server_error() -> None:
    http = FakeHttp(posts=[{"code": 0, "data": {"task_id": "t"}}], polls=[])
    result = _run(http)
    assert result.state == "failed" and "file_url" in result.detail


def test_poll_timeout_is_reported() -> None:
    http = FakeHttp(
        posts=[{"code": 0, "data": {"task_id": "t", "file_url": "https://oss.example/put"}}],
        polls=[{"data": {"state": "running"}}] * 50,
    )
    result = _run(http, timeout_s=0.0)
    assert result.state == "timeout"


def test_done_without_markdown_url_is_failed() -> None:
    http = FakeHttp(
        posts=[{"code": 0, "data": {"task_id": "t", "file_url": "https://oss.example/put"}}],
        polls=[{"data": {"state": "done"}}],
    )
    result = _run(http)
    assert not result.ok and "没有拿到 Markdown" in result.detail


def test_empty_content_short_circuits() -> None:
    result = parse_pdf_bytes(FakeHttp([], []), b"")  # type: ignore[arg-type]
    assert not result.ok and "为空" in result.detail


# ------------------------------------------------------------------ 精准接口


def test_precise_upload_uses_batch_and_bearer() -> None:
    http = FakeHttp(
        posts=[
            {"code": 0, "data": {"batch_id": "b-1", "file_urls": ["https://oss.example/put/2"]}}
        ],
        polls=[
            {
                "code": 0,
                "data": {
                    "extract_result": [
                        {"state": "done", "full_zip_url": "https://cdn.example/r.zip"}
                    ]
                },
            }
        ],
    )
    http.markdown = ""  # 走 zip 分支
    zip_bytes = _zip_with_markdown("精准解析出的正文")
    http.fetch = lambda url, **kwargs: (
        FakeResponse(content=zip_bytes) if url.endswith(".zip") else FakeResponse(http.polls.pop(0))
    )  # type: ignore[method-assign]

    result = _run(http, token="tk")
    assert result.ok and result.engine == "precise"
    assert result.text == "精准解析出的正文"
    assert http.post_calls[0]["url"] == mineru.PRECISE_FILE_URLS_URL
    assert http.post_calls[0]["headers"] == {"Authorization": "Bearer tk"}
    assert http.post_calls[0]["payload"]["model_version"] == "vlm"
    assert http.puts[0]["kwargs"] == {"min_interval_s": 1.0}


def test_precise_failure_falls_back_to_agent() -> None:
    """精准接口报错（例如 token 失效）→ 自动退回 Agent，且轮询走 Agent 端点。"""
    http = FakeHttp(
        posts=[
            FetchError("forbidden", "被拒绝（401）", url=mineru.PRECISE_FILE_URLS_URL),
            {"code": 0, "data": {"task_id": "t-agent", "file_url": "https://oss.example/put/3"}},
        ],
        polls=[{"data": {"state": "done", "markdown_url": "https://cdn.example/agent.md"}}],
    )
    result = _run(http, token="bad-token")
    assert result.ok and result.engine == "agent"
    assert http.post_calls[1]["url"] == mineru.AGENT_FILE_SUBMIT_URL
    assert mineru.AGENT_TASK_URL.format(task_id="t-agent") in http.gets, "退回后必须查 Agent 端点"


def test_precise_batch_result_picks_first_file() -> None:
    http = FakeHttp(
        posts=[{"code": 0, "data": {"batch_id": "b", "file_urls": ["https://oss.example/put/4"]}}],
        polls=[
            {"data": {"extract_result": [{"state": "failed", "err_msg": "页数超限"}]}},
        ],
    )
    result = _run(http, token="tk")
    assert result.state == "failed" and "页数超限" in result.detail


@pytest.mark.parametrize("filename", ["scan.pdf", "photo.png", "讲义.docx"])
def test_filename_is_forwarded_as_is(filename: str) -> None:
    """官方按文件名后缀判类型，所以原样传，不要自己改写。"""
    http = FakeHttp(
        posts=[{"code": 0, "data": {"task_id": "t", "file_url": "https://oss.example/put"}}],
        polls=[{"data": {"state": "done", "markdown_url": "https://cdn.example/a.md"}}],
    )
    parse_pdf_bytes(
        http,  # type: ignore[arg-type]
        b"x",
        filename=filename,
        poll_interval_s=0.0,
        sleep=lambda _s: None,
    )
    assert json.loads(json.dumps(http.post_calls[0]["payload"]))["file_name"] == filename
