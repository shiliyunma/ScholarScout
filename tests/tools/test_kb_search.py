"""`kb.search` 的离线契约（docs/01 §5.8）：FTS5 检索、中文逐字命中、降级与增量重建。

全部用例**不出网、不调模型**：语料是临时目录里现造的几份 `data/text/*.txt` + 库内的
`documents`/`sources`/`summaries` 行。要证明的四件事：

1. **中文能被命中**：`unicode61` 把「深度学习」当一个 token，所以查「深度」必须靠
   `body_cjk` 逐字影子列 —— 这条用例造了 3 份文档，命中数必须正好是 1；
2. **正文不进资料库**：检索跑完后 `run/state.db`（含 WAL）里逐字节找不到正文里的句子，
   而索引文件里有 —— 见 `test_body_never_enters_library_db`；
3. **降级不静默**：SQLite 没带 FTS5 时走 `LIKE`，回包 `index_mode="like"` 且**有一条 WARNING**；
4. **增量**：只改一份正文 → `rebuild()` 的账是 `updated=1 / unchanged=2`。
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import registry
from scholarscout.runtime.prompts import build_system_prompt, system_prompt_hash
from scholarscout.runtime.turn_context import TurnContext
from scholarscout.state import kb_index
from scholarscout.state.db import Database
from scholarscout.state.kb_index import KbIndex, probe_fts5, visible_len
from scholarscout.tools import register_tools
from scholarscout.tools.kb_search import kb_search

from ..conftest import T0, StubLlm, redirect_home, tool_ctx

#: 正文里那句"只在文件里、不在库里"的话（逐字节验证用）
SECRET_LINE = "正文只该躺在 data/text 里，资料库表里一个字节都不许有。"


def _add_doc(
    tmp_path: Path,
    db: Database,
    doc_id: str,
    *,
    title: str,
    body: str,
    source_type: str = "arxiv",
    abstract: str = "",
    summary: str = "",
    tags: list[str] | None = None,
) -> str:
    """造一份资料：库内元数据（标题/摘要/标签）+ `data/text/` 正文。"""
    source_id = f"src_{doc_id}"
    meta = "{}" if not tags else '{"tags": ' + str(tags).replace("'", '"') + "}"
    db.execute(
        """
        INSERT INTO sources (id, url, url_canonical, domain, title, source_type,
                             first_seen_at, last_seen_at, meta_json)
        VALUES (?, ?, ?, 'example.org', ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            f"https://example.org/{doc_id}",
            f"https://example.org/{doc_id}",
            title,
            source_type,
            T0,
            T0,
            meta,
        ),
    )
    rel = f"data/text/{doc_id}.txt"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    db.execute(
        """
        INSERT INTO documents (id, source_id, title, abstract, sha256, chars, text_path,
                               extract_status, fetched_at, ok)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', ?, 1)
        """,
        (doc_id, source_id, title, abstract, doc_id, len(body), rel, T0),
    )
    if summary:
        db.execute(
            """
            INSERT INTO summaries (id, doc_id, source_id, kind, title_zh, summary_short,
                                   points_json, prompt_version, created_at)
            VALUES (?, ?, ?, 'paper', ?, ?, '[]', 'v1', ?)
            """,
            (f"sum_{doc_id}", doc_id, source_id, title, summary, T0),
        )
    return source_id


@pytest.fixture
def kb(ctx: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """重定向落盘 + 三份语料（一份含中文长词，一份英文，一份无关）。"""
    redirect_home(monkeypatch, tmp_path)
    _add_doc(
        tmp_path,
        ctx.db,
        "doc_dl",
        title="深度学习在课堂上的应用",
        body=f"{SECRET_LINE}\n深度学习让课堂反馈更快，本文讨论具体做法与边界。",
    )
    _add_doc(
        tmp_path,
        ctx.db,
        "doc_rl",
        title="强化学习与教育智能体",
        # 刻意**不写**「深度学习」：那个词只该被 doc_dl 命中（② 里 `total == 1` 的前提），
        # 而「学习」要能命中两条 —— 所以这里用「监督学习」，与「深度」彻底不重叠。
        body="强化学习用于学习路线规划；与监督学习相比样本效率更高。",
        source_type="github",
    )
    _add_doc(
        tmp_path,
        ctx.db,
        "doc_en",
        title="Transformer Models for Vision",
        body="Transformers now dominate vision benchmarks.",
    )
    return tmp_path


def _index(tmp_path: Path, db: Database) -> KbIndex:
    return KbIndex(db, root=tmp_path, index_path=tmp_path / "data" / "cache" / "kb-fts.sqlite3")


# ---------------------------------------------------------------- ① FTS5 可用性


def test_fts5_is_available_on_this_build(kb: Path, ctx: AppContext) -> None:
    """这台机器的 SQLite 带 FTS5，索引因此真用虚表（而不是悄悄走了 LIKE）。"""
    index = _index(kb, ctx.db)
    try:
        assert probe_fts5(index._idx) is True
        stats = index.rebuild(force=True)
        assert stats.mode == "fts5"
        # 3 份资料都建上了（正文文件都在）
        assert stats.docs == 3
        assert index.search("深度学习").mode == "fts5"
    finally:
        index.close()


# ---------------------------------------------------------------- ② 中文逐字命中


def test_cjk_compound_word_is_hit_by_its_prefix(kb: Path, ctx: AppContext) -> None:
    """「深度学习」能被「深度」「学习」命中 —— 靠 `body_cjk` 影子列，不是靠 unicode61。

    这条是中文检索的核心断言：只建 `body` 列时，`unicode61` 把「深度学习」切成一个 token，
    查「深度」命中 0 条。所以这里同时钉住「整词」和「子词」两个方向。
    """
    index = _index(kb, ctx.db)
    try:
        whole = index.search("深度学习")
        assert whole.total == 1
        assert whole.items[0].source_id == "src_doc_dl"
        assert "«深度学习»" in whole.items[0].snippet

        part = index.search("深度")
        assert [item.source_id for item in part.items] == ["src_doc_dl"]

        # 库里另有「强化学习」：查「学习」应命中两条（深度学习 + 强化学习）
        assert index.search("学习").total == 2
    finally:
        index.close()


def test_cjk_in_index_metadata_is_searchable(kb: Path, ctx: AppContext) -> None:
    """库内**摘要/标签**也在索引里（不只是正文）：只写在 abstract 里的词也要能查。"""
    _add_doc(
        kb,
        ctx.db,
        "doc_meta",
        title="An English Title",
        body="body without the keyword",
        abstract="这篇讲了知识点追踪与遗忘曲线",
        tags=["知识点追踪"],
    )
    index = _index(kb, ctx.db)
    try:
        out = index.search("遗忘曲线")
        assert [item.source_id for item in out.items] == ["src_doc_meta"]
    finally:
        index.close()


# ---------------------------------------------------------------- ③ 英文大小写/前缀


def test_english_is_case_insensitive_and_prefix_matched(kb: Path, ctx: AppContext) -> None:
    index = _index(kb, ctx.db)
    try:
        for query in ("transformer", "TRANSFORMERS", "transf", "VISIO"):
            out = index.search(query)
            assert out.total == 1, f"{query} 应该命中那份英文资料"
            assert out.items[0].source_id == "src_doc_en"
    finally:
        index.close()


# ---------------------------------------------------------------- ④ snippet


def test_snippet_highlights_and_truncates_to_300_chars(kb: Path, ctx: AppContext) -> None:
    """片段带 `« »` 高亮，且**可见字数**（不含标记）不超过 300。"""
    filler = "无关内容。" * 400
    _add_doc(
        kb,
        ctx.db,
        "doc_long",
        title="长文档",
        body=f"开头一段。\n目标词：知识点追踪。\n{filler}",
    )
    index = _index(kb, ctx.db)
    try:
        item = index.search("知识点追踪").items[0]
        assert "«知识点追踪»" in item.snippet
        assert visible_len(item.snippet) <= 300
        # 截断了才有省略号（长正文必然超 300 字）
        assert item.snippet.endswith("…")
        # 高亮标记不能被截成半个
        assert item.snippet.count("«") == item.snippet.count("»")
    finally:
        index.close()


# ---------------------------------------------------------------- ⑤ limit / total


def test_limit_caps_items_but_total_reports_all_hits(kb: Path, ctx: AppContext) -> None:
    index = _index(kb, ctx.db)
    try:
        out = index.search("学习", limit=1)
        assert out.total == 2
        assert len(out.items) == 1
        # 上限夹在 1–50（不是静默接受任意值）
        assert len(index.search("学习", limit=0).items) == 1
    finally:
        index.close()


# ---------------------------------------------------------------- ⑥ 空 query


def test_empty_query_is_a_param_error(kb: Path, ctx: AppContext) -> None:
    """空/纯空白 query → `ok:false` + 非空 error，**绝不**退化成「列出全部资料」。"""
    tc = tool_ctx(ctx)
    for bad in ("", "   ", "\n\t "):
        envelope = kb_search(tc, query=bad)
        assert envelope["ok"] is False
        assert "query" in str(envelope["error"])
        assert envelope["data"]["error_kind"] == "invalid_query"


# ---------------------------------------------------------------- ⑦ 开关门控


def test_enable_kb_switch_controls_registration_and_tool_list(kb: Path, ctx: AppContext) -> None:
    """开关是**真的**：关着时不注册、也不在模型清单里；开着时两样都有。

    这就是当初 `enable_kb` 作为"假开关"被删掉的反面：现在开着会真的多出一个工具，
    所以四条断言都指向"行为差异"，而不是"字段存在"。
    """
    registry.clear_registry()
    registered_off = register_tools(enable_kb=False)
    assert "kb.search" not in registered_off
    assert "kb.search" not in [spec.name for spec in registry.get_tools(["research", "kb"])]

    registry.clear_registry()
    registered_on = register_tools(enable_kb=True)
    assert "kb.search" in registered_on
    assert "kb.search" in [spec.name for spec in registry.get_tools(["research", "kb"])]

    off_config = replace(ctx.config, features=replace(ctx.config.features, enable_kb=False))
    on_config = replace(ctx.config, features=replace(ctx.config.features, enable_kb=True))
    assert registry.enabled_toolsets(["research"], enable_kb=False) == ("research",)
    assert registry.enabled_toolsets(["research"], enable_kb=True) == ("research", "kb")
    # 会话显式要 kb 也没用：门控在取工具那一层再收敛一次（RPC 的 toolsets 来自前端）
    off_turn = TurnContext(db=ctx.db, config=off_config, llm=StubLlm())
    on_turn = TurnContext(db=ctx.db, config=on_config, llm=StubLlm())
    assert "kb.search" not in [spec.name for spec in off_turn.tools(["research", "kb"])]
    assert "kb.search" in [spec.name for spec in on_turn.tools(["research"])]


def test_enable_kb_adds_one_tool_and_changes_the_prompt_hash() -> None:
    """开启态：工具 13 → 14，且系统提示词**只在这时才**多出 `KB_RULE`（哈希因此不同）。

    为什么单独一条：`kb.search` 加进来会动两样东西 —— 工具清单与提示词。前者由
    `test_enable_kb_switch_controls_registration_and_tool_list` 钉住，这里钉后者：
    `KB_RULE` 是**条件段**（`runtime/prompts.build_system_prompt` 里 `if "kb" in toolsets`），
    关着时那段不进提示词，所以

    - 关态的提示词与**产品默认那一臂**（`research, files`）**逐字节相同**（`system_prompt_hash`
      与 `tests/runtime/test_prompt_arms.py` 的 `GOLDEN_PRODUCT_ON_DEMAND_HASH` 同一个值）——
      这就是"默认关不掉哈希"的依据；
    - 开态的哈希必须**不同**，否则说明 `KB_RULE` 根本没生效（开关只改了工具清单、没改行为说明）。

    两边的 `enabled_toolsets` 都必须显式带 `enable_files=True`：2026-10-25 起 `files` 是产品默认，
    而 `enabled_toolsets` 的参数默认值仍是 `False`（它不读配置）。不传的话比的是"两个都不带
    files"的另一条臂 —— 那样这条用例测的就不是 `kb` 这一个变量了。
    """
    registry.clear_registry()
    off_names = register_tools(enable_kb=False)
    registry.clear_registry()
    on_names = register_tools(enable_kb=True)

    assert len(off_names) == 13, (
        "默认注册集 13 个（旧值 11：2026-10-25 起 file.* 默认开；披露仍默认关，所以不含 tool.*）"
    )
    assert "kb.search" not in off_names
    assert len(on_names) == len(off_names) + 1
    assert "kb.search" in on_names

    off_prompt = build_system_prompt(
        toolsets=list(registry.enabled_toolsets([], enable_kb=False, enable_files=True))
    )
    on_prompt = build_system_prompt(
        toolsets=list(registry.enabled_toolsets([], enable_kb=True, enable_files=True))
    )
    # 关态：没有那段"什么时候查本地知识库"，且与产品默认臂逐字节相同
    assert "kb.search" not in off_prompt
    assert off_prompt == build_system_prompt(toolsets=["research", "files"])
    # 开态：多了 KB_RULE，哈希必须变
    assert "什么时候查本地知识库" in on_prompt
    assert "kb.search" in on_prompt
    assert system_prompt_hash(on_prompt) != system_prompt_hash(off_prompt)


def test_incremental_rebuild_only_rewrites_the_changed_doc(
    kb: Path, ctx: AppContext, tmp_path: Path
) -> None:
    index = _index(kb, ctx.db)
    try:
        first = index.rebuild(force=True)
        assert (first.added, first.updated, first.unchanged) == (3, 0, 0)

        target = kb / "data" / "text" / "doc_rl.txt"
        target.write_text("强化学习被改写成了全新的说法：多臂老虎机。", encoding="utf-8")
        # 显式把 mtime 改到未来：不依赖文件系统的秒级粒度（同秒写入会看起来"没变"）
        os.utime(target, (first.docs + 1_000_000, first.docs + 1_000_000))

        second = index.rebuild()
        assert (second.added, second.updated, second.removed) == (0, 1, 0)
        assert second.unchanged == 2

        # 旧词搜不到、新词只命中这一条 —— 增量是"重写那一条"，不是"整表没动"
        assert index.search("多臂老虎机").total == 1
        assert index.search("样本效率").total == 0
    finally:
        index.close()


# ---------------------------------------------------------------- ⑨ 降级


def test_missing_fts5_falls_back_to_like_and_warns(
    kb: Path, ctx: AppContext, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """FTS5 不可用 → LIKE 降级：**仍然能搜到**，回包标明 `like`，并且出一条 WARNING。

    降级是"能跑但更慢/更糙"，不是"静默变成空结果"—— 后者会让模型以为库里没资料。
    """
    monkeypatch.setattr(kb_index, "probe_fts5", lambda db: False)
    tc = tool_ctx(ctx)
    with caplog.at_level("WARNING", logger="scholarscout.state.kb_index"):
        envelope = kb_search(tc, query="深度学习")
    assert envelope["ok"] is True
    assert envelope["data"]["index_mode"] == "like"
    assert envelope["data"]["total"] == 1
    assert "«深度学习»" in envelope["data"]["items"][0]["snippet"]
    assert any("FTS5" in record.message for record in caplog.records)

    # 降级时没有 bm25：score 如实回 0.0（不编一个像相关度的数字）
    assert envelope["data"]["items"][0]["score"] == 0.0


# ---------------------------------------------------------------- ⑩ 正文不入库


def test_body_never_enters_library_db(kb: Path, ctx: AppContext) -> None:
    """**硬约束**：全文只在 `data/text/` 与索引缓存里，资料库（含 WAL）逐字节找不到它。"""
    tc = tool_ctx(ctx)
    assert kb_search(tc, query="深度学习")["ok"] is True

    needle = SECRET_LINE.encode("utf-8")
    library_files = [ctx.db.path, ctx.db.path.with_name(ctx.db.path.name + "-wal")]
    index_files = [
        kb / "data" / "cache" / "kb-fts.sqlite3",
        kb / "data" / "cache" / "kb-fts.sqlite3-wal",
    ]
    index_files = [path for path in index_files if path.is_file()]
    assert index_files, "索引文件没建出来"

    # 正文确实进了索引（否则下一条断言会因为"根本没索引"而假通过）
    assert any(needle in path.read_bytes() for path in index_files)
    # 资料库本体（含 WAL）里没有正文
    for path in library_files:
        if path.is_file():
            assert needle not in path.read_bytes(), f"{path.name} 里出现了正文"
    # 表结构层面也没有正文列：documents 只记 text_path
    columns = {str(row["name"]) for row in ctx.db.query("PRAGMA table_info(documents)")}
    assert "text_path" in columns
    assert not columns & {"body", "content", "text"}


# ---------------------------------------------------------------- ⑪ 回包形状


def test_envelope_shape_and_source_types_filter(kb: Path, ctx: AppContext) -> None:
    tc = tool_ctx(ctx)
    envelope = kb_search(tc, query="学习", limit=5)
    assert envelope["ok"] is True
    data = envelope["data"]
    assert set(data) >= {"query", "total", "items", "index_mode", "indexed_docs"}
    assert set(data["items"][0]) == {"source_id", "title", "path", "snippet", "score"}
    # 进入回答的 id 必须回填 refs（docs/01 §5.0）
    assert envelope["refs"]["source_ids"]
    assert envelope["refs"]["doc_ids"]
    assert len(envelope["refs"]["source_ids"]) == len(set(envelope["refs"]["source_ids"]))

    only_github = kb_search(tc, query="学习", source_types=["github"])
    assert only_github["ok"] is True
    assert [item["source_id"] for item in only_github["data"]["items"]] == ["src_doc_rl"]


def test_empty_result_is_ok_with_a_hint(kb: Path, ctx: AppContext) -> None:
    """查不到 ≠ 失败：`ok:true` + `total:0`，并给一句"去联网找"的提示（不装成有资料）。"""
    envelope = kb_search(tool_ctx(ctx), query="量子纠缠")
    assert envelope["ok"] is True
    assert envelope["data"]["total"] == 0
    assert envelope["data"]["items"] == []
    assert "research.search" in envelope["data"]["hint"]
