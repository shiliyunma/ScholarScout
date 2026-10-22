"""MinerU 官方 API：扫描版/复杂版式 PDF 的兜底解析（对应决议第 8 题"不自己造轮子"）。

## 两条接口（2026-10-05 真机实测 + 官方文档核对）

| | Agent 轻量解析（默认） | 精准解析（配了 token 才用） |
|---|---|---|
| Token | **不需要**（IP 限频） | 需要 |
| 提交 | ``POST /api/v1/agent/parse/url`` | ``POST /api/v4/extract/task`` |
| 轮询 | ``GET /api/v1/agent/parse/{task_id}`` | ``GET /api/v4/extract/task/{task_id}`` |
| 限制 | ≤ 10MB / ≤ 20 页 | ≤ 200MB / ≤ 200 页 |
| 完成态 | ``data.markdown_url``（CDN 上的 Markdown） | ``data.full_zip_url``（Zip，内含 ``full.md``） |

实测记录：
- 提交成功返回 ``{"code":0,"msg":"ok","data":{"task_id":"..."}}`` ✓
- 轮询返回 ``data.state`` ∈ ``waiting-file / uploading / pending / running / converting / done / failed``
- 未带 token 打精准接口 → **HTTP 401 ``login required``** → 代码里自动退回 Agent 接口
- **注意**：免 token 的 Agent 服务会间歇性失败（实测拿到 ``state=failed`` +
  ``model service is temporarily unavailable``）→ 失败必须如实记 ``mineru_failed`` 且带原因，
  绝不能把"服务端抽风"当成"这份文档没有内容"

无新依赖：Zip 解包用 stdlib ``zipfile``，下载走既有的 ``HttpClient``。
"""

from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from scholarscout.fetch.http import FetchError, HttpClient
from scholarscout.fetch.settings import MineruSettings

AGENT_SUBMIT_URL = "https://mineru.net/api/v1/agent/parse/url"
AGENT_FILE_SUBMIT_URL = "https://mineru.net/api/v1/agent/parse/file"
AGENT_TASK_URL = "https://mineru.net/api/v1/agent/parse/{task_id}"
PRECISE_SUBMIT_URL = "https://mineru.net/api/v4/extract/task"
PRECISE_FILE_URLS_URL = "https://mineru.net/api/v4/file-urls/batch"
PRECISE_BATCH_TASK_URL = "https://mineru.net/api/v4/extract-results/batch/{batch_id}"
PRECISE_TASK_URL = "https://mineru.net/api/v4/extract/task/{task_id}"

#: 仍在处理中的状态（官方口径）
PENDING_STATES = frozenset({"waiting-file", "uploading", "pending", "running", "converting"})
DONE_STATE = "done"
FAILED_STATE = "failed"

DEFAULT_POLL_INTERVAL_S = 3.0
DEFAULT_TIMEOUT_S = 180.0


@dataclass(frozen=True)
class MineruText:
    """解析结果。``text`` 为空时 ``state`` 说明原因（失败/超时）。"""

    text: str
    state: str
    engine: str  # agent | precise
    detail: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def _json_or_none(response: Any) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _pdf_page_count(content: bytes) -> int | None:
    """数 PDF 页数（pymupdf，项目已有依赖）。**数不出来回 ``None``**。

    为什么不数出来就当 0 页：这条路进来的字节不一定是合法 PDF（http 层可能给了个
    错误页），数不出页数就按页数拒收，等于把"看不出来"当成"超限"。
    """
    try:
        import pymupdf
    except Exception:
        return None
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        return None
    try:
        return int(document.page_count)
    finally:
        document.close()


def _refuse_over_limit(
    content: bytes, *, max_bytes: int, max_pages: int, bytes_key: str, pages_key: str
) -> None:
    """直传前在**本地**判一次超限，超了就抛 ``FetchError("too_large")``，一个请求都不发。

    为什么要本地判：这两个上限以前只写在注释里（YAML 里那四个键没人读），超限只能等
    MinerU 服务端回一句含糊的 failed，用户看不出是"太大了"还是"服务抽风"。本地判掉既省一次
    上传，也能把**是哪一条键超了**直接说清楚。
    """
    if len(content) > max_bytes:
        raise FetchError(
            "too_large", f"{bytes_key} 上限 {max_bytes} 字节，这份 {len(content)} 字节"
        )
    pages = _pdf_page_count(content)
    if pages is not None and pages > max_pages:
        raise FetchError("too_large", f"{pages_key} 上限 {max_pages} 页，这份 {pages} 页")


def _submit(
    http: HttpClient, *, url: str, token: str | None, min_interval_s: float
) -> tuple[str, str]:
    """提交解析任务，返回 ``(task_id, 实际走通的接口)``。

    注意两件事（都是单元测试抓出来的真 bug）：
    1. 精准接口失败可能是**抛异常**（401 → FetchError ``forbidden``），不只是返回空 task_id
    2. 退回 Agent 接口后，**轮询也必须用 Agent 的 URL** —— 否则会拿 Agent 的 task_id
       去查精准接口的端点（``/api/v4/extract/task/{id}``），永远查不到
    """
    if token:
        try:
            payload = {"url": url, "model_version": "vlm"}
            response = http.post_json(
                PRECISE_SUBMIT_URL,
                payload,
                headers={"Authorization": f"Bearer {token}"},
                min_interval_s=min_interval_s,
            )
            body = _json_or_none(response)
            data = (body or {}).get("data") or {}
            task_id = str(data.get("task_id") or "")
            if task_id:
                return task_id, "precise"
        except FetchError:
            pass  # 精准接口不可用（401/额度/网络）→ 退回 Agent 接口
    payload = {"url": url, "language": "ch"}
    response = http.post_json(AGENT_SUBMIT_URL, payload, min_interval_s=min_interval_s)
    body = _json_or_none(response)
    if body is not None and body.get("code") not in (0, None):
        raise FetchError("bad_request", f"MinerU 提交失败：{body.get('msg')}", url=AGENT_SUBMIT_URL)
    data = (body or {}).get("data") or {}
    task_id = str(data.get("task_id") or "")
    if not task_id:
        raise FetchError("server", "MinerU 没有返回 task_id", url=AGENT_SUBMIT_URL)
    return task_id, "agent"


def _poll(
    http: HttpClient,
    *,
    task_id: str,
    token: str | None,
    engine: str,
    poll_interval_s: float,
    timeout_s: float,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """轮询到终态（done/failed）或超时。返回最后一次的 ``data``。"""
    template = PRECISE_TASK_URL if engine == "precise" else AGENT_TASK_URL
    url = template.format(task_id=task_id)
    headers = {"Authorization": f"Bearer {token}"} if (token and engine == "precise") else None
    deadline = time.monotonic() + timeout_s
    data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = http.fetch(url, accept="application/json", headers=headers, cache=False)
        body = _json_or_none(response)
        data = ((body or {}).get("data") or {}) if isinstance(body, dict) else {}
        state = str(data.get("state") or "")
        if state == DONE_STATE:
            return data
        if state == FAILED_STATE:
            return data
        sleep(poll_interval_s)
    data.setdefault("state", "timeout")
    data.setdefault("err_msg", f"轮询超过 {int(timeout_s)}s 仍未完成")
    return data


def _markdown_from_url(http: HttpClient, markdown_url: str) -> str:
    response = http.fetch(markdown_url, accept="text/markdown, text/plain, */*")
    return response.text


def _markdown_from_zip(http: HttpClient, zip_url: str) -> str:
    """精准接口给的是 Zip（内含 full.md）—— 解包取 Markdown，不落盘。"""
    response = http.fetch(zip_url, accept="application/zip, application/octet-stream, */*")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".md")]
        if not names:
            return ""
        # 首选 full.md，其余取名字最短的（通常是正文而不是分片）
        names.sort(key=lambda name: (not name.lower().endswith("full.md"), len(name)))
        return archive.read(names[0]).decode("utf-8", "replace")


def parse_pdf_url(
    http: HttpClient,
    url: str,
    *,
    token: str | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    min_interval_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> MineruText:
    """把一份**公开可达**的 PDF 交给 MinerU 解析，返回 Markdown 正文。

    ``state`` 语义（会原样写进 ``documents.extract_status`` / ``extract_detail``）：
    - ``done`` → 拿到正文
    - ``failed`` → 服务端失败（原因在 ``detail``，如限页数、服务暂不可用）
    - ``timeout`` → 轮询超时
    """
    if not url.lower().startswith(("http://", "https://")):
        return MineruText("", "failed", "agent", "MinerU 只能解析公开 URL，本地文件不支持")

    engine = "precise" if token else "agent"
    try:
        task_id, actual_engine = _submit(http, url=url, token=token, min_interval_s=min_interval_s)
        data = _poll(
            http,
            task_id=task_id,
            token=token,
            engine=actual_engine,
            poll_interval_s=poll_interval_s,
            timeout_s=timeout_s,
            sleep=sleep,
        )
        engine = actual_engine
    except FetchError as exc:
        return MineruText("", "failed", engine, f"{exc.kind}: {exc}")

    state = str(data.get("state") or "")
    if state != DONE_STATE:
        detail = str(data.get("err_msg") or data.get("err_code") or state)
        return MineruText("", state or "failed", engine, detail)

    if engine == "precise":
        zip_url = str(data.get("full_zip_url") or "")
        text = _markdown_from_zip(http, zip_url) if zip_url else ""
    else:
        markdown_url = str(data.get("markdown_url") or "")
        text = _markdown_from_url(http, markdown_url) if markdown_url else ""
    if not text.strip():
        return MineruText("", "failed", engine, "完成态但没有拿到 Markdown（链接为空或下载为空）")
    return MineruText(text, DONE_STATE, engine, "")


# ------------------------------------------------------------------ 本地文件直传


def _agent_upload_submit(
    http: HttpClient,
    *,
    content: bytes,
    filename: str,
    is_ocr: bool,
    language: str,
    min_interval_s: float,
    limits: MineruSettings | None = None,
) -> str:
    """Agent 轻量接口的"签名上传"：申请 task_id + 预签名 URL → PUT 字节。返回 task_id。

    **免 Token、免公网 URL** —— 这是我们手里已经有本地字节时最该走的一条路
    （官方限制：≤10MB、≤20 页，现在由 ``mineru.agent_max_bytes`` / ``mineru.agent_max_pages``
    决定，未配置就是这两个官方值；扫描件必须带 ``is_ocr=true``，默认是 false）。
    """
    caps = limits if limits is not None else MineruSettings()
    # 读配置处（1/2）：轻量接口的字节/页数上限 —— 超了在这里抛 too_large，一个请求都不发。
    _refuse_over_limit(
        content,
        max_bytes=caps.agent_max_bytes,
        max_pages=caps.agent_max_pages,
        bytes_key="mineru.agent_max_bytes",
        pages_key="mineru.agent_max_pages",
    )
    response = http.post_json(
        AGENT_FILE_SUBMIT_URL,
        {"file_name": filename, "language": language, "is_ocr": bool(is_ocr)},
        min_interval_s=min_interval_s,
    )
    body = _json_or_none(response)
    if body is not None and body.get("code") not in (0, None):
        raise FetchError(
            "bad_request", f"MinerU 申请上传失败：{body.get('msg')}", url=AGENT_FILE_SUBMIT_URL
        )
    data = (body or {}).get("data") or {}
    task_id = str(data.get("task_id") or "")
    file_url = str(data.get("file_url") or data.get("url") or "")
    if not task_id or not file_url:
        raise FetchError("server", "MinerU 没有返回 task_id / file_url", url=AGENT_FILE_SUBMIT_URL)
    # 刻意不带 Content-Type：预签名地址把它算进签名，带了就 SignatureDoesNotMatch
    http.upload_bytes(file_url, content, min_interval_s=min_interval_s)
    return task_id


def _precise_upload_submit(
    http: HttpClient,
    *,
    content: bytes,
    filename: str,
    token: str,
    min_interval_s: float,
    limits: MineruSettings | None = None,
) -> str:
    """精准接口的批量上传：申请 batch_id + 预签名 URL → PUT 字节。返回 batch_id。

    上限（官方 200MB / 200 页）现在由 ``mineru.precise_max_bytes`` / ``mineru.precise_max_pages``
    决定，未配置就是这两个官方值。
    """
    caps = limits if limits is not None else MineruSettings()
    # 读配置处（2/2）：精准接口的字节/页数上限（以前只有注释里的 200MB/200 页）。
    _refuse_over_limit(
        content,
        max_bytes=caps.precise_max_bytes,
        max_pages=caps.precise_max_pages,
        bytes_key="mineru.precise_max_bytes",
        pages_key="mineru.precise_max_pages",
    )
    response = http.post_json(
        PRECISE_FILE_URLS_URL,
        {"files": [{"name": filename, "data_id": "scholarscout"}], "model_version": "vlm"},
        headers={"Authorization": f"Bearer {token}"},
        min_interval_s=min_interval_s,
    )
    body = _json_or_none(response)
    if body is not None and body.get("code") not in (0, None):
        raise FetchError(
            "bad_request", f"MinerU 申请上传失败：{body.get('msg')}", url=PRECISE_FILE_URLS_URL
        )
    data = (body or {}).get("data") or {}
    batch_id = str(data.get("batch_id") or "")
    urls = data.get("file_urls") or []
    if not batch_id or not urls:
        raise FetchError(
            "server", "MinerU 没有返回 batch_id / file_urls", url=PRECISE_FILE_URLS_URL
        )
    http.upload_bytes(str(urls[0]), content, min_interval_s=min_interval_s)
    return batch_id


def _poll_batch(
    http: HttpClient,
    *,
    batch_id: str,
    token: str,
    poll_interval_s: float,
    timeout_s: float,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """轮询精准接口的批次结果（``data.extract_result[0]`` 才是这一份文件的状态）。"""
    url = PRECISE_BATCH_TASK_URL.format(batch_id=batch_id)
    deadline = time.monotonic() + timeout_s
    data: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = http.fetch(
            url,
            accept="application/json",
            headers={"Authorization": f"Bearer {token}"},
            cache=False,
        )
        body = _json_or_none(response)
        results = ((body or {}).get("data") or {}).get("extract_result") or []
        data = dict(results[0]) if results else {}
        state = str(data.get("state") or "")
        if state in (DONE_STATE, FAILED_STATE):
            return data
        sleep(poll_interval_s)
    data.setdefault("state", "timeout")
    data.setdefault("err_msg", f"轮询超过 {int(timeout_s)}s 仍未完成")
    return data


def parse_pdf_bytes(
    http: HttpClient,
    content: bytes,
    *,
    filename: str = "scan.pdf",
    token: str | None = None,
    is_ocr: bool = True,
    language: str = "ch",
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    min_interval_s: float = 1.0,
    limits: MineruSettings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> MineruText:
    """把**本地字节**交给 MinerU（走预签名上传），返回 Markdown 正文。

    为什么必须支持这条路：老实现只认公开 URL，于是"扫描版能不能抽正文"就卡在
    "MinerU 得能自己下载到那个 PDF"（内网地址、需要登录、CDN 抖动都会失败）。
    而我们**手里本来就有字节**（`source.fetch` 已经下载过一次），直接传上去最稳。

    Token 缺失时走 Agent 轻量接口（免登录，≤10MB/20 页）；有 Token 走精准接口（≤200MB/200 页）
    —— 这两组上限**不再写死在这里**：``mineru.agent_max_bytes`` / ``agent_max_pages`` /
    ``precise_max_bytes`` / ``precise_max_pages``。不传 ``limits`` 就是官方的 10MB/20 页与
    200MB/200 页（与改造前行为一致）；超限在上传前本地判掉，回 failed + 具体是哪条键。
    精准接口失败（例如 401）会自动退回 Agent —— 与 URL 那条路同一个策略。
    """
    if not content:
        return MineruText("", "failed", "agent", "文件内容为空")
    engine = "precise" if token else "agent"
    caps = limits if limits is not None else MineruSettings()
    try:
        if token:
            try:
                batch_id = _precise_upload_submit(
                    http,
                    content=content,
                    filename=filename,
                    token=token,
                    min_interval_s=min_interval_s,
                    limits=caps,
                )
                data = _poll_batch(
                    http,
                    batch_id=batch_id,
                    token=token,
                    poll_interval_s=poll_interval_s,
                    timeout_s=timeout_s,
                    sleep=sleep,
                )
            except FetchError as exc:
                # 本地判掉的超限**不许**靠"退回轻量接口再塞一遍"糊过去：那等于绕开用户配的上限，
                # 而轻量接口的上限只会更小。其它失败（401 之类）照旧退回。
                if exc.kind == "too_large":
                    raise
                engine = "agent"
                task_id = _agent_upload_submit(
                    http,
                    content=content,
                    filename=filename,
                    is_ocr=is_ocr,
                    language=language,
                    min_interval_s=min_interval_s,
                    limits=caps,
                )
                data = _poll(
                    http,
                    task_id=task_id,
                    token=None,
                    engine="agent",
                    poll_interval_s=poll_interval_s,
                    timeout_s=timeout_s,
                    sleep=sleep,
                )
        else:
            task_id = _agent_upload_submit(
                http,
                content=content,
                filename=filename,
                is_ocr=is_ocr,
                language=language,
                min_interval_s=min_interval_s,
                limits=caps,
            )
            data = _poll(
                http,
                task_id=task_id,
                token=None,
                engine="agent",
                poll_interval_s=poll_interval_s,
                timeout_s=timeout_s,
                sleep=sleep,
            )
    except FetchError as exc:
        return MineruText("", "failed", engine, f"{exc.kind}: {exc}")

    state = str(data.get("state") or "")
    if state != DONE_STATE:
        detail = str(data.get("err_msg") or data.get("err_code") or state)
        return MineruText("", state or "failed", engine, detail)

    if engine == "precise":
        zip_url = str(data.get("full_zip_url") or "")
        text = _markdown_from_zip(http, zip_url) if zip_url else ""
    else:
        markdown_url = str(data.get("markdown_url") or "")
        text = _markdown_from_url(http, markdown_url) if markdown_url else ""
    if not text.strip():
        return MineruText("", "failed", engine, "完成态但没有拿到 Markdown（链接为空或下载为空）")
    return MineruText(text, DONE_STATE, engine, "")
