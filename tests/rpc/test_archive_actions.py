"""``archive.open_raw`` / ``archive.reveal`` 契约：路径白名单 + 交给系统程序。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scholarscout import paths
from scholarscout.rpc.errors import RpcError
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state.ingest import upsert_source

from ..conftest import insert_document, redirect_home

SOURCE = {
    "url": "https://arxiv.org/abs/2501.00001",
    "url_canonical": "https://arxiv.org/abs/2501.00001",
    "domain": "arxiv.org",
    "title": "教育智能体的检索增强方法",
    "source_type": "arxiv",
    "year": 2025,
}


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return redirect_home(monkeypatch, tmp_path)


def _document(ctx: AppContext) -> str:
    source_id, _ = upsert_source(ctx.db, SOURCE)
    doc_id = insert_document(ctx.db, source_id=source_id)
    return doc_id


def _place(doc_id: str) -> Path:
    raw = paths.raw_path_for("deadbeef", "pdf")
    text = paths.text_path_for(doc_id)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"%PDF-1.4 fake original")
    text.parent.mkdir(parents=True, exist_ok=True)
    text.write_text("正文纯文本", encoding="utf-8")
    return raw


def test_open_raw_launches_system_default(ctx: AppContext, monkeypatch) -> None:
    doc_id = _document(ctx)
    raw = _place(doc_id)
    launched: list[str] = []
    monkeypatch.setattr(
        "scholarscout.rpc.methods._launch_with_system", lambda target: launched.append(str(target))
    )

    result = dispatch(ctx, "archive.open_raw", {"doc_id": doc_id})
    assert result == {"ok": True, "path": paths.rel_to_home(raw)}
    assert launched == [str(raw)]


def test_open_raw_without_file_on_disk_is_not_found(ctx: AppContext) -> None:
    doc_id = _document(ctx)  # 只入库，不落盘
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "archive.open_raw", {"doc_id": doc_id})
    assert exc.value.code == -32002


def test_reveal_by_doc_id_selects_text_file(ctx: AppContext, monkeypatch) -> None:
    doc_id = _document(ctx)
    _place(doc_id)
    revealed: list[str] = []
    monkeypatch.setattr(
        "scholarscout.rpc.methods._reveal_in_file_manager",
        lambda target: revealed.append(str(target)),
    )

    result = dispatch(ctx, "archive.reveal", {"doc_id": doc_id, "which": "text"})
    assert result["which"] == "text"
    assert revealed == [str(paths.text_path_for(doc_id))]


def test_reveal_by_path_accepts_notes_export(ctx: AppContext, monkeypatch) -> None:
    note = paths.under_data(Path("notes") / "not_abc.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# 抽背卡", encoding="utf-8")
    revealed: list[str] = []
    monkeypatch.setattr(
        "scholarscout.rpc.methods._reveal_in_file_manager",
        lambda target: revealed.append(str(target)),
    )

    result = dispatch(ctx, "archive.reveal", {"path": paths.rel_to_home(note)})
    assert result["path"] == "data/notes/not_abc.md"
    assert revealed == [str(note)]


def test_reveal_refuses_escape(ctx: AppContext) -> None:
    with pytest.raises(ValueError, match="越出"):
        dispatch(ctx, "archive.reveal", {"path": "../../Windows/System32/drivers/etc/hosts"})


def test_reveal_missing_path_is_not_found(ctx: AppContext) -> None:
    with pytest.raises(RpcError) as exc:
        dispatch(ctx, "archive.reveal", {"path": "data/notes/不存在.md"})
    assert exc.value.code == -32002
