"""入库层契约：来源去重、文档去重、摘要版本化、出处关联。"""

from __future__ import annotations

from scholarscout.state.db import Database
from scholarscout.state.ingest import (
    latest_summary,
    record_mention,
    save_document,
    save_summary,
    upsert_source,
)
from scholarscout.state.sessions import create_session

from .test_schema import EXPECTED_TABLES  # noqa: F401  （保持与本目录其它用例一致的导入风格）

SOURCE_ROW = {
    "url": "https://arxiv.org/abs/2501.00001",
    "url_canonical": "https://arxiv.org/abs/2501.00001",
    "domain": "arxiv.org",
    "title": "A Paper",
    "authors_json": ["Ada"],
    "year": 2025,
    "venue": "arXiv",
    "doi": None,
    "source_type": "arxiv",
    "meta_json": {"external_id": "2501.00001"},
}


class FakeDraft:
    """冒充 SummaryDraft（入库只用到这几个属性）。"""

    def __init__(self, version: str = "v1", text: str = "摘要正文") -> None:
        self.kind = "paper"
        self.title_zh = "中文标题"
        self.summary_short = text
        self.points = ("要点一",)
        self.why_relevant = None
        self.model = "stub"
        self.prompt_version = version
        self.input_chars = 100
        self.input_truncated = False
        self.degraded = False
        self.guard_failed = False
        self.error = None

    def points_json(self) -> str:
        return '["要点一"]'


def test_upsert_source_creates_then_reuses(db: Database) -> None:
    first_id, created = upsert_source(db, SOURCE_ROW)
    assert created is True
    assert first_id.startswith("src_")

    same_id, created_again = upsert_source(db, {**SOURCE_ROW, "title": "A Paper v2"})
    assert created_again is False
    assert same_id == first_id
    assert db.scalar_int("SELECT count(*) FROM sources") == 1
    assert db.scalar_str("SELECT title FROM sources") == "A Paper v2"


def test_upsert_source_different_canonical_creates_new(db: Database) -> None:
    upsert_source(db, SOURCE_ROW)
    upsert_source(db, {**SOURCE_ROW, "url_canonical": "https://arxiv.org/abs/2501.00002"})
    assert db.scalar_int("SELECT count(*) FROM sources") == 2


def test_save_document_dedupes_by_sha(db: Database) -> None:
    source_id, _ = upsert_source(db, SOURCE_ROW)
    doc_id, created = save_document(db, source_id=source_id, sha256="abc", chars=10)
    assert created is True
    again, created_again = save_document(db, source_id=source_id, sha256="abc", chars=999)
    assert created_again is False
    assert again == doc_id
    assert db.scalar_int("SELECT count(*) FROM documents") == 1


def test_save_document_keeps_extraction_status(db: Database) -> None:
    source_id, _ = upsert_source(db, SOURCE_ROW)
    doc_id, _ = save_document(
        db,
        source_id=source_id,
        sha256="def",
        extract_status="no_text_layer",
        extract_engine="local",
        truncated=True,
        text_path="data/text/doc_x.txt",
    )
    row = db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))
    assert row is not None
    assert row["extract_status"] == "no_text_layer"
    assert row["truncated"] == 1
    assert row["text_path"] == "data/text/doc_x.txt"


def test_save_summary_is_idempotent_per_prompt_version(db: Database) -> None:
    source_id, _ = upsert_source(db, SOURCE_ROW)
    doc_id, _ = save_document(db, source_id=source_id, sha256="abc")
    first = save_summary(db, FakeDraft("v1", "第一版摘要"), doc_id=doc_id, source_id=source_id)
    second = save_summary(db, FakeDraft("v1", "重跑后的摘要"), doc_id=doc_id, source_id=source_id)
    assert first == second, "同版本重跑应更新同一行"
    assert db.scalar_int("SELECT count(*) FROM summaries") == 1
    assert latest_summary(db, doc_id)["summary_short"] == "重跑后的摘要"


def test_save_summary_new_version_creates_second_row(db: Database) -> None:
    source_id, _ = upsert_source(db, SOURCE_ROW)
    doc_id, _ = save_document(db, source_id=source_id, sha256="abc")
    save_summary(db, FakeDraft("v1"), doc_id=doc_id, source_id=source_id)
    save_summary(db, FakeDraft("v2"), doc_id=doc_id, source_id=source_id)
    assert db.scalar_int("SELECT count(*) FROM summaries") == 2
    assert db.scalar_int("SELECT count(*) FROM summaries WHERE prompt_version = 'v2'") == 1


def test_summary_index_is_searchable_after_ingest(db: Database) -> None:
    from scholarscout.state.search import search_summaries

    source_id, _ = upsert_source(db, SOURCE_ROW)
    doc_id, _ = save_document(db, source_id=source_id, sha256="abc")
    save_summary(
        db, FakeDraft("v1", "该工作在教育智能体上做了检索增强"), doc_id=doc_id, source_id=source_id
    )
    assert search_summaries(db, "智能体")
    assert search_summaries(db, "教育")


def test_record_mention_links_source_to_session(db: Database) -> None:
    source_id, _ = upsert_source(db, SOURCE_ROW)
    doc_id, _ = save_document(db, source_id=source_id, sha256="abc")
    summary_id = save_summary(db, FakeDraft(), doc_id=doc_id, source_id=source_id)
    session_id = create_session(db, source="eval")
    record_mention(
        db,
        source_id=source_id,
        session_id=session_id,
        doc_id=doc_id,
        summary_id=summary_id,
        turn_id="trn_1",
        role="assistant",
    )
    row = db.query_one("SELECT * FROM source_mentions WHERE source_id = ?", (source_id,))
    assert row is not None
    assert row["session_id"] == session_id and row["turn_id"] == "trn_1"


def test_latest_summary_none_for_unknown_doc(db: Database) -> None:
    assert latest_summary(db, "doc_none") is None
