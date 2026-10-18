"""`channel.send_file`：三类目标 / 找不到就不发 / 失败一律 `ok:false`（docs/17 §3.12）。

为什么不测在 `tests/channels`：渠道层只做**意图解析**（不读库、不出网，见 `test_layering`），
查库 → 生成导出件 → 上传 → 私聊发送全在这个 RPC 里。所以"真把文件发出去"的断言只能落在这里；
而"哪句话算要文件"的断言在 `tests/channels/test_feishu_file_request.py`。

出网仍然是**桩**（`httpx.MockTransport`）：零凭据、零真网络、零真飞书。
官方口径（2026-10-18 核实）：`POST /open-apis/im/v1/files`（multipart）+ 发消息
`receive_id_type=open_id`、`msg_type=file`；单文件 ≤ 30 MB 且不允许空文件。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from scholarscout import paths
from scholarscout.fetch.feishu_send import APP_ID_ENV, APP_SECRET_ENV
from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state.learning import save_route, save_topic_map
from scholarscout.state.sessions import create_session

from ..conftest import insert_document, insert_source, redirect_home

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
UPLOAD_URL = "https://open.feishu.cn/open-apis/im/v1/files"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
TOKEN_OK = {"code": 0, "msg": "ok", "tenant_access_token": "t-" + "a" * 40, "expire": 7200}
UPLOAD_OK = {"code": 0, "msg": "success", "data": {"file_key": "file_v3_01abc"}}
MESSAGE_OK = {"code": 0, "msg": "success", "data": {"message_id": "om_file_0001"}}

CHAT_ID = "oc_file_chat_0001"
OPEN_ID = "ou_file_user"

GRAPH = {
    "nodes": [{"nid": "n1", "label": "RAG", "kind": "method", "source_ids": ["src_test0001"]}],
    "edges": [],
}
PLAN = {
    "weeks": [
        {
            "idx": 1,
            "goal": "能跑通检索",
            "checkpoint": "能解释每一步",
            "items": [
                {
                    "kind": "doc",
                    "title": "官方指南",
                    "source_id": "src_test0001",
                    "why": "先跑起来",
                    "est_minutes": 60,
                }
            ],
        }
    ],
    "mode": "model",
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`data/` 与 `run/` 重定向到 tmp；凭据走环境变量（产品代码就是这么读的）。"""
    redirect_home(monkeypatch, tmp_path)
    monkeypatch.setenv(APP_ID_ENV, "cli_file")
    monkeypatch.setenv(APP_SECRET_ENV, "sec_file")


def _router(
    seen: list[httpx.Request],
    *,
    upload: httpx.Response | None = None,
    message: httpx.Response | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if "tenant_access_token" in url:
            return httpx.Response(200, json=TOKEN_OK)
        if url.endswith("/open-apis/im/v1/files"):
            return upload or httpx.Response(200, json=UPLOAD_OK)
        return message or httpx.Response(200, json=MESSAGE_OK)

    return handle


def _wire(
    ctx: AppContext,
    seen: list[httpx.Request],
    *,
    enabled: bool = True,
    upload: httpx.Response | None = None,
    message: httpx.Response | None = None,
) -> AppContext:
    """装上桩出网客户端，并把出站开关设成 `enabled`（默认开，好让"发出去了"能被断言）。"""
    ctx.http = HttpClient(
        respect_robots=False,
        transport=httpx.MockTransport(_router(seen, upload=upload, message=message)),
    )
    runtime = dataclasses.replace(ctx.config.runtime, feishu_reply_enabled=enabled)
    ctx.config = dataclasses.replace(ctx.config, runtime=runtime)
    return ctx


def _send(ctx: AppContext, **params: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"platform": "feishu", "open_id": OPEN_ID}
    payload.update(params)
    return dispatch(ctx, "channel.send_file", payload)


def _filed(rel: str, body: bytes = b"hello") -> Path:
    """在项目 `data/` 下造一个真文件（`paths.under_data` 保证它落在白名单里）。"""
    target = paths.under_data(Path(rel))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


# ================================================================== ① 本会话生成物


def test_artifact_of_this_session_is_exported_then_sent(ctx: AppContext) -> None:
    """生成物 → 导出成 `data/exports/*.md` → 上传 → 私聊发送（三步请求，顺序固定）。"""
    seen: list[httpx.Request] = []
    session_id = create_session(ctx.db, session_key=CHAT_ID)
    save_topic_map(ctx.db, topic="注意力机制", graph=GRAPH, session_id=session_id)
    _wire(ctx, seen)

    out = _send(ctx, kind="artifact", query="topic", session_key=CHAT_ID)

    assert out["ok"] is True
    assert out["kind"] == "artifact"
    assert out["file_name"].startswith("topic_") and out["file_name"].endswith(".md")
    assert out["message_id"] == "om_file_0001"
    assert "已发送" in out["reply"] and out["file_name"] in out["reply"] and "KB" in out["reply"]
    # 导出件真的落在 data/exports/ 里（不是内存里临时糊一份）
    exported = paths.under_data(Path("exports") / out["file_name"])
    text = exported.read_text(encoding="utf-8")
    assert "# 知识地图：注意力机制" in text
    assert "出处：" in text and "src_test0001" in text
    # 换 token → 上传 → 发送
    assert [str(r.url) for r in seen] == [
        TOKEN_URL,
        UPLOAD_URL,
        MESSAGE_URL + "?receive_id_type=open_id",
    ]
    assert out["file_name"].encode() in seen[1].content, "上传的必须是这份导出件"


def test_route_artifact_is_exported_by_route_export(ctx: AppContext) -> None:
    """路线走 `route.export`（同一个渲染），文件名 `route_*.md`，正文有周标题。"""
    seen: list[httpx.Request] = []
    session_id = create_session(ctx.db, session_key=CHAT_ID)
    save_route(ctx.db, topic="教育智能体", plan=PLAN, level="入门", weeks=1, session_id=session_id)
    _wire(ctx, seen)

    out = _send(ctx, kind="artifact", query="route", session_key=CHAT_ID)

    assert out["ok"] is True and out["file_name"].startswith("route_")
    text = paths.under_data(Path("exports") / out["file_name"]).read_text(encoding="utf-8")
    assert "# 学习路线：教育智能体" in text and "第 1 周" in text


def test_artifact_prefers_this_session_over_the_newest_anywhere(ctx: AppContext) -> None:
    """本会话有一份、别的会话更新的那份：**先给本会话的**（用户问的是"我刚要的那个"）。"""
    seen: list[httpx.Request] = []
    mine = create_session(ctx.db, session_key=CHAT_ID)
    other = create_session(ctx.db, session_key="oc_someone_else")
    save_topic_map(ctx.db, topic="本会话的图", graph=GRAPH, session_id=mine)
    save_topic_map(ctx.db, topic="别人的图", graph=GRAPH, session_id=other)
    _wire(ctx, seen)

    out = _send(ctx, kind="artifact", query="topic", session_key=CHAT_ID)

    assert out["ok"] is True
    assert "本会话的图" in paths.under_data(Path("exports") / out["file_name"]).read_text(
        encoding="utf-8"
    ), "本会话有产物时不许把别人的最新那份发出去"


# ================================================================== ② 已入库资料（标题模糊匹配）


def test_library_matches_a_document_title_and_sends_the_original(ctx: AppContext) -> None:
    """按标题模糊匹配 `documents`，发的是**原件**（字节原样，不是摘要文本）。"""
    seen: list[httpx.Request] = []
    insert_source(ctx.db)
    insert_document(ctx.db)
    raw = _filed("raw/de/deadbeef.pdf", b"%PDF-1.4 original body")
    ctx.db.execute("UPDATE documents SET title = '注意力机制综述' WHERE id = 'doc_test0001'")
    _wire(ctx, seen)

    out = _send(ctx, kind="library", query="注意力")

    assert out["ok"] is True and out["kind"] == "library"
    assert out["file_name"] == raw.name
    assert b"%PDF-1.4 original body" in seen[1].content


def test_library_title_match_also_hits_the_source_title(ctx: AppContext) -> None:
    """`documents.title` 为空时退回 `sources.title` 参与匹配（库里的老记录就是这样）。"""
    seen: list[httpx.Request] = []
    insert_source(ctx.db)
    insert_document(ctx.db)
    _filed("raw/de/deadbeef.pdf", b"%PDF-1.4 x")
    ctx.db.execute("UPDATE documents SET title = NULL WHERE id = 'doc_test0001'")
    ctx.db.execute("UPDATE sources SET title = 'A Test Paper' WHERE id = 'src_test0001'")
    _wire(ctx, seen)

    out = _send(ctx, kind="library", query="test paper")

    assert out["ok"] is True and out["file_name"] == "deadbeef.pdf"


# ================================================================== ③ 项目内路径


def test_path_kind_allows_only_project_files(ctx: AppContext, tmp_path: Path) -> None:
    """相对路径与**项目内**绝对路径都行；项目外的一律拒绝并说清原因。"""
    seen: list[httpx.Request] = []
    _wire(ctx, seen)
    _filed("exports/手工便签.md", "随手记一行".encode())
    inside = paths.under_data(Path("exports/手工便签.md"))

    rel = _send(ctx, kind="path", query="data/exports/手工便签.md")
    absolute = _send(ctx, kind="path", query=str(inside))
    assert rel["ok"] is True and rel["file_name"] == "手工便签.md"
    assert absolute["ok"] is True, "桌面壳点「发这个文件」给的是绝对路径"

    before = len(seen)
    outside = tmp_path / "secret.txt"
    outside.write_text("不该被发出去", encoding="utf-8")
    refused = _send(ctx, kind="path", query=str(outside))
    assert refused["ok"] is False and "只允许发项目目录" in refused["reply"]
    missing = _send(ctx, kind="path", query="data/exports/没有这个.md")
    assert missing["ok"] is False and "没有文件" in missing["reply"]
    assert len(seen) == before, "被拒的路径一次外网都不许发"


# ================================================================== ④ 找不到就不发


def test_missing_library_target_lists_candidates_and_sends_nothing(ctx: AppContext) -> None:
    """**找不到就不发**：回可读的话 + 候选清单（最多 6 条），零请求。"""
    seen: list[httpx.Request] = []
    insert_source(ctx.db)
    insert_document(ctx.db)
    _wire(ctx, seen)

    out = _send(ctx, kind="library", query="量子计算")

    assert out["ok"] is False and out["kind"] == "library"
    assert out["candidates"] == ["A Test Paper"], "候选清单要能直接回给用户挑"
    assert "没找到" in out["reply"] and "现在能发这些" in out["reply"]
    assert seen == []


def test_artifact_when_the_library_is_empty_says_so(ctx: AppContext) -> None:
    seen: list[httpx.Request] = []
    _wire(ctx, seen)

    out = _send(ctx, kind="artifact")

    assert out["ok"] is False and "还没有可发的生成物" in out["reply"]
    assert seen == []


def test_library_row_without_a_file_on_disk_is_reported(ctx: AppContext) -> None:
    """库里记着、原件被移走：如实说"盘上找不到"，不猜别的文件发出去。"""
    seen: list[httpx.Request] = []
    insert_source(ctx.db)
    insert_document(ctx.db)  # 故意不造 data/raw/de/deadbeef.pdf
    _wire(ctx, seen)

    out = _send(ctx, kind="library", query="test")

    assert out["ok"] is False and "原件在盘上找不到" in out["reply"]
    assert seen == []


# ================================================================== ⑤ 入参校验


def test_other_platforms_and_missing_open_id_are_refused(ctx: AppContext) -> None:
    seen: list[httpx.Request] = []
    _wire(ctx, seen)

    other = dispatch(
        ctx, "channel.send_file", {"platform": "wechat", "open_id": OPEN_ID, "kind": "artifact"}
    )
    assert other["ok"] is False and "只支持飞书" in other["reply"]

    anonymous = dispatch(
        ctx, "channel.send_file", {"platform": "feishu", "open_id": "  ", "kind": "artifact"}
    )
    assert anonymous["ok"] is False and "open_id" in anonymous["reply"]
    assert seen == [], "参数不合法时不许出网"


def test_unknown_kind_falls_back_to_artifact_and_says_so(ctx: AppContext) -> None:
    """kind 拼错按 `artifact` 处理（能力还在），并在回包 `kind` 里如实反映实际用了哪一类。"""
    seen: list[httpx.Request] = []
    save_topic_map(ctx.db, topic="兜底图", graph=GRAPH)
    _wire(ctx, seen)

    out = _send(ctx, kind="乱写的类")

    assert out["ok"] is True and out["kind"] == "artifact"


# ================================================================== ⑥ 失败一律 ok:false


def test_upload_permission_error_becomes_ok_false_with_a_readable_reply(ctx: AppContext) -> None:
    """权限没勾（99991672）：**不抛**，回可读中文 + 告诉用户去勾哪个 scope。"""
    seen: list[httpx.Request] = []
    denied = httpx.Response(
        200,
        json={"code": 99991672, "msg": "Access denied. One of the following scopes is required"},
    )
    save_topic_map(ctx.db, topic="注意力机制", graph=GRAPH)
    _wire(ctx, seen, upload=denied)

    out = _send(ctx, kind="artifact", query="topic")

    assert out["ok"] is False and out["kind"] == "artifact"
    assert "没发出去" in out["reply"] and "im:resource" in out["reply"]
    assert len(seen) == 2, "上传失败就不该再发一条消息"


def test_switch_off_means_zero_network(ctx: AppContext) -> None:
    """`runtime.feishu_reply_enabled=false`（产品默认）：一次外网都不发。"""
    seen: list[httpx.Request] = []
    save_topic_map(ctx.db, topic="注意力机制", graph=GRAPH)
    _wire(ctx, seen, enabled=False)

    out = _send(ctx, kind="artifact", query="topic")

    assert out["ok"] is False and "未开启出站发送" in out["reply"]
    assert seen == []


def test_document_row_with_an_escaping_path_is_skipped(ctx: AppContext) -> None:
    """库里被篡改成越界路径（`../`）：跳过那一条，不能拿它去打 `resolve_rel` 的洞。"""
    seen: list[httpx.Request] = []
    insert_source(ctx.db)
    insert_document(ctx.db)
    ctx.db.execute("UPDATE documents SET raw_path = '../../outside.pdf' WHERE id = 'doc_test0001'")
    _wire(ctx, seen)

    out = _send(ctx, kind="library", query="test")

    assert out["ok"] is False and "原件在盘上找不到" in out["reply"]
    assert seen == []


def test_exported_file_name_is_slugified(ctx: AppContext) -> None:
    """标题里的 Windows 非法字符不能直接进文件名（`paths.slugify` 的既有口径）。"""
    seen: list[httpx.Request] = []
    save_topic_map(ctx.db, topic='注意力/机制: "综述"', graph=GRAPH)
    _wire(ctx, seen)

    out = _send(ctx, kind="artifact", query="topic")

    assert out["ok"] is True
    name = out["file_name"]
    assert not set(name) & set('\\/:*?"<>|'), f"文件名里出现了非法字符：{name}"
    assert name.startswith("topic_")


def test_success_reply_carries_only_the_file_facts(ctx: AppContext) -> None:
    """回包只带**这一份文件**的事实：没有 `candidates`、也没有本机路径（渠道层原样回一句话）。"""
    seen: list[httpx.Request] = []
    save_topic_map(ctx.db, topic="注意力机制", graph=GRAPH)
    _wire(ctx, seen)

    out = _send(ctx, kind="artifact", query="topic")

    assert set(out) == {"ok", "kind", "file_name", "message_id", "reply"}
    assert "data/" not in out["reply"], "回给用户的是文件名，不是本机路径"


def test_documents_are_ordered_by_fetched_at_not_by_insert_order(ctx: AppContext) -> None:
    """按 `fetched_at` 倒序：后插入的**老**资料不能顶掉新的那条（库里的历史行就是这么排的）。"""
    seen: list[httpx.Request] = []
    insert_source(ctx.db)
    insert_document(ctx.db)  # 'A Test Paper' @ T0
    _filed("raw/de/deadbeef.pdf", b"%PDF-1.4 new")
    ctx.db.execute(
        """
        INSERT INTO documents (id, source_id, title, sha256, chars, raw_path, text_path,
                               extract_status, fetched_at)
        VALUES ('doc_old01', 'src_test0001', 'A Test Paper（旧版）', 'cafe', 10,
                'data/raw/ca/cafe.pdf', NULL, 'ok', '2026-01-01T00:00:00+08:00')
        """
    )
    _filed("raw/ca/cafe.pdf", b"%PDF-1.4 old")
    _wire(ctx, seen)

    out = _send(ctx, kind="library", query="test paper")

    assert out["ok"] is True
    assert out["file_name"] == "deadbeef.pdf", "fetched_at 更新的那条要先发"
