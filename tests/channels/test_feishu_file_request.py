"""飞书**「发我文件」意图**（docs/17 §3.12）：规则识别 + 交给 `channel.send_file`。

为什么另开一个文件：`test_feishu_ack.py` 钉的是「跑一轮」那条链路（回执 → 落库 → 回答），
而发文件**根本不跑模型**。两条路的顺序约定也不同（文件请求连回执都不发：它的回复本身就是回执），
混在一起会让两边的断言互相污染。

本文件钉住九件事：

1. **三类目标**各有识别：本会话生成物（地图/路线/笔记）／按标题模糊匹配的已入库资料／项目内路径；
2. **提问不是指令**：「给我讲讲学习路线」「为什么要做知识地图」绝不劫走问答；整段话里夹一句"发我"也不认；
3. **两种语序**都认：`发我学习路线` 与 `把学习路线发我`；
4. 识别出来只调 **`channel.send_file`**（不调 `chat.send`、不调模型、不发回执）；
5. **未授权者要不到文件**（排在白名单放行之后）；
6. **找不到就不发**：`reply` 原样带候选清单回给用户；
7. **权限/后端报错不外抛**：回可读中文；
8. **开关关 = 零外网**（走真 dispatch + 桩出网客户端，请求数断言为 0）；
9. 渠道层**自己不拼文案、不读库、不出网**（只递 kind/query/session_key）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from scholarscout import paths
from scholarscout.channels import feishu
from scholarscout.fetch.http import HttpClient
from scholarscout.rpc.methods import AppContext, dispatch
from scholarscout.state import pairing
from scholarscout.state.learning import save_topic_map
from scholarscout.state.sessions import create_session
from scholarscout.tools import register_tools

from ..conftest import redirect_home

CHAT_ID = "oc_file_chan_0001"
SENDER = "ou_file_chan_user"
ACK = "已接受消息，正在处理中"
FILE_REPLY = "已发送：topic_注意力机制.md（2 KB）"

GRAPH = {
    "nodes": [{"nid": "n1", "label": "RAG", "kind": "method", "source_ids": ["src_test0001"]}],
    "edges": [],
}

_ENV_KEYS = ("FEISHU_DM_POLICY", "FEISHU_GROUP_POLICY", "FEISHU_BOT_OPEN_ID", "FEISHU_BOT_NAME")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    redirect_home(monkeypatch, tmp_path)
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(feishu.ACK_TEXT_ENV, raising=False)
    yield
    for key in _ENV_KEYS:
        os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _registered() -> None:
    register_tools()


def _payload(text: str, *, message_id: str = "om_file_msg_0001") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"event_id": "evt_file_0001", "event_type": feishu.RECEIVE_EVENT},
        "event": {
            "sender": {"sender_id": {"open_id": SENDER}},
            "message": {
                "message_id": message_id,
                "chat_id": CHAT_ID,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _handler(
    ctx: AppContext,
    sender: feishu.RecordingSender | feishu.Sender,
    *,
    dispatcher: feishu.Dispatcher = dispatch,
) -> feishu.FeishuMessageHandler:
    return feishu.FeishuMessageHandler(
        ctx,
        credentials=feishu.FeishuCredentials(app_id="cli_file", app_secret="sec_file"),
        sender=sender,
        dispatcher=dispatcher,
        ack_text=ACK,
    )


def _approve(ctx: AppContext, open_id: str = SENDER) -> None:
    pairing.approve_user(ctx.db, open_id, user_name="Stub")


_DEFAULT = object()


class Recorder:
    """只拦 `channel.send_file`：记下入参并按脚本回结果；**其余方法照常走真 dispatch**。

    为什么不整条替换：`handle_event` 先要用 `feishu.access` 判白名单 —— 把 dispatcher 全换掉
    等于把"已放行"也一起造假，测出来的就不是产品那条链路了。
    """

    def __init__(self, result: Any = _DEFAULT, *, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._result = {"ok": True, "reply": FILE_REPLY} if result is _DEFAULT else result
        self._raises = raises

    def __call__(self, ctx: AppContext, name: str, params: Any = None) -> Any:
        if name != feishu.FILE_DELIVERY_METHOD:
            return dispatch(ctx, name, params)
        self.calls.append((name, dict(params or {})))
        if self._raises is not None:
            raise self._raises
        return self._result


# ================================================================== ① 三类目标 · 识别


@pytest.mark.parametrize(
    ("text", "kind", "query"),
    [
        # 本会话/库里最近的生成物（按线索细分成主题图 / 路线 / 笔记）
        ("发我学习路线", "artifact", "route"),
        ("发我知识地图", "artifact", "topic"),
        ("传我那份笔记", "artifact", "note"),
        # 语序反过来（目标在动词之前）
        ("把学习路线发我", "artifact", "route"),
        # 已入库资料：标题里的关键词留下来当模糊匹配词
        ("传我那份注意力机制的论文", "library", "注意力机制"),
        # 项目内路径：原样递上去（白名单由后端 `paths.resolve_rel` 判）
        ("发我 data/exports/笔记.md", "path", "data/exports/笔记.md"),
    ],
)
def test_three_target_kinds_are_recognized(text: str, kind: str, query: str) -> None:
    request = feishu.parse_file_request(text)

    assert request is not None, f"这句话应该被认成「要文件」：{text}"
    assert (request.kind, request.query) == (kind, query)


def test_library_query_keeps_only_the_search_words() -> None:
    """填充词（那份/我的/文件/论文…）要清掉，否则标题模糊匹配会一条都命不中。"""
    request = feishu.parse_file_request("给我发一份 教育智能体 的文件")
    assert request is not None and request.kind == "library"
    assert request.query == "教育智能体"


@pytest.mark.parametrize(
    "text",
    [
        "给我讲讲学习路线",  # 动词后面跟的是"讲解"，这是提问
        "发我解释一下知识地图",
        "为什么要做知识地图",  # 没有发送动词
        "帮我看看那份论文讲了什么",  # "看看"不是发文件
        "发我一下这篇文章的主要贡献，顺便说说它的局限和未来方向在哪里，我想写进综述里",  # 太长
        "",
    ],
)
def test_questions_and_long_text_are_never_file_requests(text: str) -> None:
    assert feishu.parse_file_request(text) is None, f"这句话被误判成「要文件」：{text}"


# ================================================================== ② 交给 RPC（不跑模型）


def test_file_request_calls_send_file_and_skips_ack_and_model(ctx: AppContext) -> None:
    """识别出来 → 只调 `channel.send_file`；**不发回执、不调模型**（回复本身就是回执）。"""
    ctx.llm = None  # 调模型就会炸 —— 这条断言才有意义
    _approve(ctx)
    recorder = feishu.RecordingSender()
    seen = Recorder()

    decision = _handler(ctx, recorder, dispatcher=seen).handle_event(_payload("发我学习路线"))

    assert decision is not None and decision.action == "file"
    assert [name for name, _ in seen.calls] == ["channel.send_file"], (
        f"发文件只该调 channel.send_file，实际={[n for n, _ in seen.calls]}"
    )
    assert seen.calls[0][1] == {
        "platform": "feishu",
        "open_id": SENDER,
        "kind": "artifact",
        "query": "route",
        "session_key": CHAT_ID,
    }, "渠道层只递这五个字段（谁要 / 哪一类 / 关键词 / 哪个会话），别的都不许替后端决定"
    assert recorder.sent == [(CHAT_ID, FILE_REPLY)], "只回这一条（没有回执、没有模型回答）"
    assert ACK not in [text for _, text in recorder.sent]


def test_three_kinds_are_forwarded_verbatim(ctx: AppContext) -> None:
    """三类目标各自把 `kind`/`query` 原样递上去（渠道层不做二次解释）。"""
    _approve(ctx)
    for text, kind, query in (
        ("发我学习路线", "artifact", "route"),
        ("传我那份注意力机制的论文", "library", "注意力机制"),
        ("发我 data/exports/笔记.md", "path", "data/exports/笔记.md"),
    ):
        seen = Recorder()
        _handler(ctx, feishu.RecordingSender(), dispatcher=seen).handle_event(
            _payload(text, message_id=f"om_{kind}")
        )
        assert seen.calls[0][1]["kind"] == kind and seen.calls[0][1]["query"] == query


def test_unapproved_sender_cannot_ask_for_files(ctx: AppContext) -> None:
    """**未授权要不到文件**：拦截在前，文件解析在后 —— 否则"发我笔记"就是一条越权读取通道。"""
    recorder = feishu.RecordingSender()
    seen = Recorder()

    decision = _handler(ctx, recorder, dispatcher=seen).handle_event(_payload("发我学习路线"))

    assert decision is not None and decision.action == "pair"
    assert seen.calls == [], "未授权的人连 channel.send_file 都不该被调"
    assert recorder.sent == [(CHAT_ID, feishu.PAIRING_PROMPT)]


# ================================================================== ③ 找不到 / 权限 / 崩溃


def test_not_found_reply_with_candidates_is_passed_through(ctx: AppContext) -> None:
    """**找不到就不发**：后端回的那段话（含候选清单）原样回给用户，渠道层不重写。"""
    _approve(ctx)
    recorder = feishu.RecordingSender()
    not_found = {
        "ok": False,
        "kind": "library",
        "reply": "没找到标题里带「量子计算」的已入库资料。\n现在能发这些：\n· A Test Paper",
        "candidates": ["A Test Paper"],
    }

    decision = _handler(ctx, recorder, dispatcher=Recorder(not_found)).handle_event(
        _payload("传我那份量子计算的论文")
    )

    assert decision is not None and decision.action == "file"
    assert recorder.sent == [(CHAT_ID, not_found["reply"])]
    assert "现在能发这些" in recorder.sent[0][1]


def test_permission_error_is_readable_and_never_raised(ctx: AppContext) -> None:
    """权限没勾（99991672）：后端回可读中文，渠道层原样转达，**不当异常抛**。"""
    _approve(ctx)
    recorder = feishu.RecordingSender()
    denied = {
        "ok": False,
        "kind": "artifact",
        "file_name": "topic_x.md",
        "reply": "没发出去：缺权限（99991672）：请勾 im:resource 与 im:message",
    }

    decision = _handler(ctx, recorder, dispatcher=Recorder(denied)).handle_event(
        _payload("发我知识地图")
    )

    assert decision is not None and decision.action == "file"
    assert "im:resource" in recorder.sent[0][1] and "im:message" in recorder.sent[0][1]


@pytest.mark.parametrize(
    "result",
    [
        RuntimeError("RPC 断了"),
        None,  # 后端没给结果（老版本 / 分发失败）
        {"ok": False},  # 有结果但没 reply
    ],
)
def test_broken_backend_becomes_a_readable_reply_not_an_exception(
    ctx: AppContext, result: Any
) -> None:
    """回包缺失 / 抛异常都只回一句人话：长连接回调里抛异常等于整条链路静默失聪。"""
    _approve(ctx)
    recorder = feishu.RecordingSender()
    dispatcher = Recorder(result, raises=result if isinstance(result, BaseException) else None)

    decision = _handler(ctx, recorder, dispatcher=dispatcher).handle_event(_payload("发我学习路线"))

    assert decision is not None and decision.action == "file"
    assert len(recorder.sent) == 1
    text = recorder.sent[0][1]
    assert text.strip(), "不许回空串（用户会以为没收到）"
    assert "发不了" in text or "没发出去" in text or "已发送" in text


def test_file_request_for_a_subtype_the_library_lacks_still_answers(ctx: AppContext) -> None:
    """点名"笔记"但库里没有笔记：后端回候选/提示，用户拿到一句话，不是沉默。"""
    _approve(ctx)
    recorder = feishu.RecordingSender()
    empty = {"ok": False, "kind": "artifact", "reply": "库里还没有可发的生成物。", "candidates": []}

    _handler(ctx, recorder, dispatcher=Recorder(empty)).handle_event(_payload("传我那份笔记"))

    assert recorder.sent and "还没有可发的生成物" in recorder.sent[0][1]


# ================================================================== ④ 开关关 = 零外网


def test_switch_off_means_zero_outbound_network(ctx: AppContext) -> None:
    """走**真 dispatch**：出站开关关着（产品默认）→ 回"未开启出站发送"，请求数必须为 0。"""
    seen_requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 触发即失败
        seen_requests.append(request)
        raise AssertionError(f"开关关着还发了外网请求：{request.url}")

    ctx.http = HttpClient(respect_robots=False, transport=httpx.MockTransport(handle))
    _approve(ctx)
    session_id = create_session(ctx.db, session_key=CHAT_ID)
    save_topic_map(ctx.db, topic="注意力机制", graph=GRAPH, session_id=session_id)

    recorder = feishu.RecordingSender()
    decision = _handler(ctx, recorder).handle_event(_payload("发我知识地图"))

    assert decision is not None and decision.action == "file"
    assert seen_requests == [], "开关关着一次外网都不许发"
    assert "未开启出站发送" in recorder.sent[0][1]
    # 导出件本身还是生成了（发不出去不代表没解析到目标）—— 但它没被打包上网络
    assert list(paths.under_data(Path("exports")).glob("topic_*.md")), "目标文件应已导出"


def test_empty_message_text_is_not_a_file_request(ctx: AppContext) -> None:
    """空文本（或只有标点）走正常问答路径，不许被当成"最近的那件生成物"。"""
    assert feishu.parse_file_request("。。。") is None
