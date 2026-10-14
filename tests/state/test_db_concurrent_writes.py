"""存储层并发写：**一写多读 + 应用层写锁**（docs/20 §4.2）。

真风险（不是理论，docs/20 §1.1 G）：`append_message` 用 `db.transaction()` 发显式 `BEGIN`，
而它走的是**唯一一条共享连接**（`check_same_thread=False`）→ 两个会话同时落消息时，第二个
`BEGIN` 直接抛 `sqlite3.OperationalError: cannot start a transaction within a transaction`。
更隐蔽的是**交错**：A 的 `BEGIN..COMMIT` 窗口会把 B 的自动提交写入裹进同一事务，
A 一旦回滚就把 B 的写入一起回滚 —— 跨会话丢数据、不报错。

这套用例就是那条风险的**可证伪断言**（`test_concurrent_append_message_...` 修前必须先红）：

1. 两会话并发落消息 → 不抛错、两个会话的 `idx` 各自从 0 连续无重复；
2. 写事务**串行化**：两个线程的临界区在时间上不重叠；
3. 读不被写阻塞，且读到的是**一致快照**（写事务未提交时的写入读不到）；
4. 事务里抛异常后写锁必须释放（否则后续写入全挂）；
5. 同一线程在事务里再写一次**不自锁死**（`append_message` 就是这样写标题的 → 写锁必须是 `RLock`）。

全部用真库（临时文件 + 真迁移），不真调模型、不出网。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from scholarscout.state import sessions
from scholarscout.state.db import Database

from ..conftest import T0

PER_THREAD = 50


def _idx_list(db: Database, session_id: str) -> list[int]:
    return [
        int(row["idx"])
        for row in db.query(
            "SELECT idx FROM messages WHERE session_id = ? ORDER BY idx", (session_id,)
        )
    ]


def test_concurrent_append_message_from_two_sessions_never_raises(db: Database) -> None:
    """docs/20 §1.1 G 的最小复现：两个会话**同时**落消息。

    修前：第二个 `BEGIN` 抛 `cannot start a transaction within a transaction`（测试先红）；
    修后（读连接 + 专用写连接 + 写锁）：两边都不抛错，且每个会话的 `idx` 从 0 连续无重复
    （`UNIQUE(session_id, idx)` 没被破 —— 选下一个 idx 与 INSERT 在同一临界区里）。
    """
    session_a = sessions.create_session(db, model="stub-model")
    session_b = sessions.create_session(db, model="stub-model")
    start = threading.Barrier(2)
    errors: list[str] = []

    def worker(session_id: str) -> None:
        start.wait(timeout=10)
        try:
            for n in range(PER_THREAD):
                # 混着用 user：首条 user 消息会经 `db.execute` 再写一次标题
                sessions.append_message(
                    db,
                    session_id,
                    role="user" if n % 2 == 0 else "assistant",
                    content=f"{session_id}-第{n}条",
                )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(session_id,)) for session_id in (session_a, session_b)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "并发落消息没有跑完（可能卡在锁上）"

    assert errors == [], f"并发落消息抛错了：{errors}"
    for session_id in (session_a, session_b):
        assert _idx_list(db, session_id) == list(range(PER_THREAD)), (
            f"{session_id} 的消息序号不连续（交错/回滚把别的写入裹走了）"
        )


def test_write_transactions_are_serialized_not_interleaved(db: Database) -> None:
    """两个写事务不许重叠：重叠意味着"同一连接上的显式事务"那条老路又回来了。"""
    gate = threading.Barrier(2)
    guard = threading.Lock()
    windows: list[tuple[float, float]] = []
    errors: list[str] = []

    def worker(index: int) -> None:
        gate.wait(timeout=10)
        try:
            with db.transaction() as conn:
                opened = time.perf_counter()
                conn.execute(
                    "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (f"ses_conc{index:02d}", "并发用例", T0, T0),
                )
                time.sleep(0.05)  # 模拟"事务里还要干活"：给另一个线程留出抢锁的机会
                closed = time.perf_counter()
            with guard:
                windows.append((opened, closed))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == [], f"两个写事务互斥失败：{errors}"
    assert len(windows) == 2
    (_first_open, first_close), (second_open, _second_close) = sorted(windows)
    assert second_open >= first_close, f"两个写事务在时间上重叠了：{windows}"


def test_reads_are_not_blocked_by_an_open_write_transaction(db: Database) -> None:
    """写事务开着的时候，读仍要能拿到**一致快照**（读连接 + WAL 的意义所在）。"""
    session_id = sessions.create_session(db, model="stub-model")
    for n in range(5):
        sessions.append_message(db, session_id, role="assistant", content=f"旧消息 {n}")

    in_transaction = threading.Event()
    read_done = threading.Event()
    committed = threading.Event()
    snapshot: dict[str, Any] = {}
    errors: list[str] = []

    def writer() -> None:
        try:
            with db.transaction() as conn:
                in_transaction.set()
                conn.execute(
                    "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("ses_uncommitted", "还没提交", T0, T0),
                )
                committed.wait(timeout=5)  # 等读那边读完再提交
        except Exception as exc:
            errors.append(f"writer: {type(exc).__name__}: {exc}")
        finally:
            in_transaction.set()

    def reader() -> None:
        try:
            assert in_transaction.wait(timeout=5), "写事务没开起来"
            snapshot["idx"] = _idx_list(db, session_id)
            snapshot["sees_uncommitted"] = (
                db.query_one("SELECT id FROM sessions WHERE id = 'ses_uncommitted'") is not None
            )
        except Exception as exc:
            errors.append(f"reader: {type(exc).__name__}: {exc}")
        finally:
            read_done.set()

    write_thread = threading.Thread(target=writer, daemon=True)
    write_thread.start()
    read_thread = threading.Thread(target=reader, daemon=True)
    read_thread.start()
    assert read_done.wait(timeout=10), "读被写挡住了（读路径不该持写锁）"
    committed.set()
    read_thread.join(timeout=10)
    write_thread.join(timeout=10)

    assert errors == [], f"读/写互相踩了：{errors}"
    assert snapshot["idx"] == list(range(5)), f"读到的不是一致快照：{snapshot}"
    assert snapshot["sees_uncommitted"] is False, "读到了未提交的数据"


def test_rollback_releases_the_write_lock(db: Database) -> None:
    """事务里抛异常 → 回滚 + **放锁**；否则一次失败就把整库的写全挂住。"""
    session_id = sessions.create_session(db, model="stub-model")

    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("ses_rollback", "会被回滚", T0, T0),
        )
        raise RuntimeError("事务里炸了")

    assert db.query_one("SELECT id FROM sessions WHERE id = 'ses_rollback'") is None, "没回滚"
    # 锁还在的话，这一句会一直等下去（在 __main__ 里它是本用例挂住的信号）
    sessions.append_message(db, session_id, role="assistant", content="回滚之后还能写")
    assert _idx_list(db, session_id) == [0]


def test_nested_write_in_the_same_transaction_does_not_self_deadlock(db: Database) -> None:
    """同一线程在事务里再经 `db.execute` 写一次不许自锁死 —— 写锁必须是 `RLock`。

    `append_message` 就是这么干的：它 `with db.transaction()` 之后还要调
    `set_title_if_unnamed`（内部是 `db.execute`）。用普通 `Lock` 会**当场自锁**，
    而这是每个新会话第一条消息都会走的路。
    """
    session_id = sessions.create_session(db, model="stub-model")
    done = threading.Event()
    errors: list[str] = []

    def body() -> None:
        try:
            with db.transaction():
                db.execute(
                    "UPDATE sessions SET title = ? WHERE id = ?", ("事务里改的名字", session_id)
                )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            done.set()

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    assert done.wait(timeout=10), "同一线程在事务里再写一次自锁死了（写锁不是 RLock？）"
    assert errors == []
    assert (
        db.query_one("SELECT title FROM sessions WHERE id = ?", (session_id,))["title"]
        == "事务里改的名字"
    )
