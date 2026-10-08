"""看板可编辑：勾子问题覆盖状态要真的落库（大浩选定）。"""

from __future__ import annotations

import pytest

from scholarscout.runtime import research_state


def test_set_question_status_writes_through(db):
    research_state.save_state(
        db,
        "ses_x",
        {"questions": [{"q": "一", "status": "open"}, {"q": "二", "status": "open"}]},
    )
    state = research_state.set_question_status(db, "ses_x", 2, "covered")
    assert state["questions"][1]["status"] == "covered"
    assert state["questions"][0]["status"] == "open"
    # 重新读一遍：确认是真落库，不是只改了内存里的那份
    again = research_state.load_state(db, "ses_x")
    assert again["questions"][1]["status"] == "covered"


def test_bad_index_or_status_is_rejected_instead_of_silently_written(db):
    research_state.save_state(db, "ses_y", {"questions": [{"q": "一", "status": "open"}]})
    with pytest.raises(ValueError):
        research_state.set_question_status(db, "ses_y", 5, "covered")
    with pytest.raises(ValueError):
        research_state.set_question_status(db, "ses_y", 1, "unknown")
    assert research_state.load_state(db, "ses_y")["questions"][0]["status"] == "open"
