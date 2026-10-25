"""产物入库：``py.exec`` 写出的 docx/xlsx/pdf 要**立刻**出现在资料库/路线/笔记里（docs/23 缺陷 1）。

缺陷现场：模型用 ``py.exec`` 把 ``毕业论文.docx`` 写到 ``data/exports/``，文件确实在，
但 ``archive.list``（资料库列表）、``data/library/index.md``（索引）、路线与笔记的
``source_id`` 全都看不到它 —— 只能重启或手工重建。根因是**产物从来没有进过库**：
``py.exec`` 只回 stdout/stderr，``library/index.py`` 只渲染 ``documents JOIN sources``。

修法口径（本文件就是它的契约）：

1. **事件驱动**：``py.exec`` 成功返回前扫一次工作目录，只登记新/变文件；
2. **增量**：判据是 ``mtime_ns + size``，两个都没变就**不重算 sha256、不重写索引**；
3. **幂等**：同一份产物扫两次，``documents`` / ``sources`` / ``source_mentions`` 都不长行；
4. **失败不拖垮主流程**：索引失败只让回包标 ``indexed=false`` + ``index_error``，产物仍算成功。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scholarscout import paths
from scholarscout.library import artifacts as artifact_index
from scholarscout.library import index as library_index
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime.toolctx import ToolContext
from scholarscout.state import archive, learning, research_board, source_context
from scholarscout.tools.py_exec import py_exec

from ..conftest import insert_session, redirect_home

#: 真写一个 docx 容器（zip + word/document.xml）：产物是二进制容器，不是 Markdown。
DOCX_SNIPPET = (
    "import zipfile\n"
    "with zipfile.ZipFile('毕业论文.docx', 'w') as z:\n"
    "    z.writestr('word/document.xml', '<w:document/>')\n"
    "print('wrote docx')\n"
)
#: 什么都不写的片段：用来证明「没有新文件 → 不重扫、不重写索引」。
NOOP_SNIPPET = "print('nothing')\n"


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """落盘根重定向到 tmp（``data/exports``、``data/library`` 都跟着进 tmp）。"""
    return redirect_home(monkeypatch, tmp_path)


def _ctx(ctx: AppContext, session: str = "ses_py_exec") -> ToolContext:
    return ToolContext(db=ctx.db, config=ctx.config, session_id=session)


def _exports(home: Path) -> Path:
    return home / "data" / "exports"


def _run(ctx: AppContext, snippet: str, **kwargs: Any) -> dict[str, Any]:
    return py_exec(_ctx(ctx), code=snippet, approval="once", **kwargs)


# --------------------------------------------------------------------------- #
# ① 生成后立刻能查到（资料库列表 + 索引文件 + 回包 artifacts）
# --------------------------------------------------------------------------- #


def test_generated_docx_is_visible_in_library_and_index_immediately(
    ctx: AppContext, tmp_path: Path
) -> None:
    insert_session(ctx.db, "ses_py_exec")
    events: list[tuple[str, dict[str, Any]]] = []
    tool_ctx = ToolContext(
        db=ctx.db,
        config=ctx.config,
        session_id="ses_py_exec",
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    result = py_exec(tool_ctx, code=DOCX_SNIPPET, approval="once")

    assert result["ok"] is True
    data = result["data"]
    assert data["indexed"] is True
    assert data["index_error"] == ""
    assert [Path(item["path"]).name for item in data["artifacts"]] == ["毕业论文.docx"]
    assert data["artifact_scan"] == {"added": 1, "updated": 0, "unchanged": 0, "refreshed": True}

    rows, total = archive.list_documents(ctx.db)
    assert total == 1
    row = rows[0]
    assert row["source_type"] == "artifact"
    assert row["title"] == "毕业论文"
    assert row["raw_path"] == "data/exports/毕业论文.docx"
    assert row["session_id"] == "ses_py_exec"

    body = (tmp_path / "data" / "library" / "index.md").read_text(encoding="utf-8")
    assert "毕业论文" in body
    assert "data/exports/毕业论文.docx" in body
    assert "共 1 条。" in body

    # 事件驱动：界面靠这条通知刷新资料库/路线/笔记，不必等切页签
    assert [kind for kind, _ in events] == ["library.updated"]
    assert events[0][1]["added"] == 1
    assert events[0][1]["session_id"] == "ses_py_exec"


# --------------------------------------------------------------------------- #
# ② 幂等：重复触发不重复入库
# --------------------------------------------------------------------------- #


def test_second_run_does_not_register_the_same_product_twice(
    ctx: AppContext, tmp_path: Path
) -> None:
    insert_session(ctx.db, "ses_py_exec")

    first = _run(ctx, DOCX_SNIPPET)
    second = _run(ctx, DOCX_SNIPPET)

    assert first["data"]["artifact_scan"]["added"] == 1
    assert second["ok"] is True
    assert second["data"]["artifact_scan"]["added"] == 0
    assert second["data"]["artifacts"] == []  # 没有"新东西"就不报
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 1
    assert ctx.db.scalar_int("SELECT count(*) FROM sources") == 1
    assert ctx.db.scalar_int("SELECT count(*) FROM source_mentions") == 1
    assert ctx.db.scalar_int("SELECT count(*) FROM documents WHERE source_id IS NULL") == 0
    body = (tmp_path / "data" / "library" / "index.md").read_text(encoding="utf-8")
    assert body.count("毕业论文.docx") == 1


# --------------------------------------------------------------------------- #
# ③ 索引失败：产物仍算成功，回包标 indexed=false
# --------------------------------------------------------------------------- #


def test_index_failure_keeps_the_product_and_flags_indexed_false(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_session(ctx.db, "ses_py_exec")

    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise OSError("disk full")

    monkeypatch.setattr(artifact_index, "write_indexes", boom)

    result = _run(ctx, DOCX_SNIPPET)

    assert result["ok"] is True, "索引失败不许把产物判成失败"
    data = result["data"]
    assert data["indexed"] is False
    assert "disk full" in data["index_error"]
    assert data["artifact_scan"]["added"] == 1
    # 产物本身：磁盘上有、库里已登记（只是索引没刷新）
    assert (_exports(tmp_path) / "毕业论文.docx").is_file()
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 1
    assert data["stdout"].strip() == "wrote docx"


# --------------------------------------------------------------------------- #
# ④ 增量：mtime + size 没变就既不重算 sha256，也不重写索引
# --------------------------------------------------------------------------- #


def test_unchanged_file_is_not_rehashed_nor_reindexed(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(ctx, DOCX_SNIPPET)
    # 钉住时间：第二次扫描与第一次看到的 mtime/size 完全一致（真实文件系统也如此，这里防抖动）
    target = _exports(tmp_path) / "毕业论文.docx"
    stat = target.stat()

    hashed: list[Path] = []
    real = artifact_index._sha256

    def counting(path: Path, **kwargs: Any) -> str:
        hashed.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(artifact_index, "_sha256", counting)

    scan = artifact_index.scan_artifacts(ctx.db, _exports(tmp_path), session_id="ses_py_exec")

    assert hashed == [], "mtime+size 都没变就不该再读一遍文件算哈希"
    assert scan["added"] == 0 and scan["updated"] == 0 and scan["unchanged"] == 1
    assert scan["refreshed"] is False, "没有变化就不该重写 index.md"
    assert target.stat().st_size == stat.st_size


def test_changed_content_updates_the_same_row_and_refreshes_once(
    ctx: AppContext, tmp_path: Path
) -> None:
    insert_session(ctx.db, "ses_py_exec")
    _run(ctx, DOCX_SNIPPET)
    # 改动内容（大小不同，保证 mtime+size 判据一定命中）
    changed = DOCX_SNIPPET.replace("<w:document/>", "<w:document><w:body/></w:document>")
    result = _run(ctx, changed)

    scan = result["data"]["artifact_scan"]
    assert scan["updated"] == 1 and scan["added"] == 0
    assert ctx.db.scalar_int("SELECT count(*) FROM documents") == 1, "同一个文件只该有一行"
    row = ctx.db.query_one("SELECT sha256, title FROM documents")
    assert row is not None and row["title"] == "毕业论文"
    assert result["data"]["indexed"] is True


# --------------------------------------------------------------------------- #
# ⑤ 路线 / 笔记的引用能解析到新产物（source_id 是真的、能反查）
# --------------------------------------------------------------------------- #


def test_route_and_note_references_resolve_to_the_new_product(
    ctx: AppContext, tmp_path: Path
) -> None:
    insert_session(ctx.db, "ses_py_exec")
    result = _run(ctx, DOCX_SNIPPET)
    product = result["data"]["artifacts"][0]
    source_id = str(product["source_id"])

    plan = {
        "mode": "model",
        "weeks": [
            {
                "idx": 1,
                "goal": "读自己的产物",
                "items": [
                    {
                        "kind": "doc",
                        "title": "毕业论文",
                        "why": "这是本次生成的成品",
                        "source_id": source_id,
                    }
                ],
            }
        ],
    }
    route_id = learning.save_route(ctx.db, topic="产物引用", plan=plan, session_id="ses_py_exec")
    loaded = learning.get_route(ctx.db, route_id)
    assert loaded is not None
    assert loaded["plan"]["weeks"][0]["items"][0]["source_id"] == source_id

    note_id = learning.save_note(
        ctx.db,
        kind="note",
        content="产物见附件",
        title="产物笔记",
        refs={"source_ids": [source_id]},
        session_id="ses_py_exec",
    )
    assert [hit["id"] for hit in learning.notes_for_source(ctx.db, source_id)] == [note_id]

    # 路线/笔记里那个 id 落到 sources 上必须真能取到，且能点回文件
    source = source_context.source_by_id(ctx.db, source_id)
    assert source is not None and source["source_type"] == "artifact"
    assert research_board.sources_by_ids(ctx.db, [source_id])[source_id]["title"] == "毕业论文"
    document = archive.get_document(ctx.db, str(product["doc_id"]))
    assert document is not None
    assert document["document"]["raw_path"] == product["path"]
    assert archive.document_paths(ctx.db, str(product["doc_id"]))["exists"] == {
        "raw_path": True,
        "text_path": False,
    }
    # 索引里也有它（资料库/学习页共用的那份口径）
    assert [row["doc_id"] for row in library_index.build_rows(ctx.db)] == [str(product["doc_id"])]


# --------------------------------------------------------------------------- #
# ⑥ 空目录 / 不存在的目录不崩
# --------------------------------------------------------------------------- #


def test_empty_or_missing_workdir_does_not_crash(ctx: AppContext, tmp_path: Path) -> None:
    missing = tmp_path / "data" / "no_such_dir"
    result = artifact_index.scan_artifacts(ctx.db, missing)
    assert result["added"] == 0 and result["updated"] == 0 and result["refreshed"] is False

    empty = tmp_path / "data" / "empty_exports"
    empty.mkdir(parents=True)
    result = artifact_index.scan_artifacts(ctx.db, empty)
    assert result["added"] == 0 and result["unchanged"] == 0
    assert result["indexed"] is True

    # py.exec 真跑一遍但一个文件都没写：产物为空、索引照样成功
    tool = _run(ctx, NOOP_SNIPPET)
    assert tool["ok"] is True
    assert tool["data"]["indexed"] is True
    assert tool["data"]["artifacts"] == []


# --------------------------------------------------------------------------- #
# ⑦ 只登记工作目录里真实的文件：不碰隐藏文件与半成品
# --------------------------------------------------------------------------- #


def test_hidden_and_partial_files_are_skipped(ctx: AppContext, tmp_path: Path) -> None:
    exports = _exports(tmp_path)
    exports.mkdir(parents=True, exist_ok=True)
    (exports / ".DS_Store").write_bytes(b"junk")
    (exports / "~$report.docx").write_bytes(b"lock")
    (exports / "report.docx.tmp").write_bytes(b"partial")
    (exports / "report.xlsx").write_bytes(b"real")

    scan = artifact_index.scan_artifacts(ctx.db, exports, session_id="ses_x")

    assert scan["added"] == 1
    rows, _total = archive.list_documents(ctx.db)
    assert [row["raw_path"] for row in rows] == ["data/exports/report.xlsx"]
    assert paths.rel_to_home(exports / "report.xlsx") == "data/exports/report.xlsx"
