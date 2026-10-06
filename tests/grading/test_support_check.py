"""引用语义支持度校验（`grading/support.py`）。

这是第 5 章那条"覆盖率≠语义支持度"局限的补法，所以测的重点不是"跑得通"，而是
**这三件事不能被做错**：

1. 证据分两层必须分开算（标题级证据算进总支持率会被字面重合污染）；
2. 输出解析不出来就是 `unknown`，**绝不能默认成 supported**（否则指标虚高）；
3. 只审资料类条目（行动类不声称出处，与 ungrounded 口径一致）—— 否则分母里混进练习安排。
"""

from __future__ import annotations

import json

from scholarscout.grading import support
from scholarscout.state.db import Database


def seed_source(
    db: Database, source_id: str, *, title: str = "A Paper", with_summary: bool = False
) -> None:
    db.execute(
        "INSERT INTO sources (id, url, url_canonical, domain, title, source_type,"
        " first_seen_at, last_seen_at, year) VALUES (?, ?, ?, 'arxiv.org', ?, 'arxiv',"
        " '2026-10-05T10:00:00', '2026-10-05T10:00:00', 2025)",
        (
            source_id,
            f"https://arxiv.org/abs/{source_id}",
            f"https://arxiv.org/abs/{source_id}",
            title,
        ),
    )
    if with_summary:
        # 摘要挂在文档上（`summaries.doc_id` 非空）—— 先有文档行，才有摘要
        doc_id = f"doc_{source_id}"
        db.execute(
            "INSERT INTO documents (id, source_id, title, sha256, chars, raw_path, text_path,"
            " extract_status, fetched_at) VALUES (?, ?, ?, 'deadbeef', 1234,"
            " 'data/raw/de/deadbeef.pdf', ?, 'ok', '2026-10-05T10:00:00')",
            (doc_id, source_id, title, f"data/text/{doc_id}.txt"),
        )
        db.execute(
            "INSERT INTO summaries (id, doc_id, source_id, kind, title_zh, summary_short,"
            " points_json, prompt_version, created_at) VALUES (?, ?, ?, 'paper', ?, ?, ?, 'v1', 'x')",
            (
                f"sum_{source_id}",
                doc_id,
                source_id,
                "中文标题",
                "这是摘要正文，说明该工作提出了自适应支架框架。",
                json.dumps(["要点一", "要点二"], ensure_ascii=False),
            ),
        )


def seed_map(db: Database, nodes: list[dict]) -> str:
    db.execute(
        "INSERT INTO topic_maps (id, session_id, topic, graph_json, created_at)"
        " VALUES ('tm1', NULL, 'T', ?, 'x')",
        (json.dumps({"nodes": nodes}, ensure_ascii=False),),
    )
    return "tm1"


def seed_route(db: Database, weeks: list[dict]) -> str:
    db.execute(
        "INSERT INTO learning_routes (id, session_id, topic, plan_json, created_at)"
        " VALUES ('lr1', NULL, 'T', ?, 'x')",
        (json.dumps({"weeks": weeks}, ensure_ascii=False),),
    )
    return "lr1"


# ------------------------------------------------------------------ 配对


def test_map_pairs_skip_nodes_without_sources(db: Database) -> None:
    seed_source(db, "src_a")
    artifact = seed_map(
        db,
        [
            {"nid": "n1", "label": "有出处", "kind": "concept", "source_ids": ["src_a"]},
            {"nid": "n2", "label": "没出处", "kind": "concept", "source_ids": []},
        ],
    )
    pairs = support.build_pairs(db, "topic_map", artifact)
    assert [pair.item_id for pair in pairs] == ["n1"]


def test_route_pairs_skip_exercises(db: Database) -> None:
    """行动类（exercise）不声称出处，纳入就污染分母。"""
    seed_source(db, "src_a")
    artifact = seed_route(
        db,
        [
            {
                "idx": 1,
                "items": [
                    {"kind": "paper", "title": "论文", "why": "读它", "source_id": "src_a"},
                    {"kind": "exercise", "title": "练习", "why": "动手", "source_id": None},
                ],
            }
        ],
    )
    pairs = support.build_pairs(db, "route", artifact)
    assert len(pairs) == 1 and pairs[0].claim.startswith("论文")


def test_route_item_id_uses_week_idx_and_never_has_question_mark(db: Database) -> None:
    """周号字段是 `idx` —— 取错键会编出 `w?-0` 这种编号（真踩过）。"""
    seed_source(db, "src_a")
    artifact = seed_route(
        db, [{"idx": 3, "items": [{"kind": "paper", "title": "X", "source_id": "src_a"}]}]
    )
    pairs = support.build_pairs(db, "route", artifact)
    assert pairs[0].item_id == "w3-0"
    assert "?" not in pairs[0].item_id


def test_evidence_level_reflects_what_we_actually_have(db: Database) -> None:
    seed_source(db, "src_sum", with_summary=True)
    seed_source(db, "src_title")
    artifact = seed_map(
        db,
        [
            {"nid": "n1", "label": "有摘要", "source_ids": ["src_sum"]},
            {"nid": "n2", "label": "仅标题", "source_ids": ["src_title"]},
        ],
    )
    levels = {
        pair.item_id: pair.evidence_level for pair in support.build_pairs(db, "topic_map", artifact)
    }
    assert levels == {"n1": "summary", "n2": "title"}


def test_evidence_text_includes_summary_and_points(db: Database) -> None:
    seed_source(db, "src_sum", with_summary=True)
    artifact = seed_map(db, [{"nid": "n1", "label": "X", "source_ids": ["src_sum"]}])
    pair = support.build_pairs(db, "topic_map", artifact)[0]
    assert "摘要：" in pair.evidence_text and "要点：要点一" in pair.evidence_text


def test_missing_source_is_reported_not_skipped(db: Database) -> None:
    """引用了库里没有的来源 → 证据文本要说清，而不是悄悄跳过（那会把问题藏起来）。"""
    artifact = seed_map(db, [{"nid": "n1", "label": "X", "source_ids": ["src_ghost"]}])
    pair = support.build_pairs(db, "topic_map", artifact)[0]
    assert "库中没有这条来源" in pair.evidence_text


def test_unknown_artifact_kind_raises(db: Database) -> None:
    try:
        support.build_pairs(db, "note", "x")
    except ValueError as exc:
        assert "不认识的产物类型" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应当报错")


# ------------------------------------------------------------------ 解析


def test_parse_clean_json() -> None:
    assert support.parse_verdict('{"verdict": "supported", "reason": "摘要要点1提到"}') == (
        "supported",
        "摘要要点1提到",
    )


def test_parse_json_wrapped_in_prose_or_fence() -> None:
    text = '好的，判断如下：\n```json\n{"verdict":"partial","reason":"仅标题"}\n```'
    assert support.parse_verdict(text)[0] == "partial"


def test_parse_garbage_is_unknown_not_supported() -> None:
    """解析失败必须落到 unknown —— 默认成 supported 会让指标虚高（最坏的错法）。"""
    assert support.parse_verdict("我无法判断")[0] == support.UNKNOWN
    assert support.parse_verdict("")[0] == support.UNKNOWN
    assert support.parse_verdict(None)[0] == support.UNKNOWN  # type: ignore[arg-type]


def test_parse_keyword_fallback() -> None:
    assert support.parse_verdict("结论：unsupported，因为标题里没有该机制")[0] == "unsupported"


# ------------------------------------------------------------------ 汇总


class FakeReply:
    def __init__(self, content: str) -> None:
        self.content = content
        self.model = "fake"


class FakeClient:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return FakeReply(self.answer)


def pair(level: str = "title", item: str = "n1") -> support.SupportPair:
    return support.SupportPair(
        artifact_kind="topic_map",
        artifact_id="tm1",
        item_id=item,
        claim="X",
        source_ids=("src_a",),
        evidence_level=level,
        evidence_text="证据",
    )


def test_summarise_splits_by_evidence_level(db: Database) -> None:
    rows = [
        (pair("summary", "n1"), support.SupportVerdict("supported", "")),
        (pair("summary", "n2"), support.SupportVerdict("unsupported", "")),
        (pair("title", "n3"), support.SupportVerdict("supported", "")),
    ]
    got = support.summarise(rows)
    assert got["supported_rate"] == 2 / 3
    assert got["by_evidence_level"]["summary"]["supported_rate"] == 0.5
    assert got["by_evidence_level"]["title"]["supported_rate"] == 1.0
    assert len(got["weak_cases"]) == 1


def test_unknown_is_excluded_from_the_denominator(db: Database) -> None:
    rows = [
        (pair(), support.SupportVerdict("supported", "")),
        (pair(item="n2"), support.SupportVerdict(support.UNKNOWN, "解析失败")),
    ]
    got = support.summarise(rows)
    assert got["checked"] == 1 and got["unknown"] == 1
    assert got["supported_rate"] == 1.0, "unknown 不参与分母，否则就是拿解析失败当通过"


def test_summarise_with_no_checked_items_gives_none() -> None:
    got = support.summarise([(pair(), support.SupportVerdict(support.UNKNOWN, ""))])
    assert got["supported_rate"] is None


# ------------------------------------------------------------------ 落库


def test_record_and_reuse(db: Database) -> None:
    seed_source(db, "src_a")
    artifact = seed_map(db, [{"nid": "n1", "label": "X", "source_ids": ["src_a"]}])
    item = support.build_pairs(db, "topic_map", artifact)[0]
    support.record_check(
        db, item, support.SupportVerdict("partial", "仅标题", model="m", prompt_version="v")
    )
    rows = support.list_checks(db, artifact)
    assert len(rows) == 1 and rows[0]["verdict"] == "partial" and rows[0]["prompt_version"] == "v"


def test_record_check_upserts_instead_of_duplicating(db: Database) -> None:
    """重复审同一件产物不能堆出重复行（否则报告里的分母会翻倍）。"""
    seed_source(db, "src_a")
    artifact = seed_map(db, [{"nid": "n1", "label": "X", "source_ids": ["src_a"]}])
    item = support.build_pairs(db, "topic_map", artifact)[0]
    support.record_check(db, item, support.SupportVerdict("partial", "a"))
    support.record_check(db, item, support.SupportVerdict("supported", "b"))
    rows = support.list_checks(db, artifact)
    assert len(rows) == 1 and rows[0]["verdict"] == "supported"


def test_judge_uses_the_client_and_keeps_the_prompt_version() -> None:
    client = FakeClient('{"verdict": "supported", "reason": "摘要里有"}')
    result = support.judge(client, pair("summary"), "提示词", version="abc123")
    assert client.calls == 1
    assert result.verdict == "supported" and result.prompt_version == "abc123"


def test_prompt_file_exists_and_hashes() -> None:
    prompt = support.load_prompt()
    assert "supported|partial|unsupported" in prompt
    assert len(support.prompt_version(prompt)) == 12


def test_messages_mark_the_evidence_level() -> None:
    """提示词必须知道证据强弱 —— 否则模型会把"看起来相关"当"支持"。"""
    strong = support.build_messages(pair("summary"), "P")
    weak = support.build_messages(pair("title"), "P")
    assert "强（含摘要与要点）" in strong[1]["content"]
    assert "弱（仅标题与元数据）" in weak[1]["content"]
