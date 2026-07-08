"""agmsg 互換ビュー／エクスポートのテスト（stdlib のみ）。"""

from __future__ import annotations

from subaco_hive import agmsg_compat, messaging
from subaco_hive.messaging import Session


def _seed(conn):
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
    res = messaging.hive_post(conn, Session("alpha", "alice"), body="hello room", request_id="p1")
    # bob が既読化。
    conn.execute(
        "INSERT OR IGNORE INTO message_reads(message_id, member, read_at) VALUES(?,?,?)",
        (res.message_id, "bob", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    return res.message_id


def test_export_reconstructs_read_by_without_via(conn):
    mid = _seed(conn)
    rows = agmsg_compat.export_rows(conn, team="alpha")
    row = next(r for r in rows if r["id"] == mid)
    assert row["read_by"] == ["bob"]  # message_reads から再構成
    assert "via" not in row  # via は投影しない


def test_view_projection_excludes_via(conn):
    _seed(conn)
    agmsg_compat.create_view(conn)
    cols = [
        d[0]
        for d in conn.execute(f"SELECT * FROM {agmsg_compat.AGMSG_VIEW_NAME} LIMIT 1").description
    ]
    assert "via" not in cols
    assert {"id", "team", "sender", "recipient", "body", "created_at", "read_by"} <= set(cols)


def test_view_read_by_json_group_array(conn):
    mid = _seed(conn)
    agmsg_compat.create_view(conn)
    row = conn.execute(
        f"SELECT read_by FROM {agmsg_compat.AGMSG_VIEW_NAME} WHERE id=?", (mid,)
    ).fetchone()
    import json

    assert json.loads(row["read_by"]) == ["bob"]
