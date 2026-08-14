"""ops CLI backup の fail-closed テスト（stdlib のみ——レビュー指摘の再現）。"""

from __future__ import annotations

import argparse

from subaco_hive import cli, db, writer


def _setup_hive(tmp_path, monkeypatch, *, with_memory: bool):
    hive = tmp_path / ".hive"
    hive.mkdir(mode=0o700)
    (hive / "team").write_text("alpha")
    db.init_db(hive / "messages.db", "alpha", embedding_model="fake", embedding_dim=8).close()
    monkeypatch.setenv("HIVE_DB_PATH", str(hive / "messages.db"))
    monkeypatch.setenv("HIVE_TEAM", "alpha")
    if with_memory:
        coll = hive / "memory" / "hive_alpha"
        coll.mkdir(parents=True)
        (coll / "data").write_text("x")
    return hive


def test_backup_fails_closed_when_writer_holds_lock_and_memory_exists(tmp_path, monkeypatch):
    """常駐ライター稼働中（flock 保持）で Zvec コレクションが在るなら backup は中止する。

    稼働中コレクションの copytree は破損スナップショットを「backup 完了」として残すため、
    警告続行ではなく fail-closed でなければならない。
    """
    hive = _setup_hive(tmp_path, monkeypatch, with_memory=True)
    dest = tmp_path / "bkp"
    fh = writer.try_acquire_lock(hive)  # 常駐ライターを模して flock を保持
    assert fh is not None
    try:
        rc = cli.cmd_backup(argparse.Namespace(path=str(dest)))
    finally:
        writer.release_lock(fh)
    assert rc == 1
    assert not dest.exists()  # 中止時は不完全な産物を残さない

    # ライター不在なら成功し、コレクションも同梱される。
    rc2 = cli.cmd_backup(argparse.Namespace(path=str(dest)))
    assert rc2 == 0
    assert (dest / "memory" / "hive_alpha" / "data").exists()


def test_backup_sqlite_only_proceeds_with_live_writer(tmp_path, monkeypatch):
    """コレクション不在（メッセージングのみ）なら、ライター稼働中でも SQLite backup は安全に続行。"""
    hive = _setup_hive(tmp_path, monkeypatch, with_memory=False)
    dest = tmp_path / "bkp"
    fh = writer.try_acquire_lock(hive)
    assert fh is not None
    try:
        rc = cli.cmd_backup(argparse.Namespace(path=str(dest)))
    finally:
        writer.release_lock(fh)
    assert rc == 0
    assert (dest / "messages.db").exists()
    assert not (dest / "memory").exists()
