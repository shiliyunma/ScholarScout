"""`card_index` 口径契约：**同一个数字不能有两种含义**，两种含义必须在文档里写清并各自钉住。

现状（也是本文件钉住的口径，见 `docs/01 §3.4`）：

- `note.review` / `note.reveal` / `note.history`：**0 起**，就是卡在 `cards[]` 里的下标
  （`card.card_index`）；
- `note.grade` / `note.extend` / `note.help`：**1 起**，是界面上的「第几张」，0 直接报 `-32602`
  （既有流式契约把这一族钉在 1 起：`tests/rpc/test_practice_stream.py`、`test_events_carry_session_id.py`）。

为什么这张测试要照前端的序列化方式发参数：前端的 `card` 对象里如果没有 `card_index` 这个键，
`JSON.stringify` 会**整个丢掉**它（undefined 不上线），后端只能落到默认值上。真实现状就是这条路：
`note.get` 的 `cards[]` 只有 `index` 键 → 前端读 `card.card_index` 拿到 undefined →
`note.review` 收到的参数里没有 card_index → 默认 -1 → 值域校验抛 INVALID_PARAMS(-32602) →
`autoReview` 的 catch 吞掉 → 界面上"练过了"，库里排期一行也没有。

所以这里的断言不是"函数返回什么"，而是"**前端拿到的那张卡，能把它自己记进排期**"。
"""

from __future__ import annotations

import pytest

from scholarscout.rpc.errors import Code, RpcError
from scholarscout.rpc.methods import AppContext, dispatch

CARD_CONTENT = (
    "## [填空] 第一题？\n**A.** 答一。\n\n"
    "## [填空] 第二题？\n**A.** 答二。\n\n"
    "## [填空] 第三题？\n**A.** 答三。\n"
)


def _card_note(ctx: AppContext, note_id: str = "nt_basis") -> str:
    ctx.db.execute(
        "INSERT INTO notes (id, session_id, kind, title, content, created_at) VALUES (?,?,?,?,?,?)",
        (note_id, "ses_1", "card", "基数契约 抽背卡", CARD_CONTENT, "2026-10-08T00:00:00+08:00"),
    )
    return note_id


def _ui_cards(ctx: AppContext, note_id: str) -> list[dict]:
    """照前端 `startPractice` 的做法：把 `note.get` 的 `cards[]` 变成练习队列。"""
    payload = dispatch(ctx, "note.get", {"note_id": note_id})
    return [{"note_id": note_id, **card} for card in payload["cards"]]


def _ui_params(card: dict, **extra) -> dict:
    """照 `JSON.stringify` 的口径：值为 None 的键**不出现在**参数里。"""
    params: dict = {"note_id": card["note_id"]}
    if card.get("card_index") is not None:
        params["card_index"] = card["card_index"]
    params.update(extra)
    return params


def _row(ctx: AppContext, note_id: str, card_index: int) -> dict | None:
    return ctx.db.query_one(
        "SELECT card_index, rating, reviews, interval_days, due_at, reviewed_at"
        " FROM card_reviews WHERE note_id = ? AND card_index = ?",
        (note_id, card_index),
    )


# ---------------------------------------------------------------- 键：前端读的键必须真实存在


def test_note_get_cards_carry_the_key_the_ui_reads(ctx: AppContext) -> None:
    """`cards[]` 必须带 `card_index`（前端读的就是它），且与下标一致。"""
    note_id = _card_note(ctx)
    cards = _ui_cards(ctx, note_id)
    assert [card.get("card_index") for card in cards] == [0, 1, 2]


# ---------------------------------------------------------------- 记账：0 起 0 和 1 都能记上


@pytest.mark.parametrize("wanted", [0, 1])
def test_ui_card_can_review_itself(ctx: AppContext, wanted: int) -> None:
    """走真实调用路径：前端手里的那张卡喂回 `note.review`，不许回 -32602。"""
    note_id = _card_note(ctx)
    card = _ui_cards(ctx, note_id)[wanted]
    try:
        result = dispatch(ctx, "note.review", _ui_params(card, rating="good"))
    except RpcError as exc:
        pytest.fail(f"note.review 回了 {exc.code}（{exc.message}）—— 参数 {_ui_params(card)}")
    assert result["ok"] is True
    assert result["card_index"] == wanted, "评价落在了别的卡上"
    assert _row(ctx, note_id, wanted) is not None, "库里没有这张卡的排期"
    assert _row(ctx, note_id, wanted)["reviews"] == 1


def test_review_missing_card_index_is_still_an_error(ctx: AppContext) -> None:
    """缺 card_index 仍然要报 -32602：把它改成"默认第 0 张"就是把错记到别的卡上。"""
    note_id = _card_note(ctx)
    with pytest.raises(RpcError) as caught:
        dispatch(ctx, "note.review", {"note_id": note_id, "rating": "good"})
    assert caught.value.code == int(Code.INVALID_PARAMS)


# ---------------------------------------------------------------- 排期：review 之后确实推后


def test_review_pushes_the_next_due_date_forward(ctx: AppContext) -> None:
    """一次 review 把下次复习推到将来；再评一次（good）间隔还要更长。"""
    note_id = _card_note(ctx)
    card = _ui_cards(ctx, note_id)[1]
    dispatch(ctx, "note.review", _ui_params(card, rating="good"))
    first = _row(ctx, note_id, 1)
    assert first is not None
    assert first["due_at"] > first["reviewed_at"], "下次复习时间没有推后"

    dispatch(ctx, "note.review", _ui_params(card, rating="good"))
    second = _row(ctx, note_id, 1)
    assert second["reviews"] == 2
    assert second["interval_days"] > first["interval_days"], "第二次 good 的间隔没有变长"
    assert second["due_at"] > first["due_at"], "第二次复习没有把下次时间再推后"


# ---------------------------------------------------------------- 同基准：其余 note.* 对齐 0 起


def test_reveal_uses_the_same_basis(ctx: AppContext) -> None:
    """同一张卡：`note.get` 的下标喂给 `note.reveal`，取回的必须是它自己的答案。"""
    note_id = _card_note(ctx)
    card = _ui_cards(ctx, note_id)[2]
    payload = dispatch(ctx, "note.reveal", _ui_params(card))
    assert payload["card_index"] == 2
    assert payload["back"] == "答三。"


def test_grade_takes_the_one_based_number(ctx: AppContext) -> None:
    """界面上的「第 2 张」传给 `note.grade` 的是 2；必须判第二张（作答"答二"→ 对）。"""
    note_id = _card_note(ctx)
    card = _ui_cards(ctx, note_id)[1]
    params = {**_ui_params(card), "card_index": card["card_index"] + 1, "response": "答二"}
    result = dispatch(ctx, "note.grade", params)
    assert result["graded"] is True
    assert result["correct"] is True


def test_help_takes_the_one_based_number(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """`note.help` 交给模型的卡必须是前端那一张（看 front 与它回显的编号）。"""
    from scholarscout.runtime import practice

    seen: dict = {}

    def fake_build_help(*args, **kwargs):
        seen.update(kwargs["card"])
        return {"text": "讲一下", "sources": [], "truncated": False, "error": None}

    monkeypatch.setattr(practice, "build_help", fake_build_help)
    ctx.llm = object()  # 走到模型前就被假实现接管，不需要真 provider
    note_id = _card_note(ctx)
    card = _ui_cards(ctx, note_id)[1]
    dispatch(ctx, "note.help", {**_ui_params(card), "card_index": card["card_index"] + 1})
    assert seen["card_index"] == 2
    assert seen["front"] == "第二题？"


def test_one_based_family_rejects_zero(ctx: AppContext) -> None:
    """0 起与 1 起差一张：把 0 传进 1 起那一族必须报错，不许静默落到别的卡上。"""
    note_id = _card_note(ctx)
    for name in ("note.grade", "note.help", "note.extend"):
        with pytest.raises(RpcError) as caught:
            dispatch(ctx, name, {"note_id": note_id, "card_index": 0, "response": "答一"})
        assert caught.value.code == int(Code.INVALID_PARAMS), name


def test_history_uses_the_same_basis(ctx: AppContext) -> None:
    """刚评过的那张卡，作答历史里有且只有这一笔。"""
    note_id = _card_note(ctx)
    card = _ui_cards(ctx, note_id)[0]
    dispatch(ctx, "note.review", _ui_params(card, rating="bad", response="答一", correct=False))
    items = dispatch(ctx, "note.history", _ui_params(card))["items"]
    assert len(items) == 1
    assert items[0]["response"] == "答一"
