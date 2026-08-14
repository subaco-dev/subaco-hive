"""agmsg 形エクスポート面のテスト（stdlib のみ。仕様の正典は 06_spike結果 §3）。"""

from __future__ import annotations

import sqlite3

from subaco_hive import agmsg_compat, messaging
from subaco_hive.messaging import Session

READ_AT = "2026-01-01T00:00:00+00:00"

# agmsg v1.1.13 の実 DDL（06_spike結果 §2.1 の実測値をそのまま固定）。
# 将来ブリッジを実装するときの契約回帰ガード——agmsg が発行する SQL がこの DDL で通ること。
AGMSG_REAL_DDL = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team TEXT NOT NULL,
  from_agent TEXT NOT NULL,
  to_agent TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_unread  ON messages(team, to_agent, read_at) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_history ON messages(team, created_at DESC);
"""


def _seed(conn):
    """broadcast 1 件 + bob 宛 DM 1 件（bob 既読）を投入し (broadcast_id, dm_id) を返す。"""
    messaging.hive_join(
        conn,
        team="alpha",
        name="alice",
        vendor="x",
        request_id="j1",
        trusted_agents={"alice": None},
    )
    messaging.hive_join(
        conn, team="alpha", name="bob", vendor="x", request_id="j2", trusted_agents={"bob": None}
    )
    s = Session("alpha", "alice")
    bcast = messaging.hive_post(conn, s, body="hello room", request_id="p1")
    dm = messaging.hive_post(conn, s, body="direct to bob", recipient="bob", request_id="p2")
    conn.execute(
        "INSERT OR IGNORE INTO message_reads(message_id, member, read_at) VALUES(?,?,?)",
        (dm.message_id, "bob", READ_AT),
    )
    conn.commit()
    return bcast.message_id, dm.message_id


def test_export_projects_agmsg_columns(conn):
    bcast_id, dm_id = _seed(conn)
    rows = {r["id"]: r for r in agmsg_compat.export_rows(conn, team="alpha")}
    dm = rows[dm_id]
    assert dm["from_agent"] == "alice" and dm["to_agent"] == "bob"
    assert dm["read_at"] == READ_AT  # 1:1 行は当該宛先の既読時刻を投影
    bcast = rows[bcast_id]
    assert bcast["to_agent"] is None  # ブロードキャストは hive 拡張（agmsg に存在しない）
    assert bcast["read_at"] is None
    # agmsg に存在しない列・出所メタは出さない。
    assert "via" not in dm and "read_by" not in dm
    assert "sender" not in dm and "recipient" not in dm


def test_view_projects_agmsg_columns(conn):
    bcast_id, dm_id = _seed(conn)
    agmsg_compat.create_view(conn)
    cols = [
        d[0]
        for d in conn.execute(f"SELECT * FROM {agmsg_compat.AGMSG_VIEW_NAME} LIMIT 1").description
    ]
    assert set(cols) == {"id", "team", "from_agent", "to_agent", "body", "created_at", "read_at"}
    dm = conn.execute(
        f"SELECT * FROM {agmsg_compat.AGMSG_VIEW_NAME} WHERE id=?", (dm_id,)
    ).fetchone()
    assert dm["from_agent"] == "alice" and dm["to_agent"] == "bob" and dm["read_at"] == READ_AT
    bcast = conn.execute(
        f"SELECT * FROM {agmsg_compat.AGMSG_VIEW_NAME} WHERE id=?", (bcast_id,)
    ).fetchone()
    assert bcast["to_agent"] is None and bcast["read_at"] is None


def test_view_matches_export(conn):
    _seed(conn)
    agmsg_compat.create_view(conn)
    view_rows = [
        dict(r) for r in conn.execute(f"SELECT * FROM {agmsg_compat.AGMSG_VIEW_NAME} ORDER BY id")
    ]
    assert view_rows == agmsg_compat.export_rows(conn, team="alpha")


def test_agmsg_real_ddl_accepts_agmsg_sql():
    """agmsg が発行する SQL（send の INSERT・inbox の SELECT/UPDATE）が実 DDL で通ること。

    別ファイル DB＋scripts ブリッジ構成（06_spike結果 §4.2）の前提を固定する契約テスト。
    """
    db = sqlite3.connect(":memory:")
    db.executescript(AGMSG_REAL_DDL)
    # send.sh L76 相当。
    db.execute(
        "INSERT INTO messages (team, from_agent, to_agent, body) VALUES (?,?,?,?)",
        ("alpha", "alice", "bob", "hi"),
    )
    # inbox.sh L31-35 相当（未読取得）。
    rows = db.execute(
        "SELECT id, from_agent, body, created_at FROM messages"
        " WHERE team=? AND to_agent=? AND read_at IS NULL ORDER BY id",
        ("alpha", "bob"),
    ).fetchall()
    assert len(rows) == 1
    # inbox.sh L74 相当（既読化）。
    db.execute(
        "UPDATE messages SET read_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id IN (?)",
        (rows[0][0],),
    )
    assert db.execute("SELECT COUNT(*) FROM messages WHERE read_at IS NULL").fetchone()[0] == 0


def test_create_view_migrates_old_definition(conn):
    """旧定義（sender/recipient/read_by）のビューが残る DB でも新定義へ移行すること。

    CREATE VIEW IF NOT EXISTS だけでは旧ビューが残り続ける（レビュー指摘）。
    """
    conn.execute(
        f"CREATE VIEW {agmsg_compat.AGMSG_VIEW_NAME} AS "
        "SELECT id, team, sender, recipient, body, created_at, 'x' AS read_by FROM messages"
    )
    conn.commit()
    _seed(conn)
    agmsg_compat.create_view(conn)
    cols = [
        d[0]
        for d in conn.execute(f"SELECT * FROM {agmsg_compat.AGMSG_VIEW_NAME} LIMIT 1").description
    ]
    assert "from_agent" in cols and "to_agent" in cols and "read_at" in cols
    assert "read_by" not in cols and "sender" not in cols
