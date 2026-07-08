"""db モジュールのテスト（stdlib sqlite3 のみ）。"""

from __future__ import annotations

import sqlite3

import pytest

from subaco_hive import db


def test_init_creates_all_tables(tmp_path):
    conn = db.init_db(
        tmp_path / "messages.db", "myteam", embedding_model="dummy", embedding_dim=384
    )
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for table in db.TABLES:
        assert table in names, f"missing table: {table}"
    conn.close()


def test_init_writes_meta(tmp_path):
    conn = db.init_db(tmp_path / "messages.db", "myteam", embedding_model="m1", embedding_dim=768)
    assert db.get_meta(conn, db.META_SCHEMA_VERSION) == db.SCHEMA_VERSION
    assert db.get_meta(conn, db.META_ACTIVE_COLLECTION) == "hive_myteam"
    assert db.get_meta(conn, db.META_EMBEDDING_MODEL) == "m1"
    assert db.get_meta(conn, db.META_EMBEDDING_DIM) == "768"
    conn.close()


def test_wal_and_busy_timeout(tmp_path):
    conn = db.init_db(tmp_path / "messages.db", "t", embedding_model="m", embedding_dim=1)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert bt == db.DEFAULT_BUSY_TIMEOUT_MS
    conn.close()


def test_open_verifies_schema_version(tmp_path):
    path = tmp_path / "messages.db"
    db.init_db(path, "t", embedding_model="m", embedding_dim=1).close()
    conn = db.open_db(path)  # 一致すれば例外なし
    db.verify_schema_version(conn)
    conn.close()


def test_open_missing_db_raises(tmp_path):
    with pytest.raises(db.SchemaError):
        db.open_db(tmp_path / "nope.db")


def test_open_uninitialized_raises(tmp_path):
    path = tmp_path / "empty.db"
    # スキーマ無しの空 DB を作る。
    sqlite3.connect(str(path)).close()
    with pytest.raises(db.SchemaError):
        db.open_db(path)


def test_schema_version_mismatch(tmp_path, monkeypatch):
    path = tmp_path / "messages.db"
    db.init_db(path, "t", embedding_model="m", embedding_dim=1).close()
    conn = db.connect(path)
    db.set_meta(conn, db.META_SCHEMA_VERSION, "999")
    with pytest.raises(db.SchemaError):
        db.verify_schema_version(conn)
    conn.close()


def test_init_is_idempotent_and_preserves_active_collection(tmp_path):
    path = tmp_path / "messages.db"
    conn = db.init_db(path, "t", embedding_model="m", embedding_dim=1)
    # reembed が active_collection を差し替えた状況を模す。
    db.set_meta(conn, db.META_ACTIVE_COLLECTION, "hive_t__reembed_123")
    conn.close()
    # 再 init しても既存の active_collection は温存される。
    conn2 = db.init_db(path, "t", embedding_model="m", embedding_dim=1)
    assert db.get_meta(conn2, db.META_ACTIVE_COLLECTION) == "hive_t__reembed_123"
    conn2.close()


def test_active_collection_helper(tmp_path):
    conn = db.init_db(tmp_path / "messages.db", "teamz", embedding_model="m", embedding_dim=1)
    assert db.active_collection(conn) == "hive_teamz"
    conn.close()


def test_verify_embedding(tmp_path):
    conn = db.init_db(tmp_path / "messages.db", "t", embedding_model="m1", embedding_dim=384)
    db.verify_embedding(conn, model="m1", dim=384)  # 一致
    with pytest.raises(db.SchemaError):
        db.verify_embedding(conn, model="m2", dim=384)
    with pytest.raises(db.SchemaError):
        db.verify_embedding(conn, model="m1", dim=512)
    conn.close()
