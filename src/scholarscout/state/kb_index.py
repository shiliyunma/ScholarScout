"""知识库检索索引：SQLite FTS5 + 中文逐字 shadow 列（`kb.search` 的底座，docs/01 §5.8）。

**正文不入库**（项目硬约束）：`documents` 表只有 `text_path` 指向 `data/text/` 下的全文，
正文一个字节都不进资料库表。本模块建的是**另一个 SQLite 文件**
（`paths.kb_index_path()` → `data/cache/kb-fts.sqlite3`）：它是**可丢弃的索引缓存** ——
删掉照样能跑（下次检索按 `data/text/` 重建），也因此「资料库本体里没有正文」可以逐字节验证
（`tests/tools/test_kb_search.py::test_body_never_enters_library_db`）。

索引文件里三张表：

- `kb_docs`：每条资料的元数据 + **增量判据**（`mtime` / `size` / `sha`）。检索结果的
  `source_id` / `title` / `path` 只从这里取，**不存正文**；
- `kb_fts`（FTS5）：`title` / `meta` / `body` / `body_cjk` / `meta_cjk` 五列。`meta` 是库内的
  摘要与标签，`body` 是 `data/text/` 原文；`body_cjk` / `meta_cjk` 是**逐字分隔**的 shadow 列
  （`深度学习` → `深 度 学 习`）。为什么必须多这两列：FTS5 默认分词器 `unicode61` 把一整串
  汉字当成**一个** token，于是「深度学习」这份资料查「深度」、只在摘要里出现的「遗忘曲线」
  查「遗忘曲线」都永远不中 —— 逐字列 + 短语查询才治得住（查询侧见 :func:`build_match`，
  查询是 CJK 短语时**每一列都得有 shadow**，否则命中只可能来自正文）；
- `kb_plain`（**只有 FTS5 不可用时才建**）：降级通道，`LIKE` 全表扫。

FTS5 是 SQLite 的**编译期**选项，不是每个发行版都带（Windows 官方包带，某些系统自带的
`libsqlite3` 不带）。不可用时**不许静默**：:func:`probe_fts5` 检测一次，降级走 `kb_plain`，
并打一条 `WARNING` 说明「本次检索走 LIKE、没有 bm25 排序、中文按子串命中」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scholarscout.state.db import Database

log = logging.getLogger(__name__)

#: 索引结构版本：**改了 `kb_fts` 的列或切分口径就加一**，旧索引因此整体重建。
#: v2：加 `meta_cjk` 逐字影子列 —— 摘要/标签里的中文原本一个都查不到（`meta` 列被
#: `unicode61` 当成一个整串 token），只有正文能命中，与"库内元数据也在索引里"不符。
INDEX_VERSION = "2"

#: `snippet` 的可见长度上限（不含高亮标记与省略号）
SNIPPET_CHARS = 300

#: 高亮标记（FTS5 `snippet()` 的 start/end）
MARK_OPEN = "«"
MARK_CLOSE = "»"

#: 一次检索最多回多少条（`limit` 的上限，超出按上限夹住并如实回到 `limit`）
MAX_LIMIT = 50

#: 索引"新鲜度"窗口：超过这么多秒的索引在下次检索时**增量**扫一遍（只 stat，不重读全文）
REFRESH_SECONDS = 300.0

#: 单篇正文进索引的字符上限（超长 PDF 全文可达数十万字，索引体积与 snippet 都用不上那么多）
MAX_BODY_CHARS = 200_000

#: FTS5 的匹配表达式里允许原样当前缀查的 token（纯 ASCII 字母数字）
_ALNUM = re.compile(r"^[0-9A-Za-z_]+$")

#: 汉字区段（基本区 / 扩展 A / 兼容区）—— n-gram 切分只认这三段
_CJK_CLASS = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"

#: snippet 里被逐字分隔的汉字之间的那个空格（还原成正常中文排版用）
_CJK_GAP = re.compile(rf"(?<=[{_CJK_CLASS}])[ ]+(?=[{_CJK_CLASS}])")


def _is_cjk(ch: str) -> bool:
    return "\u3400" <= ch <= "\u4dbf" or "\u4e00" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"


def cjk_shadow(text: str) -> str:
    """逐字分隔（**只拆汉字**，英文单词保持完整）：`深度学习` → `深 度 学 习`。

    为什么英文不拆：`unicode61` 本来就按空格/标点切英文，拆成单字母会让
    `transformers` 命中 `t r a n s …` 这种噪声，而前缀查询（`transf*`）已经覆盖了英文的
    部分匹配需求。汉字反过来 —— 它没有空格可切，整串是一个 token，所以必须人为拆开。
    """
    out: list[str] = []
    for ch in text:
        if _is_cjk(ch):
            out.append(f" {ch} ")
        else:
            out.append(ch)
    return re.sub(r"[ \t\r\f\v]+", " ", "".join(out)).strip()


def build_match(query: str) -> str:
    """把用户输入翻成 FTS5 的 `MATCH` 表达式（**多词之间是 AND**）。

    - 含汉字：走**逐字短语**（`"深 度 学 习"`），于是「深度学习」这份资料能被「深度」命中，
      顺序也能保住（短语要求相邻）；
    - 纯 ASCII 字母数字：走**前缀**（`transf*`），大小写不敏感由 `unicode61` 负责；
    - 其余（带标点、连字符等）：退成短语，避免把 `-` / `:` 当成 FTS5 语法。

    空串返回 `""`（调用方在进来之前就该拒掉空 query）。
    """
    parts: list[str] = []
    for token in query.split():
        if any(_is_cjk(ch) for ch in token):
            parts.append(_quoted(cjk_shadow(token)))
        elif _ALNUM.match(token):
            parts.append(f"{token}*")
        else:
            parts.append(_quoted(token))
    return " AND ".join(parts)


def _quoted(text: str) -> str:
    """FTS5 短语：内部双引号翻倍（`""` 是 FTS5 的转义写法，不是 SQL 的）。"""
    return f'"{text.replace(chr(34), chr(34) * 2)}"'


def collapse_cjk(text: str) -> str:
    """把逐字列里的空格收回去：`深 度 学 习` → `深度学习`（切分是给索引看的，不是给人看的）。"""
    return _CJK_GAP.sub("", text)


def visible_len(text: str) -> int:
    """可见字数：**不数**高亮标记（截断与测试都按这个口径，否则标记会吃掉字数）。"""
    return len(text.replace(MARK_OPEN, "").replace(MARK_CLOSE, ""))


def truncate_marked(text: str, limit: int = SNIPPET_CHARS) -> str:
    """截到 ``limit`` 个可见字符，**不把 « » 撕成半个**（撕了前端就没法渲染高亮）。

    超长时补一个 `…`（它不计入 ``limit``，`visible_len` 会带上它，所以最多 ``limit`` 个非标记字符）。
    """
    if visible_len(text) <= limit:
        return text
    kept: list[str] = []
    visible = 0
    for ch in text:
        if ch in (MARK_OPEN, MARK_CLOSE):
            kept.append(ch)
            continue
        if visible >= limit:
            break
        kept.append(ch)
        visible += 1
    marked = "".join(kept)
    if marked.count(MARK_OPEN) > marked.count(MARK_CLOSE):
        marked += MARK_CLOSE
    return marked + "…"


def probe_fts5(db: Database) -> bool:
    """这次运行的 SQLite 有没有编译 FTS5（**不抛**：没有就走降级通道）。

    探测用 `temp` 库：不留任何痕（虚拟表建在临时库里，进程结束即消失）。
    """
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.kb_fts_probe USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    return True


@dataclass(frozen=True)
class SearchItem:
    """一条命中：**没有正文**，只有定位信息与一小段带高亮的片段。"""

    doc_id: str
    source_id: str
    title: str
    path: str
    snippet: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "path": self.path,
            "snippet": self.snippet,
            "score": self.score,
        }


@dataclass(frozen=True)
class SearchOutcome:
    """一次检索的结果：``total`` 是**命中总数**（不是本页条数）。"""

    query: str
    total: int
    items: list[SearchItem]
    mode: str
    docs: int


@dataclass(frozen=True)
class RebuildStats:
    """一次重建的账：增量时 ``updated`` 只该等于「真变了的文件数」。"""

    mode: str
    added: int
    updated: int
    removed: int
    unchanged: int
    docs: int


#: 库内元数据（**只读 SELECT，不写回**）：标题 / 摘要 / 标签 / 正文路径。
#: 摘要取该资料**最新一版**（`summaries` 按 `created_at` 倒序）；标签在 `sources.meta_json`。
_ROWS_SQL = """
    SELECT d.id AS doc_id,
           d.source_id AS source_id,
           s.source_type AS source_type,
           coalesce(d.title, s.title, '') AS title,
           coalesce(d.abstract, '') AS abstract,
           d.text_path AS text_path,
           d.chars AS chars,
           coalesce(sm.title_zh, '') AS title_zh,
           coalesce(sm.summary_short, '') AS summary_short,
           s.meta_json AS meta_json
    FROM documents d
    JOIN sources s ON s.id = d.source_id
    LEFT JOIN summaries sm ON sm.id = (
        SELECT id FROM summaries WHERE doc_id = d.id ORDER BY created_at DESC, rowid DESC LIMIT 1
    )
    WHERE d.ok = 1
"""

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kb_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_docs (
  doc_id      TEXT PRIMARY KEY,
  source_id   TEXT NOT NULL,
  source_type TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  path        TEXT NOT NULL DEFAULT '',
  mtime       REAL NOT NULL DEFAULT 0,
  size        INTEGER NOT NULL DEFAULT 0,
  sha         TEXT NOT NULL DEFAULT '',
  chars       INTEGER NOT NULL DEFAULT 0,
  indexed_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_docs_source ON kb_docs(source_id);
CREATE INDEX IF NOT EXISTS idx_kb_docs_type   ON kb_docs(source_type);
"""

#: `kb_fts` 的列（与 `_FTS_SQL` 同源；旧索引靠它判定是否过期，见 `_drop_stale_fts`）
_FTS_COLUMNS = frozenset({"doc_id", "title", "meta", "body", "body_cjk", "meta_cjk"})

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
  doc_id UNINDEXED,
  title,
  meta,
  body,
  body_cjk,
  meta_cjk,
  tokenize = 'unicode61 remove_diacritics 2'
)
"""

_PLAIN_SQL = """
CREATE TABLE IF NOT EXISTS kb_plain (
  doc_id TEXT PRIMARY KEY,
  title  TEXT NOT NULL DEFAULT '',
  body   TEXT NOT NULL DEFAULT ''
)
"""


def _meta_tags(raw: Any) -> str:
    """`sources.meta_json` 里的标签/关键词（可能是 list、逗号串或缺席）。"""
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    words: list[str] = []
    for key in ("tags", "keywords"):
        value = parsed.get(key)
        if isinstance(value, list):
            words.extend(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, str) and value.strip():
            words.append(value.strip())
    return " ".join(words)


class KbIndex:
    """索引缓存的唯一入口：`rebuild()` 建/增量，`search()` 检索，`ensure()` 懒建。

    用法（工具层）：``KbIndex.for_config(ctx.db, ctx.config).search(query, limit=...)``。
    """

    def __init__(self, db: Database, *, root: Path, index_path: Path) -> None:
        self.db = db
        self.root = root
        self.index_path = index_path
        self._idx = Database(index_path)
        self._mode: str | None = None

    @classmethod
    def for_config(cls, db: Database, config: Any) -> KbIndex:
        """按当前配置定位索引：根目录取 `data_dir` 的上一级（`text_path` 是相对它记的）。"""
        from scholarscout import paths

        root = Path(config.data_dir).parent
        return cls(db, root=root, index_path=paths.kb_index_path(root))

    # ---------------------------------------------------------------- 模式

    @property
    def mode(self) -> str:
        """`"fts5"` 或 `"like"`（降级）。首次访问时探测一次，之后固定。"""
        if self._mode is None:
            self._mode = "fts5" if probe_fts5(self._idx) else "like"
            if self._mode == "like":
                log.warning(
                    "SQLite 未编译 FTS5（sqlite_version=%s）：kb.search 降级为 LIKE 全表扫，"
                    "结果没有 bm25 排序、中文按子串命中 —— 建议换带 FTS5 的 SQLite 发行版",
                    sqlite3.sqlite_version,
                )
        return self._mode

    def close(self) -> None:
        self._idx.close()

    # ---------------------------------------------------------------- 建索引

    def ensure(self) -> None:
        """按需构建：没建过 / 版本或模式变了 → 全量；过期 → 增量。

        **不在启动时跑**：`kb.search` 第一次被调用才走到这里（docs/01 §5.8），
        所以关着开关或从不检索的会话一个字节都不写。
        """
        self._ensure_schema()
        if self._meta("version") != INDEX_VERSION or self._meta("mode") != self.mode:
            self.rebuild(force=True)
            return
        built_at = self._meta("built_at")
        if built_at is None or time.time() - float(built_at) > REFRESH_SECONDS:
            self.rebuild()

    def rebuild(self, *, force: bool = False) -> RebuildStats:
        """扫描库表 + `data/text/`，按 `mtime`/`size` 增量更新索引。

        增量判据：**文件 `mtime` 与 `size` 都没变就跳过**（连读都不读）；变了的读一遍、
        记 `sha256` 并重写该条。`force=True` 清空重来（版本升级、模式切换、索引被删）。
        """
        self._ensure_schema()
        rows = self.db.query(_ROWS_SQL)
        existing = {
            str(row["doc_id"]): (float(row["mtime"]), int(row["size"]))
            for row in self._idx.query("SELECT doc_id, mtime, size FROM kb_docs")
        }

        added = 0
        updated = 0
        removed = 0
        unchanged = 0
        live: set[str] = set()

        with self._idx.transaction() as conn:
            if force:
                self._purge(conn)
                existing = {}
            for row in rows:
                doc_id = str(row["doc_id"])
                text_path = str(row["text_path"] or "")
                path = self._resolve(text_path) if text_path else None
                if path is None or not path.is_file():
                    # 正文缺失（抽取失败/被清理）：不进索引，旧条目当删除处理 ——
                    # 检索必须只回「真的能打开的正文」，否则模型拿到路径却读不到内容。
                    continue
                stat = path.stat()
                live.add(doc_id)
                old = existing.get(doc_id)
                if old is not None and old == (stat.st_mtime, stat.st_size):
                    unchanged += 1
                    continue
                body = path.read_text(encoding="utf-8", errors="replace")[:MAX_BODY_CHARS]
                self._write_doc(conn, row, path, stat.st_mtime, stat.st_size, body)
                if old is None:
                    added += 1
                else:
                    updated += 1
            for doc_id in sorted(set(existing) - live):
                self._drop(conn, doc_id)
                removed += 1
            docs = self._count(conn)
            self._set_meta(conn, "version", INDEX_VERSION)
            self._set_meta(conn, "mode", self.mode)
            self._set_meta(conn, "built_at", str(time.time()))
            self._set_meta(conn, "docs", str(docs))

        stats = RebuildStats(
            mode=self.mode,
            added=added,
            updated=updated,
            removed=removed,
            unchanged=unchanged,
            docs=docs,
        )
        log.info(
            "kb 索引重建：mode=%s added=%d updated=%d removed=%d unchanged=%d docs=%d",
            stats.mode,
            stats.added,
            stats.updated,
            stats.removed,
            stats.unchanged,
            stats.docs,
        )
        return stats

    # ---------------------------------------------------------------- 检索

    def search(
        self, query: str, *, limit: int = 10, source_types: Sequence[str] | None = None
    ) -> SearchOutcome:
        """检索：`total` 是命中总数，`items` 按分数降序（降级模式下按收录时间降序）。"""
        text = " ".join(str(query).split())
        if not text:
            raise ValueError("query 不能为空：kb.search 不做「列全部资料」")
        capped = max(1, min(int(limit), MAX_LIMIT))
        types = tuple(str(t).strip() for t in (source_types or ()) if str(t).strip())
        self.ensure()
        if self.mode == "fts5":
            return self._search_fts(text, capped, types)
        return self._search_like(text, capped, types)

    def _search_fts(self, query: str, limit: int, types: tuple[str, ...]) -> SearchOutcome:
        match = build_match(query)
        where = ""
        params: list[Any] = [match]
        if types:
            where = f" AND d.source_type IN ({', '.join('?' * len(types))})"
            params.extend(types)
        base = f"FROM kb_fts JOIN kb_docs AS d USING (doc_id) WHERE kb_fts MATCH ?{where}"
        # `count(DISTINCT source_id)`：**一条资料一行**（同一来源可能有多份抽取文本，
        # 各自 sha 不同却同名同源，逐行回给模型只会浪费预算）。
        total = self._idx.scalar_int(f"SELECT count(DISTINCT d.source_id) {base}", params)
        rows = self._idx.query(
            "SELECT d.doc_id AS doc_id, d.source_id AS source_id, d.title AS title, "
            "d.path AS path, -bm25(kb_fts) AS score, "
            "snippet(kb_fts, -1, '«', '»', '…', 40) AS fragment "
            f"{base} ORDER BY score DESC, d.doc_id LIMIT ?",
            # 多取几倍再按来源去重：重复率通常 0–1 条，只有语料极碎时才接近上限
            [*params, limit * 3],
        )
        items: list[SearchItem] = []
        seen: set[str] = set()
        for row in rows:
            source_id = str(row["source_id"])
            if source_id in seen:
                continue
            seen.add(source_id)
            items.append(
                SearchItem(
                    doc_id=str(row["doc_id"]),
                    source_id=source_id,
                    title=str(row["title"]),
                    path=str(row["path"]),
                    snippet=truncate_marked(collapse_cjk(str(row["fragment"] or ""))),
                    score=round(float(row["score"]), 9),
                )
            )
            if len(items) >= limit:
                break
        return SearchOutcome(
            query=query, total=total, items=items, mode="fts5", docs=self._meta_int("docs")
        )

    def _search_like(self, query: str, limit: int, types: tuple[str, ...]) -> SearchOutcome:
        pattern = _like_pattern(query)
        where = ""
        params: list[Any] = [pattern, pattern]
        if types:
            where = f" AND d.source_type IN ({', '.join('?' * len(types))})"
            params.extend(types)
        base = (
            "FROM kb_plain AS p JOIN kb_docs AS d ON d.doc_id = p.doc_id "
            f"WHERE (p.title LIKE ? ESCAPE '\\' OR p.body LIKE ? ESCAPE '\\'){where}"
        )
        total = self._idx.scalar_int(f"SELECT count(DISTINCT d.source_id) {base}", params)
        rows = self._idx.query(
            "SELECT p.doc_id AS doc_id, d.source_id AS source_id, d.title AS title, "
            f"d.path AS path, p.body AS body {base} ORDER BY d.indexed_at DESC, p.doc_id LIMIT ?",
            [*params, limit * 3],
        )
        items: list[SearchItem] = []
        seen: set[str] = set()
        for row in rows:
            source_id = str(row["source_id"])
            if source_id in seen:
                continue
            seen.add(source_id)
            items.append(
                SearchItem(
                    doc_id=str(row["doc_id"]),
                    source_id=source_id,
                    title=str(row["title"]),
                    path=str(row["path"]),
                    snippet=_like_snippet(str(row["body"]), query),
                    # 降级模式没有 bm25：**如实回 0.0**，不编一个看起来像相关度的数字
                    score=0.0,
                )
            )
            if len(items) >= limit:
                break
        return SearchOutcome(
            query=query, total=total, items=items, mode="like", docs=self._meta_int("docs")
        )

    # ---------------------------------------------------------------- 内部

    def _ensure_schema(self) -> None:
        self._idx.executescript(_SCHEMA_SQL)
        if self.mode == "fts5":
            self._drop_stale_fts()
            self._idx.executescript(_FTS_SQL)
            return
        self._idx.executescript(_PLAIN_SQL)
        if self._has_table("kb_fts"):
            # 库是从别的机器拷来的（那边有 FTS5）：这台没有，留着它只会让后续语句报
            # "no such module: fts5" —— 直接丢，反正它是可重建的索引。
            try:
                self._idx.execute("DROP TABLE kb_fts")
            except sqlite3.OperationalError as exc:
                log.warning("清理旧 FTS5 索引失败（继续走 LIKE 降级）：%s", exc)

    def _drop_stale_fts(self) -> None:
        """旧版索引的列对不上就整张丢掉：`CREATE ... IF NOT EXISTS` 不会改已存在的列。

        不丢的后果不是"少一列"，而是 `INSERT` 列数对不上 → 每次检索都炸在写索引上。
        索引是可重建缓存（`INDEX_VERSION` 一变本来就该全量重建），所以这里直接 DROP。
        """
        if not self._has_table("kb_fts"):
            return
        columns = {str(row["name"]) for row in self._idx.query("PRAGMA table_info(kb_fts)")}
        if columns == _FTS_COLUMNS:
            return
        try:
            self._idx.execute("DROP TABLE kb_fts")
            log.info(
                "kb 索引列已过期（%s ≠ %s）：丢弃并重建", sorted(columns), sorted(_FTS_COLUMNS)
            )
        except sqlite3.OperationalError as exc:
            log.warning("丢弃旧 FTS5 索引失败（继续用现有表）：%s", exc)

    def _purge(self, conn: sqlite3.Connection) -> None:
        for table in ("kb_fts", "kb_plain", "kb_docs"):
            if self._has_table(table):
                conn.execute(f"DELETE FROM {table}")

    def _has_table(self, name: str) -> bool:
        row = self._idx.query_one(
            "SELECT 1 FROM sqlite_master WHERE name = ? AND type = 'table' LIMIT 1", (name,)
        )
        return row is not None

    def _count(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT count(*) FROM kb_docs").fetchone()
        return 0 if row is None else int(row[0])

    def _write_doc(
        self,
        conn: sqlite3.Connection,
        row: Any,
        path: Path,
        mtime: float,
        size: int,
        body: str,
    ) -> None:
        doc_id = str(row["doc_id"])
        title = str(row["title"] or "")
        meta = " ".join(
            part
            for part in (
                str(row["abstract"] or ""),
                str(row["title_zh"] or ""),
                str(row["summary_short"] or ""),
                _meta_tags(row["meta_json"]),
            )
            if part
        )
        self._drop(conn, doc_id)
        if self.mode == "fts5":
            conn.execute(
                "INSERT INTO kb_fts (doc_id, title, meta, body, body_cjk, meta_cjk) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, title, meta, body, cjk_shadow(body), cjk_shadow(f"{title} {meta}")),
            )
        else:
            conn.execute(
                "INSERT INTO kb_plain (doc_id, title, body) VALUES (?, ?, ?)",
                (doc_id, f"{title} {meta}".strip(), body),
            )
        conn.execute(
            "INSERT INTO kb_docs (doc_id, source_id, source_type, title, path, mtime, size, "
            "sha, chars, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                str(row["source_id"]),
                str(row["source_type"] or ""),
                title,
                str(path),
                mtime,
                size,
                hashlib.sha256(body.encode("utf-8")).hexdigest(),
                len(body),
                time.time(),
            ),
        )

    def _drop(self, conn: sqlite3.Connection, doc_id: str) -> None:
        for table in ("kb_fts", "kb_plain"):
            if self._has_table(table):
                try:
                    conn.execute(f"DELETE FROM {table} WHERE doc_id = ?", (doc_id,))
                except sqlite3.OperationalError as exc:
                    # 旧 FTS5 表在无 FTS5 的机器上无法 DELETE —— 它马上会被 DROP，不是错误路径
                    log.debug("跳过清理 %s（%s）：%s", table, doc_id, exc)
        conn.execute("DELETE FROM kb_docs WHERE doc_id = ?", (doc_id,))

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO kb_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _meta(self, key: str) -> str | None:
        if not self._has_table("kb_meta"):
            return None
        return self._idx.scalar_str("SELECT value FROM kb_meta WHERE key = ?", (key,))

    def _meta_int(self, key: str) -> int:
        raw = self._meta(key)
        try:
            return int(raw or 0)
        except ValueError:
            return 0

    def _resolve(self, text_path: str) -> Path:
        path = Path(text_path)
        return path if path.is_absolute() else self.root / path


def _like_pattern(text: str) -> str:
    """LIKE 模式里把 `%`/`_` 转义掉（用户查 `100%` 不该变成通配符）。"""
    escaped = text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def _like_snippet(body: str, needle: str, limit: int = SNIPPET_CHARS) -> str:
    """降级模式的「手工 snippet」：命中处前后各取一段，把命中词包上 « »。"""
    index = body.lower().find(needle.lower())
    if index < 0:
        return truncate_marked(body[:limit].strip())
    start = max(0, index - 60)
    fragment = body[start : start + limit * 2]
    highlighted = re.sub(
        re.escape(needle),
        lambda m: f"{MARK_OPEN}{m.group(0)}{MARK_CLOSE}",
        fragment,
        count=1,
        flags=re.IGNORECASE,
    )
    return truncate_marked(highlighted.strip())
