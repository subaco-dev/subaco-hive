"""audit 層のテスト（stdlib のみ）。"""

from __future__ import annotations

import json

import pytest

from subaco_hive import audit


def test_build_summary_drops_none_and_serializes():
    s = audit.build_summary(tool="hive_post", recipient=None, body_len=12, verdict="ok")
    d = json.loads(s)
    assert d == {"tool": "hive_post", "body_len": 12, "verdict": "ok"}  # None は落ちる


@pytest.mark.parametrize("key", ["body", "text", "content", "token", "redacted_text"])
def test_build_summary_rejects_body_keys(key):
    # 本文系キーは audit に載せられない（機械的に弾く）。
    with pytest.raises(ValueError):
        audit.build_summary(**{key: "AKIAIOSFODNN7EXAMPLE"})


def test_record_writes_row(conn):
    rid = audit.record(
        conn,
        team="alpha",
        tool="hive_post",
        member="alice",
        args_summary=audit.build_summary(tool="hive_post", body_len=3),
    )
    assert rid > 0
    row = conn.execute("SELECT * FROM audit WHERE id=?", (rid,)).fetchone()
    assert row["team"] == "alpha" and row["tool"] == "hive_post" and row["member"] == "alice"
    assert "AKIA" not in (row["args_summary"] or "")
