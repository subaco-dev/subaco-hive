"""冪等キー（request_id）による再送非重複のテスト。

書込系ツール（hive_join / hive_post / hive_remember）は request_id を冪等キーとし、
processed_requests で突合して重複実行を抑止する（フェイルオーバー後の新ライターも突合可能）。
本ファイルは「同一 request_id の再送が二重書きしない」ことを各ツールで集中的に検証する（stdlib のみ）。

記憶層は zvec を使わず InMemoryVectorBackend + FakeProvider（conftest）で SQLite 状態機械を検証する。
"""

from __future__ import annotations

import pytest

from subaco_hive import messaging
from subaco_hive.memory import InMemoryVectorBackend, MemoryStore
from subaco_hive.messaging import Session


def _store(conn, provider):
    return MemoryStore(conn, provider, backend=InMemoryVectorBackend())


def _proc_count(conn, request_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM processed_requests WHERE request_id = ?", (request_id,)
    ).fetchone()[0]


# ---- hive_join の冪等----------------------------------------------------------
def test_join_resend_same_request_id_no_duplicate_member(conn):
    """同一 request_id の再送は members を二重作成せず、トークンを再発行する。"""
    r1 = messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="J1")
    r2 = messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="J1")
    assert r1.created and not r2.created and r2.reused
    # 名義は 1 行のみ。再送でトークンは再発行される（応答喪失時の名義ロックアウト回避）。
    assert conn.execute("SELECT COUNT(*) FROM members WHERE name='w'").fetchone()[0] == 1
    assert r2.token and r2.token != r1.token
    assert messaging.member_row(conn, "alpha", "w")["token_hash"] == messaging.hash_token(r2.token)


def test_join_distinct_request_ids_are_reconnect_not_new_row(conn):
    """異なる request_id での同名 join は再接続扱い（トークン照合必須）で新規行を作らない。"""
    r1 = messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="J1")
    r2 = messaging.hive_join(
        conn, team="alpha", name="w", vendor="x", request_id="J2", token=r1.token
    )
    assert not r2.created and not r2.reused
    assert conn.execute("SELECT COUNT(*) FROM members WHERE name='w'").fetchone()[0] == 1
    # 新規 request の再接続は processed_requests に記録される（突合の対象になる）。
    assert _proc_count(conn, "J2") == 1


def test_join_processed_requests_single_row_per_id(conn):
    """初回 join の request_id は processed_requests にちょうど 1 行。再送しても増えない。"""
    messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="J1")
    assert _proc_count(conn, "J1") == 1
    messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="J1")
    assert _proc_count(conn, "J1") == 1


# ---- hive_post の冪等------------------------------------------------------------------
def test_post_resend_same_request_id_no_duplicate_message(conn):
    messaging.hive_join(conn, team="alpha", name="s", vendor="x", request_id="J1")
    sess = Session("alpha", "s")
    r1 = messaging.hive_post(conn, sess, body="hello", request_id="P1")
    r2 = messaging.hive_post(conn, sess, body="hello", request_id="P1")
    assert r1.accepted and not r1.reused
    assert r2.reused and r2.message_id is None
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    # mcp_posts 台帳も二重記録されない（trust 解決の基準）。
    assert conn.execute("SELECT COUNT(*) FROM mcp_posts").fetchone()[0] == 1


def test_post_distinct_request_ids_create_distinct_messages(conn):
    messaging.hive_join(conn, team="alpha", name="s", vendor="x", request_id="J1")
    sess = Session("alpha", "s")
    messaging.hive_post(conn, sess, body="one", request_id="P1")
    messaging.hive_post(conn, sess, body="two", request_id="P2")
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_post_reject_does_not_consume_request_id(conn):
    """秘密検査 reject の再送は非 INSERT（processed_requests にも入らないため再試行余地を残す）。"""
    messaging.hive_join(conn, team="alpha", name="s", vendor="x", request_id="J1")
    sess = Session("alpha", "s")
    r = messaging.hive_post(conn, sess, body="key AKIAIOSFODNN7EXAMPLE end", request_id="P1")
    assert not r.accepted
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert _proc_count(conn, "P1") == 0  # reject は冪等キーを消費しない


# ---- hive_remember の冪等（二段書きの committed 更新と同一 tx で記録）-------------
def test_remember_resend_same_request_id_no_duplicate_memory(conn, provider):
    messaging.hive_join(
        conn, team="alpha", name="a", vendor="x", request_id="J1", trusted_agents={"a": None}
    )
    st = _store(conn, provider)
    sess = Session("alpha", "a")
    r1 = st.hive_remember(sess, kind="finding", text="sky is blue", request_id="rid-1")
    r2 = st.hive_remember(sess, kind="finding", text="sky is blue", request_id="rid-1")
    assert r1.accepted and not r1.reused
    assert r2.reused and r2.memory_id is None
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert _proc_count(conn, "rid-1") == 1


def test_remember_pending_insert_does_not_record_request_id(conn, provider):
    """pending 挿入時点では processed_requests に記録しない（committed 更新と同一 tx）。

    Zvec insert 前にクラッシュしても再送が「処理済み」と誤判定されないための不変条件。
    """
    messaging.hive_join(
        conn, team="alpha", name="a", vendor="x", request_id="J1", trusted_agents={"a": None}
    )

    class FailingBackend(InMemoryVectorBackend):
        def insert(self, *a, **k):
            raise RuntimeError("Zvec insert 前クラッシュを模擬")

    st = MemoryStore(conn, provider, backend=FailingBackend())
    with pytest.raises(RuntimeError):
        st.hive_remember(Session("alpha", "a"), kind="finding", text="x", request_id="rid-1")
    # pending 行は残るが、request_id は未記録（＝再送が処理可能）。
    assert _proc_count(conn, "rid-1") == 0
    assert conn.execute("SELECT status FROM memories").fetchone()["status"] == "pending"
