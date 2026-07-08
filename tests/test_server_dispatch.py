"""server.dispatch_tool の統合テスト（mcp 無し・純関数経路。DoD 経路）。"""

from __future__ import annotations

from subaco_hive import server
from subaco_hive.memory import InMemoryVectorBackend, MemoryStore


def _ctx(conn, provider, trusted=None):
    store = MemoryStore(conn, provider, backend=InMemoryVectorBackend())
    return server.Context(
        conn=conn,
        default_team="alpha",
        trusted_agents=trusted or {},
        store=store,
        memory_enabled=True,
    )


def _join(ctx, name):
    return server.dispatch_tool(
        ctx,
        {"tool": "hive_join", "args": {"name": name}, "session": None, "request_id": f"j-{name}"},
    )


def test_join_post_inbox_flow(conn, provider):
    ctx = _ctx(conn, provider, trusted={"alice": None, "reader": None})
    ja = _join(ctx, "alice")
    assert ja["trust_level"] == 1 and ja["session"]["name"] == "alice"
    alice = {"team": "alpha", "name": "alice"}
    server.dispatch_tool(
        ctx,
        {"tool": "hive_post", "args": {"body": "hi team"}, "session": alice, "request_id": "p1"},
    )
    reader = {"team": "alpha", "name": "reader"}
    _join(ctx, "reader")
    res = server.dispatch_tool(ctx, {"tool": "hive_inbox", "args": {}, "session": reader})
    assert "hi team" in res["text"]


def test_untrusted_post_not_delivered_by_default(conn, provider):
    # DoD(4) 相当: trust=0 名義（許可リスト外）の投稿は既定 inbox に本文配送されない。
    ctx = _ctx(conn, provider, trusted={"reader": None})  # untrusted は許可リスト外
    _join(ctx, "untrusted")  # trust=0
    _join(ctx, "reader")
    server.dispatch_tool(
        ctx,
        {
            "tool": "hive_post",
            "args": {"body": "sneaky order"},
            "session": {"team": "alpha", "name": "untrusted"},
            "request_id": "p1",
        },
    )
    res = server.dispatch_tool(
        ctx, {"tool": "hive_inbox", "args": {}, "session": {"team": "alpha", "name": "reader"}}
    )
    entry = res["entries"][0]
    assert entry["delivered"] is False and entry["body"] is None  # メタデータのみ


def test_audit_recorded_and_no_secret_body(conn, provider):
    # 全ツールが audit に記録され、拒否された秘密本文は audit に残らない。
    ctx = _ctx(conn, provider, trusted={"alice": None})
    _join(ctx, "alice")
    server.dispatch_tool(
        ctx,
        {
            "tool": "hive_post",
            "args": {"body": "leak AKIAIOSFODNN7EXAMPLE"},
            "session": {"team": "alpha", "name": "alice"},
            "request_id": "p1",
        },
    )
    summaries = [r[0] for r in conn.execute("SELECT args_summary FROM audit").fetchall()]
    assert any("hive_join" in (s or "") for s in summaries)
    assert any("hive_post" in (s or "") for s in summaries)
    assert not any("AKIAIOSFODNN7EXAMPLE" in (s or "") for s in summaries)


def test_unjoined_rejected(conn, provider):
    import pytest

    ctx = _ctx(conn, provider)
    with pytest.raises(Exception, match="未 join"):
        server.dispatch_tool(ctx, {"tool": "hive_post", "args": {"body": "x"}, "session": None})


def test_stats_counts(conn, provider):
    ctx = _ctx(conn, provider, trusted={"alice": None})
    _join(ctx, "alice")
    server.dispatch_tool(
        ctx,
        {
            "tool": "hive_post",
            "args": {"body": "hi"},
            "session": {"team": "alpha", "name": "alice"},
            "request_id": "p1",
        },
    )
    res = server.dispatch_tool(
        ctx, {"tool": "hive_stats", "args": {}, "session": {"team": "alpha", "name": "alice"}}
    )
    assert res["stats"]["messages"] == 1 and res["stats"]["members"] == 1


def test_admin_via_dispatch(conn, provider):
    ctx = _ctx(conn, provider, trusted={"alice": None})
    _join(ctx, "alice")
    server.dispatch_tool(
        ctx,
        {
            "tool": "admin:set-trust",
            "args": {"team": "alpha", "name": "alice", "level": 2},
            "session": None,
            "request_id": "s1",
        },
    )
    from subaco_hive import messaging

    assert messaging.member_trust(conn, "alpha", "alice") == 2
