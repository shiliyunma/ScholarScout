"""MinerU 解析的状态机、两条接口、Zip 解包（全离线，用 MockTransport）。

真机契约（2026-10-05 实测 + 官方文档核对）：
- 提交：``POST /api/v1/agent/parse/url`` → ``{"code":0,"data":{"task_id":"…"}}``
- 轮询：``GET /api/v1/agent/parse/{task_id}`` → ``data.state`` ∈
  ``waiting-file / uploading / pending / running / converting / done / failed``
- 完成态：Agent 给 ``data.markdown_url``；精准接口给 ``data.full_zip_url``
- 无 token 打精准接口 → HTTP 401 ``login required`` → 必须自动退回 Agent 接口
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from scholarscout.fetch.http import HttpClient
from scholarscout.fetch.mineru import MineruText, parse_pdf_url


def _client(handler) -> HttpClient:
    return HttpClient(respect_robots=False, transport=httpx.MockTransport(handler))


def test_agent_path_polls_until_done_and_fetches_markdown() -> None:
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v1/agent/parse/url"):
            assert request.method == "POST"
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"task_id": "t1"}})
        if "/api/v1/agent/parse/t1" in url:
            polls["n"] += 1
            state = "running" if polls["n"] == 1 else "done"
            body: dict = {"code": 0, "msg": "ok", "data": {"task_id": "t1", "state": state}}
            if state == "done":
                body["data"]["markdown_url"] = "https://cdn.test/full.md"
            return httpx.Response(200, json=body)
        if url.endswith("/full.md"):
            return httpx.Response(200, text="# 正文\n\nMinerU 抽出来的内容。")
        raise AssertionError(f"意外的请求：{url}")

    http = _client(handler)
    result = parse_pdf_url(http, "https://x.test/paper.pdf", sleep=lambda _s: None)
    assert result.ok and result.engine == "agent"
    assert "MinerU 抽出来的内容" in result.text
    assert polls["n"] == 2, "应该先 running 再 done，两次轮询"


def test_failed_state_surfaces_server_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v1/agent/parse/url"):
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t2"}})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "task_id": "t2",
                    "state": "failed",
                    "err_code": -30003,
                    "err_msg": "file page count exceeds lightweight API limit",
                },
            },
        )

    result = parse_pdf_url(_client(handler), "https://x.test/big.pdf", sleep=lambda _s: None)
    assert not result.ok
    assert result.state == "failed"
    assert "page count exceeds" in result.detail, "服务端给的原因必须被保留"


def test_timeout_when_never_finishes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v1/agent/parse/url"):
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "t3"}})
        return httpx.Response(200, json={"code": 0, "data": {"task_id": "t3", "state": "running"}})

    result = parse_pdf_url(
        _client(handler), "https://x.test/slow.pdf", timeout_s=0.0, sleep=lambda _s: None
    )
    assert not result.ok
    assert result.state == "timeout"
    assert "仍未完成" in result.detail


def test_precise_path_unzips_full_markdown() -> None:
    """配了 token → 走精准接口，结果是 Zip，必须从里面取 full.md。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("full.md", "# 精准解析正文")
        archive.writestr("layout.json", "{}")
    zip_bytes = buffer.getvalue()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v4/extract/task"):
            assert request.headers.get("authorization") == "Bearer tok"
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "p1"}})
        if "/api/v4/extract/task/p1" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "task_id": "p1",
                        "state": "done",
                        "full_zip_url": "https://cdn.test/out.zip",
                    },
                },
            )
        if url.endswith("/out.zip"):
            seen["zip"] = "yes"
            return httpx.Response(
                200, content=zip_bytes, headers={"content-type": "application/zip"}
            )
        raise AssertionError(f"意外的请求：{url}")

    result = parse_pdf_url(
        _client(handler), "https://x.test/scan.pdf", token="tok", sleep=lambda _s: None
    )
    assert seen.get("zip") == "yes"
    assert result.ok and result.engine == "precise"
    assert "精准解析正文" in result.text


def test_precise_401_falls_back_to_agent() -> None:
    """token 失效/额度用完（401）：退回免 token 的 Agent 接口，而不是整体失败。"""
    used: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v4/extract/task"):
            used.append("precise")
            return httpx.Response(401, json={"msg": "login required"})
        if url.endswith("/api/v1/agent/parse/url"):
            used.append("agent")
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "a9"}})
        if "/api/v1/agent/parse/a9" in url:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "task_id": "a9",
                        "state": "done",
                        "markdown_url": "https://cdn.test/a.md",
                    },
                },
            )
        if url.endswith("/a.md"):
            return httpx.Response(200, text="退回 Agent 拿到的正文")
        raise AssertionError(f"意外的请求：{url}")

    result = parse_pdf_url(
        _client(handler), "https://x.test/scan.pdf", token="stale", sleep=lambda _s: None
    )
    assert used == ["precise", "agent"], "应该先试精准再退回 Agent"
    assert result.ok and result.engine == "agent"
    assert "退回 Agent" in result.text


def test_local_path_is_rejected_honestly() -> None:
    """本地文件不支持（接口只收公开 URL）—— 如实拒绝，不发请求。"""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("本地路径不该发请求")

    result = parse_pdf_url(
        _client(handler), "D:/data/raw/papers/x.pdf", token=None, sleep=lambda _s: None
    )
    assert not result.ok
    assert "只能解析公开 URL" in result.detail


def test_done_but_empty_markdown_link_is_failure() -> None:
    """完成态但没给 markdown_url → 不能当成成功（空正文）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v1/agent/parse/url"):
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "e1"}})
        return httpx.Response(200, json={"code": 0, "data": {"task_id": "e1", "state": "done"}})

    result = parse_pdf_url(_client(handler), "https://x.test/x.pdf", sleep=lambda _s: None)
    assert not result.ok
    assert result.state == "failed"
    assert "没有拿到 Markdown" in result.detail


def test_mineru_text_ok_property() -> None:
    assert MineruText("正文", "done", "agent").ok is True
    assert MineruText("   ", "done", "agent").ok is False


@pytest.mark.parametrize("state", ["pending", "running", "converting"])
def test_pending_states_keep_polling(state: str) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v1/agent/parse/url"):
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "k"}})
        if "/api/v1/agent/parse/k" in url:  # 只数轮询次数，别把最后的 Markdown 下载也数进去
            calls["n"] += 1
            if calls["n"] <= 2:
                return httpx.Response(
                    200, json={"code": 0, "data": {"task_id": "k", "state": state}}
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "task_id": "k",
                        "state": "done",
                        "markdown_url": "https://cdn.test/k.md",
                    },
                },
            )
        if url.endswith("/k.md"):
            return httpx.Response(200, text="正文")
        raise AssertionError(f"意外的请求：{url}")

    result = parse_pdf_url(_client(handler), "https://x.test/k.pdf", sleep=lambda _s: None)
    assert result.ok
    assert calls["n"] == 3
