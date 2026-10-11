"""``archive.download`` 契约：开关 / 大文件 / 去重 / 失败回包 / 落盘与写库 —— **全桩，不真下载**。

五条主路径（口径见 docs/01 §3.3、docs/19 §8.4）：

1. 开关关（``features.enable_download=false``）：**不动网**，回 ``{ok:false, reason, threshold_bytes}``；
2. 超 ``fetch.large_file_mb``：默认**拒收且不落盘**（回 ``large:true`` + 实际字节数 + 阈值），
   ``force:true`` 才放行；
3. ``already:true``：原件已在盘上 → 不重下（``force`` 才重抓）；
4. 非 2xx（``FetchError``）：``ok:false`` + ``http_status``，**不抛回调用方**；
5. 网络异常 / 落盘失败：同样 ``ok:false`` + 原因。

出网用 ``FetchResult`` 桩（不打真实网络）；落盘写到 tmp 的 ``data/`` 下（SCHOLARSCOUT_HOME 重定向）。
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from scholarscout.fetch.http import FetchError, FetchResult
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state.ingest import upsert_source

from ..conftest import T0, insert_document, insert_session, redirect_home

SOURCE = {
    "url": "https://arxiv.org/abs/2501.00001",
    "url_canonical": "https://arxiv.org/abs/2501.00001",
    "domain": "arxiv.org",
    "title": "A Test Paper",
    "source_type": "arxiv",
    "year": 2025,
}

#: 桩内容：够小（默认 20MB 阈值下不会触发大文件分支），但头四字节像 PDF。
PDF_BYTES = b"%PDF-1.4 stub original"


class StubHttp:
    """出网的桩：只回预设结果；**每次调用都记下来** —— "不动网"的断言全靠它。"""

    def __init__(
        self,
        *,
        content: bytes = b"",
        media_type: str = "",
        status: int = 200,
        error: FetchError | None = None,
    ) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.content = content
        self.media_type = media_type
        self.status = status
        self.error = error

    def fetch(self, url: str, *, cache: bool = True) -> FetchResult:
        self.calls.append((url, cache))
        if self.error is not None:
            raise self.error
        return FetchResult(
            url=url,
            status=self.status,
            content=self.content,
            headers={},
            media_type=self.media_type,
        )


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return redirect_home(monkeypatch, tmp_path)


def _enable(ctx: AppContext, *, large_mb: float = 20.0) -> None:
    """打开下载开关（并可调大/调小大文件阈值）—— 只动配置，不碰磁盘。"""
    ctx.config = replace(
        ctx.config,
        features=replace(ctx.config.features, enable_download=True),
        fetch_cfg=replace(ctx.config.fetch_cfg, large_file_mb=large_mb),
    )


def _raw_files(home: Path) -> list[Path]:
    raw = home / "data" / "raw"
    return [p for p in raw.rglob("*") if p.is_file()] if raw.is_dir() else []


# ------------------------------------------------------------------ 1 开关关


def test_disabled_switch_returns_reason_and_never_touches_network(
    ctx: AppContext, tmp_path: Path
) -> None:
    """默认关：不动网、不落盘，只回 ``{ok:false, reason, threshold_bytes}``。"""
    http = StubHttp(content=PDF_BYTES, media_type="application/pdf")
    ctx.http = http

    result = dispatch(ctx, "archive.download", {"url": SOURCE["url"]})

    assert result["ok"] is False
    assert "enable_download" in result["reason"]
    assert result["threshold_bytes"] == 20_000_000
    assert http.calls == []
    assert _raw_files(tmp_path) == []


def test_disabled_switch_also_short_circuits_known_doc(ctx: AppContext, tmp_path: Path) -> None:
    """给了库里真实存在的 doc_id 也一样——开关是第一道闸，先于任何查库动作。"""
    source_id, _ = upsert_source(ctx.db, SOURCE)
    doc_id = insert_document(ctx.db, source_id=source_id)
    http = StubHttp(content=PDF_BYTES, media_type="application/pdf")
    ctx.http = http

    result = dispatch(ctx, "archive.download", {"doc_id": doc_id})

    assert result["ok"] is False and "enable_download" in result["reason"]
    assert http.calls == []


# ------------------------------------------------------------------ 2 大文件阈值


def test_over_threshold_is_rejected_and_writes_nothing(ctx: AppContext, tmp_path: Path) -> None:
    """超阈值默认拒绝：回 ``large:true`` + 字节数 + 阈值，**不落盘、不改库**。"""
    _enable(ctx, large_mb=0.001)  # 阈值 = 1000 字节
    http = StubHttp(content=b"p" * 2500, media_type="application/pdf")
    ctx.http = http

    result = dispatch(ctx, "archive.download", {"url": SOURCE["url"]})

    assert result["ok"] is False
    assert result["large"] is True
    assert result["bytes"] == 2500
    assert result["threshold_bytes"] == 1000
    assert result["mime"] == "application/pdf"
    assert result["http_status"] == 200
    assert "force" in result["reason"]
    assert _raw_files(tmp_path) == []
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 0


def test_force_lands_file_with_docs19_layout(ctx: AppContext, tmp_path: Path) -> None:
    """``force:true`` 放行：落盘命名 = ``<会话>/<格式>/<标题>--<sha8>.<ext>``（docs/19 §2）。"""
    _enable(ctx, large_mb=0.001)
    source_id, _ = upsert_source(ctx.db, SOURCE)
    insert_session(ctx.db)  # 标题「测试会话」
    ctx.db.execute(
        "INSERT INTO source_mentions (source_id, session_id, created_at) VALUES (?, ?, ?)",
        (source_id, "ses_test0001", T0),
    )
    content = b"%PDF-1.4 " + b"z" * 2500
    http = StubHttp(content=content, media_type="application/pdf")
    ctx.http = http

    result = dispatch(ctx, "archive.download", {"source_id": source_id, "force": True})

    sha = hashlib.sha256(content).hexdigest()
    assert result["ok"] is True
    assert result["path"] == f"data/raw/测试会话/paper/A-Test-Paper--{sha[:8]}.pdf"
    assert result["bytes"] == len(content)
    assert result["ext"] == "pdf"
    assert result["large"] is True
    assert result["already"] is False
    assert (tmp_path / result["path"]).read_bytes() == content
    # force → 不吃缓存（重抓就要真出网）
    assert http.calls == [("https://arxiv.org/pdf/2501.00001", False)]
    row = ctx.db.query_one(
        "SELECT sha256, raw_path, mime, extract_status, extract_engine, http_status, fetched_at"
        " FROM documents WHERE source_id = ?",
        (source_id,),
    )
    assert row is not None
    assert row["sha256"] == sha
    assert row["raw_path"] == result["path"]
    assert row["mime"] == "application/pdf"
    # 超阈值：原件落盘但**不抽正文**（不假装 ok）
    assert row["extract_status"] == "skipped_large"
    assert row["extract_engine"] == "none"
    assert row["http_status"] == 200


# ------------------------------------------------------------------ 3 去重 / 写库


def test_already_on_disk_is_not_refetched(ctx: AppContext, tmp_path: Path) -> None:
    """``already:true``：原件已在盘上 → 直接回路径与字节数，**一次网都不出**。"""
    _enable(ctx)
    source_id, _ = upsert_source(ctx.db, SOURCE)
    doc_id = insert_document(ctx.db, source_id=source_id)
    rel = "data/raw/unknown/paper/A-Test-Paper--deadbeef.pdf"
    on_disk = tmp_path / rel
    on_disk.parent.mkdir(parents=True, exist_ok=True)
    on_disk.write_bytes(PDF_BYTES)
    ctx.db.execute("UPDATE documents SET raw_path = ? WHERE id = ?", (rel, doc_id))
    http = StubHttp(error=FetchError("network", "桩：这一路不该出网"))
    ctx.http = http

    result = dispatch(ctx, "archive.download", {"doc_id": doc_id})

    assert result["ok"] is True
    assert result["already"] is True
    assert result["path"] == rel
    assert result["bytes"] == len(PDF_BYTES)
    assert result["ext"] == "pdf"
    assert http.calls == []


def test_force_refetches_even_when_file_is_on_disk(ctx: AppContext, tmp_path: Path) -> None:
    """``force:true`` 时"盘上有"不算数：真重抓，落到新 sha 的名下。"""
    _enable(ctx)
    source_id, _ = upsert_source(ctx.db, SOURCE)
    doc_id = insert_document(ctx.db, source_id=source_id)
    stale = tmp_path / "data" / "raw" / "unknown" / "paper" / "A-Test-Paper--deadbeef.pdf"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes("%PDF-1.4 旧件".encode())
    ctx.db.execute(
        "UPDATE documents SET raw_path = ? WHERE id = ?",
        (str(stale.relative_to(tmp_path)).replace("\\", "/"), doc_id),
    )
    content = b"%PDF-1.4 " + b"new" * 40
    http = StubHttp(content=content, media_type="application/pdf")
    ctx.http = http

    result = dispatch(ctx, "archive.download", {"doc_id": doc_id, "force": True})

    sha8 = hashlib.sha256(content).hexdigest()[:8]
    assert http.calls == [("https://arxiv.org/pdf/2501.00001", False)]
    assert result["ok"] is True
    assert result["already"] is False
    assert result["path"].endswith(f"A-Test-Paper--{sha8}.pdf")
    assert (tmp_path / result["path"]).read_bytes() == content


def test_update_existing_document_keeps_sha256_anchor(ctx: AppContext, tmp_path: Path) -> None:
    """已有 ``documents`` 行：只刷新 raw_path/mime/http_status/fetched_at，**sha256 不动**（docs/19 §0）。"""
    _enable(ctx)
    source_id, _ = upsert_source(ctx.db, SOURCE)
    doc_id = insert_document(ctx.db, source_id=source_id)  # sha256 = 'deadbeef'
    http = StubHttp(content=PDF_BYTES, media_type="application/pdf")
    ctx.http = http

    result = dispatch(ctx, "archive.download", {"doc_id": doc_id})

    sha8 = hashlib.sha256(PDF_BYTES).hexdigest()[:8]
    assert result["ok"] is True
    assert result["doc_id"] == doc_id
    assert result["path"] == f"data/raw/unknown/paper/A-Test-Paper--{sha8}.pdf"
    assert result["large"] is False
    assert (tmp_path / result["path"]).read_bytes() == PDF_BYTES
    row = ctx.db.query_one(
        "SELECT sha256, raw_path, mime, http_status, extract_status, fetched_at"
        " FROM documents WHERE id = ?",
        (doc_id,),
    )
    assert row is not None
    assert row["sha256"] == "deadbeef"  # 去重锚：下载不许改
    assert row["raw_path"] == result["path"]
    assert row["mime"] == "application/pdf"
    assert row["http_status"] == 200
    assert row["extract_status"] == "ok"  # 下载不改"有没有抽正文"这件事
    assert row["fetched_at"] != T0
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 1  # 不新建行


def test_url_only_creates_source_and_document_rows(ctx: AppContext, tmp_path: Path) -> None:
    """只给 url：先补 ``sources`` 行再插 ``documents``（``extract_status=pending``：正文还没抽）。"""
    _enable(ctx)
    http = StubHttp(content=PDF_BYTES, media_type="application/pdf")
    ctx.http = http

    result = dispatch(ctx, "archive.download", {"url": "https://example.org/file.pdf"})

    assert result["ok"] is True
    assert result["source_id"] and result["doc_id"]
    sha8 = hashlib.sha256(PDF_BYTES).hexdigest()[:8]
    # 标题取不到 → 回落成域名（绝不编标题），格式目录 web → doc
    assert result["path"] == f"data/raw/unknown/doc/example.org--{sha8}.pdf"
    row = ctx.db.query_one(
        "SELECT s.url, s.source_type, d.extract_status, d.raw_path FROM documents d"
        " JOIN sources s ON s.id = d.source_id WHERE d.id = ?",
        (result["doc_id"],),
    )
    assert row is not None
    assert row["url"] == "https://example.org/file.pdf"  # 存的是**用户给的那个地址**（去原站入口）
    assert row["source_type"] == "web"
    assert row["extract_status"] == "pending"
    assert (tmp_path / row["raw_path"]).is_file()


# ------------------------------------------------------------------ 4/5 失败一律 ok:false


@pytest.mark.parametrize(
    ("error", "expected_status", "hint"),
    [
        (
            FetchError("not_found", "不存在（404）：https://arxiv.org/pdf/2501.00001", status=404),
            404,
            "404",
        ),
        (
            FetchError("forbidden", "被拒绝（403）：https://arxiv.org/pdf/2501.00001", status=403),
            403,
            "403",
        ),
        (
            FetchError("server", "服务端错误（503）：https://arxiv.org/pdf/2501.00001", status=503),
            503,
            "503",
        ),
        (FetchError("too_large", "响应超过上限 20000000 字节：25000000"), None, "上限"),
        (FetchError("network", "连接失败：桩"), None, "连接失败"),
        (FetchError("timeout", "请求超时（30s）"), None, "超时"),
    ],
)
def test_fetch_errors_return_ok_false_and_never_raise(
    ctx: AppContext, tmp_path: Path, error: FetchError, expected_status: int | None, hint: str
) -> None:
    """非 2xx / 网络异常 / 超硬上限：一律 ``ok:false`` + 原因，**不抛**（界面靠 reason 显示）。"""
    _enable(ctx)
    source_id, _ = upsert_source(ctx.db, SOURCE)
    ctx.http = StubHttp(error=error)

    result = dispatch(ctx, "archive.download", {"source_id": source_id})

    assert result["ok"] is False
    assert hint in result["reason"]
    assert result["http_status"] == expected_status
    assert result["threshold_bytes"] == 20_000_000
    assert result["source_id"] == source_id
    assert _raw_files(tmp_path) == []


def test_write_failure_is_reported_not_raised(ctx: AppContext, tmp_path: Path) -> None:
    """落盘失败（真实文件系统错误，不 mock pathlib）：``ok:false`` + 「落盘失败」原因。"""
    _enable(ctx)
    raw = tmp_path / "data" / "raw"
    shutil.rmtree(raw)
    raw.write_bytes(b"not a directory")  # data/raw 变成文件 → mkdir 必炸
    ctx.http = StubHttp(content=PDF_BYTES, media_type="application/pdf")

    result = dispatch(ctx, "archive.download", {"url": "https://example.org/file.pdf"})

    assert result["ok"] is False
    assert "落盘失败" in result["reason"]
    assert result["bytes"] == len(PDF_BYTES)
    assert result["http_status"] == 200


def test_unknown_doc_id_returns_reason_instead_of_raising(ctx: AppContext) -> None:
    _enable(ctx)
    ctx.http = StubHttp(error=FetchError("network", "不该被调到"))

    result = dispatch(ctx, "archive.download", {"doc_id": "doc_不存在"})

    assert result["ok"] is False
    assert "库里没有这条资料" in result["reason"]


def test_missing_all_identifiers_is_invalid_params(ctx: AppContext) -> None:
    _enable(ctx)
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "archive.download", {})
    assert exc.value.code == -32602


def test_source_without_url_cannot_be_downloaded(ctx: AppContext) -> None:
    """库里那条来源没记源地址 → 如实回原因（不编一个地址去抓）。"""
    _enable(ctx)
    source_id, _ = upsert_source(ctx.db, {**SOURCE, "url": "", "url_canonical": "urn:empty"})
    ctx.db.execute("UPDATE sources SET url = '' WHERE id = ?", (source_id,))
    ctx.http = StubHttp(error=FetchError("network", "不该被调到"))

    result = dispatch(ctx, "archive.download", {"source_id": source_id})

    assert result["ok"] is False
    assert "没有记录源地址" in result["reason"]


def test_without_http_client_returns_reason(ctx: AppContext) -> None:
    _enable(ctx)
    ctx.http = None

    result = dispatch(ctx, "archive.download", {"url": SOURCE["url"]})

    assert result["ok"] is False
    assert "出网客户端" in result["reason"]
