"""测试公共夹具：每个用例一个临时库，建表走真实的迁移路径（不打桩）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from scholarscout.config import Config, Features, LlmProfile, RuntimeProfile
from scholarscout.providers.llm import ChatReply, ToolCall
from scholarscout.rpc.methods import AppContext
from scholarscout.runtime import registry
from scholarscout.state.db import Database
from scholarscout.state.migrate import apply_migrations

#: 固定时间，避免用例依赖当前时钟
T0 = "2026-10-05T10:00:00+08:00"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "state.db")
    apply_migrations(database)
    try:
        yield database
    finally:
        database.close()


def insert_source(
    db: Database, source_id: str = "src_test0001", *, url: str = "https://arxiv.org/abs/2501.00001"
) -> str:
    db.execute(
        """
        INSERT INTO sources (id, url, url_canonical, domain, title, source_type, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 'arxiv.org', 'A Test Paper', 'arxiv', ?, ?)
        """,
        (source_id, url, url, T0, T0),
    )
    return source_id


def insert_document(
    db: Database, doc_id: str = "doc_test0001", source_id: str = "src_test0001"
) -> str:
    db.execute(
        """
        INSERT INTO documents (id, source_id, title, sha256, chars, raw_path, text_path,
                               extract_status, fetched_at)
        VALUES (?, ?, 'A Test Paper', 'deadbeef', 1234, 'data/raw/de/deadbeef.pdf',
                ?, 'ok', ?)
        """,
        (doc_id, source_id, f"data/text/{doc_id}.txt", T0),
    )
    return doc_id


def insert_summary(
    db: Database,
    summary_id: str = "sum_test0001",
    *,
    doc_id: str = "doc_test0001",
    source_id: str = "src_test0001",
    title_zh: str = "教育智能体的检索增强方法",
    summary_short: str = "该工作提出一种教育智能体框架，在 MATH 数据集上把准确率从 42% 提到 58%。",
    points_json: str | None = None,
    prompt_version: str = "abc123def456",
) -> str:
    db.execute(
        """
        INSERT INTO summaries (id, doc_id, source_id, kind, title_zh, summary_short, points_json,
                               model, prompt_version, created_at)
        VALUES (?, ?, ?, 'paper', ?, ?, ?, 'test-model', ?, ?)
        """,
        (
            summary_id,
            doc_id,
            source_id,
            title_zh,
            summary_short,
            points_json if points_json is not None else '["在 MATH 上 42% -> 58%"]',
            prompt_version,
            T0,
        ),
    )
    return summary_id


def insert_session(db: Database, session_id: str = "ses_test0001") -> str:
    db.execute(
        "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, '测试会话', ?, ?)",
        (session_id, T0, T0),
    )
    return session_id


def insert_message(
    db: Database,
    message_id: str = "msg_test0001",
    *,
    session_id: str = "ses_test0001",
    idx: int = 0,
    content: str = "帮我找教育智能体方向的入门资料",
) -> str:
    db.execute(
        """
        INSERT INTO messages (id, session_id, idx, role, content, created_at)
        VALUES (?, ?, ?, 'user', ?, ?)
        """,
        (message_id, session_id, idx, content, T0),
    )
    return message_id


@pytest.fixture
def seeded(db: Database) -> Database:
    """一条完整链路：source -> document -> summary，外加一个会话与一条消息。"""
    insert_source(db)
    insert_document(db)
    insert_summary(db)
    insert_session(db)
    insert_message(db)
    return db


# --------------------------------------------------------------------- M2：模型桩与装配


class StubLlm:
    """按预设脚本返回的模型桩。记录每次调用的 messages/tools，供断言。"""

    def __init__(self, *replies: ChatReply) -> None:
        self.replies: list[ChatReply] = list(replies)
        self.calls: list[dict[str, object]] = []

    def chat(
        self,
        messages: list[dict[str, object]],
        *,
        tools: list[dict[str, object]] | None = None,
    ) -> ChatReply:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.replies:
            raise AssertionError("StubLlm 的脚本用完了，用例应显式给出足够的回复")
        return self.replies.pop(0)


def reply(content: str = "好的", *, tool_calls: list[ToolCall] | None = None) -> ChatReply:
    return ChatReply(
        content=content,
        tool_calls=list(tool_calls or []),
        finish_reason="tool_calls" if tool_calls else "stop",
        model="stub-model",
        usage={"prompt_tokens": 11, "completion_tokens": 7},
    )


def tool_call(name: str, arguments: str = "{}", *, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """测试配置：库在 tmp_path 下，模型档位用假值（不联网）。"""
    return Config(
        chat=LlmProfile(model="stub-model", base_url="http://localhost:9/v1", temperature=0.0),
        summary=LlmProfile(model="stub-model", base_url="http://localhost:9/v1", temperature=0.0),
        runtime=RuntimeProfile(max_tool_calls_per_turn=3, stream=False, allow_clarify=True),
        features=Features(enable_kb=False),
        data_dir=tmp_path / "data",
        db_path=tmp_path / "state.db",
        raw={},
    )


@pytest.fixture
def ctx(db: Database, config: Config) -> Iterator[AppContext]:
    """已装配的应用上下文，模型桩可后置替换（``ctx.llm = StubLlm(...)``）。"""
    context = AppContext(config=config, db=db, llm=StubLlm(), toolsets=("research",))
    try:
        yield context
    finally:
        registry.clear_registry()


def tool_ctx(ctx: AppContext):
    """把应用上下文塞进工具层要的 ToolContext（与真实调用同一形状）。"""
    from scholarscout.runtime.toolctx import ToolContext

    return ToolContext(db=ctx.db, config=ctx.config, http=getattr(ctx, "http", None), llm=ctx.llm)


def redirect_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把落盘根目录重定向到 tmp，避免用例往仓库的 data/ 里写文件。"""

    from scholarscout import paths

    monkeypatch.setenv("SCHOLARSCOUT_HOME", str(tmp_path))
    paths.ensure_dirs()
    return tmp_path
