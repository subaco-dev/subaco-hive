"""messaging 層のテスト（stdlib のみ）。"""

from __future__ import annotations

import pytest

from subaco_hive import messaging
from subaco_hive.messaging import Session, hash_token


def _join(conn, name, **kw):
    kw.setdefault("vendor", "claude-code")
    kw.setdefault("request_id", f"r-{name}-{kw.pop('rid', '1')}")
    return messaging.hive_join(conn, team="alpha", name=name, **kw)


# ---- join: 冪等・トークン・異名 team------------------------------------------------
def test_first_join_issues_token_untrusted(conn):
    res = _join(conn, "worker")
    assert res.created and res.trust_level == messaging.TRUST_UNTRUSTED
    assert res.token  # 初回はトークン発行
    row = messaging.member_row(conn, "alpha", "worker")
    assert row["token_hash"] == hash_token(res.token)


def test_resend_reissues_token(conn):
    r1 = messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="same")
    r2 = messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="same")
    assert r2.reused and not r2.created
    assert r2.token and r2.token != r1.token  # 再送でトークン再発行
    row = messaging.member_row(conn, "alpha", "w")
    assert row["token_hash"] == hash_token(r2.token)
    assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 1


def test_reconnect_requires_token(conn):
    r1 = messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="a")
    ok = messaging.hive_join(
        conn, team="alpha", name="w", vendor="x", request_id="b", token=r1.token
    )
    assert not ok.created and ok.token is None  # 照合成功・再発行なし
    with pytest.raises(messaging.TokenError):
        messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="c", token="wrong")
    with pytest.raises(messaging.TokenError):
        messaging.hive_join(conn, team="alpha", name="w", vendor="x", request_id="d")


def test_team_mismatch_rejected(conn):
    _join(conn, "w")
    with pytest.raises(messaging.TeamMismatchError):
        messaging.hive_join(conn, team="beta", name="w2", vendor="x", request_id="z")


# ---- 許可リスト（trusted_agents）------------------------------------------------
def test_trusted_agents_grant_trust1_on_creation(conn):
    res = _join(conn, "vip", trusted_agents={"vip": None})
    assert res.trust_level == messaging.TRUST_NORMAL
    assert res.token  # トークン非併記なので発行して返す


def test_trusted_preshared_token_squatting_rejected(conn):
    ta = {"vip": hash_token("preshared-xyz")}
    with pytest.raises(messaging.SquattingRejectedError):
        _join(conn, "vip", trusted_agents=ta)  # 未提示 → 行を作らず拒否
    assert messaging.member_row(conn, "alpha", "vip") is None
    with pytest.raises(messaging.SquattingRejectedError):
        _join(conn, "vip", token="wrong", trusted_agents=ta, rid="2")
    assert messaging.member_row(conn, "alpha", "vip") is None
    # 正しい事前共有トークンで初回 join 成立、以後もそれで再接続できる。
    ok = _join(conn, "vip", token="preshared-xyz", trusted_agents=ta, rid="3")
    assert ok.created and ok.trust_level == messaging.TRUST_NORMAL and ok.token is None
    re = messaging.hive_join(
        conn, team="alpha", name="vip", vendor="x", request_id="rc", token="preshared-xyz"
    )
    assert not re.created


# ---- post: 秘密検査 / 実在検証 / 冪等--------------------------------------
def test_post_broadcast_and_ledger(conn):
    _join(conn, "sender")
    res = messaging.hive_post(conn, Session("alpha", "sender"), body="hello", request_id="p1")
    assert res.accepted and res.message_id
    assert conn.execute("SELECT COUNT(*) FROM mcp_posts").fetchone()[0] == 1


def test_post_secret_rejected_not_inserted(conn):
    _join(conn, "s")
    res = messaging.hive_post(
        conn, Session("alpha", "s"), body="key AKIAIOSFODNN7EXAMPLE end", request_id="p1"
    )
    assert not res.accepted and res.message_id is None and res.reason_codes
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_post_unknown_recipient(conn):
    _join(conn, "s")
    with pytest.raises(messaging.UnknownRecipientError):
        messaging.hive_post(
            conn, Session("alpha", "s"), body="hi", recipient="ghost", request_id="p1"
        )
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_post_idempotent_resend(conn):
    _join(conn, "s")
    messaging.hive_post(conn, Session("alpha", "s"), body="hi", request_id="p1")
    r2 = messaging.hive_post(conn, Session("alpha", "s"), body="hi", request_id="p1")
    assert r2.reused
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


# ---- inbox: trust フィルタ・通知一度きり・include_untrusted-----------------
def _cli_insert(conn, sender, body, via="cli", recipient=None):
    """CLI 直書き（mcp_posts に記録しない）。台帳外＝trust=0。"""
    conn.execute(
        "INSERT INTO messages(team, sender, recipient, body, created_at, via) VALUES(?,?,?,?,?,?)",
        ("alpha", sender, recipient, body, "2026-01-01T00:00:00+00:00", via),
    )
    conn.commit()


def test_inbox_trust_filter_and_notify_once(conn):
    _join(conn, "reader")
    _join(conn, "alice", trusted_agents={"alice": None})  # trust=1
    messaging.hive_post(conn, Session("alpha", "alice"), body="trusted hi", request_id="a1")
    _cli_insert(conn, "cli_bob", "untrusted hi", via="mcp")  # via を詐称しても台帳外＝trust=0

    res = messaging.hive_inbox(conn, Session("alpha", "reader"))
    by_id = {e.trust: e for e in res.entries}
    assert by_id[1].body is not None and "trusted hi" in by_id[1].body  # 本文配送
    assert by_id[0].body is None and by_id[0].via == "cli"  # メタデータのみ・via=cli 表示

    # 2 回目: 既読/通知済みで何も返らない（再通知抑止）。
    assert messaging.hive_inbox(conn, Session("alpha", "reader")).entries == []

    # include_untrusted で未信頼本文を取得できる。
    res3 = messaging.hive_inbox(conn, Session("alpha", "reader"), include_untrusted=True)
    assert any(e.body and "untrusted hi" in e.body for e in res3.entries)


def test_inbox_untrusted_body_redacted_on_read(conn):
    _join(conn, "reader")
    _cli_insert(conn, "cli_bob", "leak AKIAIOSFODNN7EXAMPLE now")  # CLI 直書きは書込検査を迂回
    res = messaging.hive_inbox(conn, Session("alpha", "reader"), include_untrusted=True)
    body = next(e.body for e in res.entries if e.body)
    assert "AKIAIOSFODNN7EXAMPLE" not in body and "[REDACTED]" in body


def test_inbox_dm_predicate(conn):
    _join(conn, "reader")
    _join(conn, "alice", trusted_agents={"alice": None})
    messaging.hive_post(
        conn, Session("alpha", "alice"), body="for reader", recipient="reader", request_id="a1"
    )
    messaging.hive_post(
        conn, Session("alpha", "alice"), body="for other", recipient="alice", request_id="a2"
    )  # 自分宛（reader には来ない）
    res = messaging.hive_inbox(conn, Session("alpha", "reader"))
    bodies = [e.body for e in res.entries if e.body]
    assert any("for reader" in b for b in bodies)
    assert not any("for other" in b for b in bodies)


def test_sender_excluded_from_own_inbox(conn):
    _join(conn, "alice", trusted_agents={"alice": None})
    messaging.hive_post(conn, Session("alpha", "alice"), body="mine", request_id="a1")
    assert messaging.hive_inbox(conn, Session("alpha", "alice")).entries == []


# ---- members / history --------------------------------------------------------------------------
def test_members_excludes_cli_participants(conn):
    _join(conn, "alice", trusted_agents={"alice": None})
    _cli_insert(conn, "cli_bob", "hi")
    names = {m.name for m in messaging.hive_members(conn, Session("alpha", "alice"))}
    assert names == {"alice"}  # CLI 参加者は members に現れない


def test_history_broadcast_only_and_redacts_untrusted(conn):
    _join(conn, "reader")
    _join(conn, "alice", trusted_agents={"alice": None})
    messaging.hive_post(conn, Session("alpha", "alice"), body="broadcast", request_id="a1")
    messaging.hive_post(
        conn, Session("alpha", "alice"), body="dm only", recipient="reader", request_id="a2"
    )
    _cli_insert(conn, "cli_bob", "leak AKIAIOSFODNN7EXAMPLE x")
    res = messaging.hive_history(conn, Session("alpha", "reader"), limit=50)
    texts = "\n".join(e.body for e in res.entries if e.body)
    assert "broadcast" in texts and "dm only" not in texts  # DM は履歴に出ない
    assert "AKIAIOSFODNN7EXAMPLE" not in texts  # 未信頼は読出時赤塗り


# ---- admin-----------------------------------------------------------------------------
def test_admin_set_trust_persists_and_no_rollback_on_rejoin(conn):
    r = _join(conn, "worker")
    messaging.admin_set_trust(conn, team="alpha", name="worker", level=2)
    assert messaging.member_trust(conn, "alpha", "worker") == 2
    # 再 join（トークン照合）しても trust は巻き戻らない。
    messaging.hive_join(
        conn, team="alpha", name="worker", vendor="x", request_id="rj", token=r.token
    )
    assert messaging.member_trust(conn, "alpha", "worker") == 2


def test_admin_reset_token(conn):
    _join(conn, "worker")
    token, _ = messaging.admin_reset_token(conn, team="alpha", name="worker")
    assert messaging.member_row(conn, "alpha", "worker")["token_hash"] == hash_token(token)
    with pytest.raises(messaging.MessagingError):
        messaging.admin_reset_token(conn, team="alpha", name="nobody")
