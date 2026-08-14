"""memory 層のテスト（InMemoryVectorBackend + FakeProvider）。"""

from __future__ import annotations

from subaco_hive import messaging
from subaco_hive.memory import InMemoryVectorBackend, MemoryStore
from subaco_hive.messaging import Session
from subaco_hive.models import MEMORY_COMMITTED, MEMORY_PENDING


def _store(conn, provider):
    return MemoryStore(conn, provider, backend=InMemoryVectorBackend())


def _join(conn, name, trusted=True):
    ta = {name: None} if trusted else {}
    return messaging.hive_join(
        conn, team="alpha", name=name, vendor="x", request_id=f"j-{name}", trusted_agents=ta
    )


def test_remember_two_phase_commits(conn, provider):
    _join(conn, "alice")
    st = _store(conn, provider)
    res = st.hive_remember(
        Session("alpha", "alice"), kind="finding", text="the sky is blue", request_id="m1"
    )
    assert res.accepted and res.memory_id
    row = conn.execute(
        "SELECT status, source_trust FROM memories WHERE id=?", (res.memory_id,)
    ).fetchone()
    assert row["status"] == MEMORY_COMMITTED and row["source_trust"] == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM processed_requests WHERE request_id='m1'").fetchone()[0]
        == 1
    )


def test_remember_idempotent_resend(conn, provider):
    _join(conn, "alice")
    st = _store(conn, provider)
    st.hive_remember(Session("alpha", "alice"), kind="finding", text="x", request_id="m1")
    r2 = st.hive_remember(Session("alpha", "alice"), kind="finding", text="x", request_id="m1")
    assert r2.reused
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_recall_returns_trusted_memory(conn, provider):
    _join(conn, "alice")
    st = _store(conn, provider)
    st.hive_remember(
        Session("alpha", "alice"), kind="finding", text="sky color is blue today", request_id="m1"
    )
    res = st.hive_recall(Session("alpha", "alice"), query="what color is the sky")
    assert res.entries and "blue" in res.entries[0].body
    assert res.entries[0].header.startswith("[memory: author=alice")


def test_recall_hides_source_trust0(conn, provider):
    _join(conn, "eve", trusted=False)  # trust=0
    st = _store(conn, provider)
    res = st.hive_remember(
        Session("alpha", "eve"), kind="finding", text="secret plan sky", request_id="m1"
    )
    assert any("trust は 0" in w for w in res.warnings)  # 警告応答
    assert st.hive_recall(Session("alpha", "eve"), query="sky plan").entries == []


def test_recall_hides_demoted_author(conn, provider):
    _join(conn, "alice")
    st = _store(conn, provider)
    st.hive_remember(Session("alpha", "alice"), kind="finding", text="sky is blue", request_id="m1")
    messaging.admin_set_trust(conn, team="alpha", name="alice", level=0)  # 降格
    assert st.hive_recall(Session("alpha", "alice"), query="sky").entries == []


def test_orphan_cleanup_then_resend_commits_once(conn, provider):
    """crash（pending 孤児）→ 起動時掃除 → 同一 request_id 再送 → 一度だけ committed。"""
    _join(conn, "alice")
    st = _store(conn, provider)
    # crash を模して pending 行のみ作る（processed_requests には入れない — commit 前）。
    conn.execute(
        "INSERT INTO memories(id, team, author, kind, created_at, source_trust, status)"
        " VALUES(?,?,?,?,?,?,?)",
        ("orphan1", "alpha", "alice", "finding", "2026-01-01T00:00:00+00:00", 1, MEMORY_PENDING),
    )
    conn.commit()
    assert st.cleanup_orphans() == 1
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    # 再送（同一 request_id は processed_requests に無いので再実行される）。
    res = st.hive_remember(Session("alpha", "alice"), kind="finding", text="sky", request_id="m1")
    assert res.accepted
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE status='committed'").fetchone()[0] == 1


def test_remember_secret_rejected(conn, provider):
    _join(conn, "alice")
    st = _store(conn, provider)
    res = st.hive_remember(
        Session("alpha", "alice"),
        kind="finding",
        text="token ghp_012345678901234567890123456789012345 x",
        request_id="m1",
    )
    assert not res.accepted and res.reason_codes
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


class _AngleProvider:
    """クエリ= x 軸単位ベクトル・本文=登録した角度、の決定的埋め込み（埋没検証用）。"""

    dim = 8
    model_name = "angle-fake"

    def __init__(self) -> None:
        self.by_text: dict[str, list[float]] = {}

    @staticmethod
    def _vec(deg: float) -> list[float]:
        import math

        r = math.radians(deg)
        v = [0.0] * 8
        v[0] = math.cos(r)
        v[1] = math.sin(r)
        return v

    def embed_query(self, text: str) -> list[float]:
        return self.by_text.get(text, self._vec(0.0))


def test_recall_not_starved_by_low_trust_memories(conn):
    """低 trust 記憶が候補枠を占有しても、正当な記憶が埋没しないこと（レビュー再現）。

    未信頼著者の記憶 21 件がクエリ近傍を占めると、固定 top_k*4=20 件取得では正当な 1 件が
    候補に入らず 0 件になる。取得数の段階拡大で正当な記憶が返ることを固定する。
    """
    provider = _AngleProvider()
    _join(conn, "alice")  # trust=1
    _join(conn, "mallory", trusted=False)  # trust=0
    st = MemoryStore(conn, provider, backend=InMemoryVectorBackend())
    # 未信頼記憶 21 件がクエリ近傍（0.1..2.1 度）を占有する。
    for i in range(21):
        text = f"spam-{i}"
        provider.by_text[text] = provider._vec(0.1 * (i + 1))
        st.hive_remember(Session("alpha", "mallory"), kind="note", text=text, request_id=f"s{i}")
    # 正当な記憶 1 件はやや遠方（30 度）。
    provider.by_text["legit"] = provider._vec(30.0)
    st.hive_remember(Session("alpha", "alice"), kind="note", text="legit", request_id="ok")
    out = st.hive_recall(Session("alpha", "alice"), query="q", top_k=5)
    assert len(out.entries) == 1
    assert out.entries[0].author == "alice"
