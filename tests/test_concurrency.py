"""並行性テスト（中核リスク。エージェント不要のプロセスレベル自動化）。

検証項目:
  (1) N プロセス同時起動での first-writer-wins（flock 保持者が常に 1 つ）。
  (2) ライター kill → プロキシ昇格 → 書き込み継続 → 同一 request_id 再送の非重複（フェイルオーバー）。
  (3) CLI 直書き（messages / message_reads INSERT OR IGNORE）と WAL 並行 →
      (a) PRAGMA integrity_check が通る、(b) 欠落/重複なし、(c) busy_timeout 超過はサイレント欠落でなく
      エラーで返る、(d) 書込昇格の SQLITE_BUSY 再試行が成立する。

プロセスレベルの部分は `tests/_concurrency_worker.py` を subprocess で起動して実現する
（pytest テストモジュールを spawn で再 import させる罠を避けるため独立スクリプトに分離）。
flock / AF_UNIX は Unix 前提のため Windows ではスキップする。CI は ubuntu / macos の両ランナー必須。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import subaco_hive
from subaco_hive import db as dbmod
from subaco_hive import server, writer

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="flock / AF_UNIX は Unix 前提。CI は ubuntu / macos で実行する。",
)

_WORKER = Path(__file__).resolve().parent / "_concurrency_worker.py"
# サブプロセスから `import subaco_hive` を解決できるようにリポジトリルートを PYTHONPATH へ。
_REPO_ROOT = Path(subaco_hive.__file__).resolve().parent.parent

_INSERT_MSG = (
    "INSERT INTO messages(team, sender, recipient, body, created_at, via)"
    " VALUES('alpha', ?, NULL, 'x', '2026-01-01T00:00:00+00:00', 'cli')"
)


def _env() -> dict[str, str]:
    e = dict(os.environ)
    prev = e.get("PYTHONPATH", "")
    e["PYTHONPATH"] = str(_REPO_ROOT) + (os.pathsep + prev if prev else "")
    return e


def _spawn(*args) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(_WORKER), *map(str, args)],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _mk_hive(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / ".hive"
    root.mkdir(mode=0o700)
    return root, root / "messages.db"


def _init_db(db_path: Path) -> None:
    c = dbmod.init_db(db_path, "alpha", embedding_model="fake", embedding_dim=16)
    c.close()


def _wait_for(pred, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("条件が時間内に成立しませんでした。")


def _kill(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.kill()
    try:
        p.communicate(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - 異常系
        p.terminate()


# ---- (1) N プロセス first-writer-wins（flock 保持者は常に 1 つ）--------------------------------
def test_n_process_first_writer_wins(tmp_path):
    root, _db = _mk_hive(tmp_path)
    start = tmp_path / "start"
    n = 5
    # 各ワーカーは start 出現後に flock を 1 度だけ試み、勝者は hold 秒保持し続ける。
    procs = [_spawn("lock-race", root, _db, start, "2.0") for _ in range(n)]
    time.sleep(0.6)  # 全ワーカーが start 待ちに入る猶予（勝者の保持窓 2.0s 内に全員試行する）
    start.write_text("go")
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        outs.append(out.strip())
    gots = [o for o in outs if o == "GOT"]
    assert len(gots) == 1, f"flock 保持者は常に 1 つのはず: {outs}"


# ---- (1b) 単一ライターが複数プロキシ要求を処理し flock を保持し続ける ---------------------------
def test_single_writer_serves_proxy_and_holds_flock(tmp_path):
    root, db_path = _mk_hive(tmp_path)
    _init_db(db_path)
    ready = tmp_path / "ready"
    w = _spawn("writer-serve", root, db_path, ready)
    try:
        _wait_for(lambda: ready.exists() and writer.socket_path(root).exists(), 20)
        # 親プロセスは flock を取れない（ライターが保持中）。
        assert writer.try_acquire_lock(root) is None
        proxy = writer.ProxyClient(root, timeout=5.0)
        r = proxy.request(
            {"tool": "hive_join", "args": {"name": "alice"}, "session": None, "request_id": "J1"}
        )
        assert r["ok"], r
        r2 = proxy.request(
            {
                "tool": "hive_post",
                "args": {"body": "hi"},
                "session": {"team": "alpha", "name": "alice"},
                "request_id": "P1",
            }
        )
        assert r2["ok"], r2
    finally:
        _kill(w)
    conn = dbmod.open_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM members WHERE name='alice'").fetchone()[0] == 1
    finally:
        conn.close()


# ---- (2) ライター kill → 昇格 → 同一 request_id 再送の非重複----------------------------
def test_writer_kill_promotion_and_idempotent_resend(tmp_path):
    root, db_path = _mk_hive(tmp_path)
    _init_db(db_path)
    ready = tmp_path / "ready"
    w = _spawn("writer-serve", root, db_path, ready)
    main_ep = None
    try:
        _wait_for(lambda: ready.exists() and writer.socket_path(root).exists(), 20)

        def factory():
            conn = dbmod.open_db(db_path)
            ctx = server.Context(
                conn=conn,
                default_team="alpha",
                trusted_agents={},
                store=None,
                memory_enabled=False,
            )
            return ctx, (lambda req: server.dispatch_tool(ctx, req))

        # 親はプロキシ（flock はライターが保持）。EOF→昇格を短いタイムアウトで駆動する。
        main_ep = writer.Endpoint(root, factory, interval=0.05, timeout=3.0)

        # (a) プロキシ転送で join → ライターが alice + J1（processed_requests）を共有 DB にコミット。
        r = main_ep.dispatch(
            {"tool": "hive_join", "args": {"name": "alice"}, "session": None, "request_id": "J1"}
        )
        assert r["ok"], r
        assert not main_ep.is_writer  # まだプロキシ

        # ライターを SIGKILL（flock 解放・stale socket 残置）。
        _kill(w)

        # (b) 同一 request_id J1 の再送 → プロキシ失敗 → 親が昇格 → 台帳突合で二重作成なし。
        r2 = main_ep.dispatch(
            {"tool": "hive_join", "args": {"name": "alice"}, "session": None, "request_id": "J1"}
        )
        assert r2["ok"], r2
        assert main_ep.is_writer, "EOF 検知後にライターへ昇格しているはず（フェイルオーバー）"

        # (c) 昇格後の書き込み継続と再送の非重複。
        post = {
            "tool": "hive_post",
            "args": {"body": "hi"},
            "session": {"team": "alpha", "name": "alice"},
            "request_id": "P1",
        }
        assert main_ep.dispatch(post)["ok"]
        assert main_ep.dispatch(post)["ok"]  # 同一 request_id の再送
    finally:
        if main_ep is not None:
            main_ep.close()
        _kill(w)

    conn = dbmod.open_db(db_path)
    try:
        # 名義は 1 行（フェイルオーバーをまたいでも J1 が二重に members を作らない）。
        assert conn.execute("SELECT COUNT(*) FROM members WHERE name='alice'").fetchone()[0] == 1
        # メッセージは 1 件（P1 再送は非重複）。
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        conn.close()


# ---- (3a) WAL 並行 CLI 直書き: 欠落・重複なし＋integrity_check--------------------
def test_wal_concurrent_cli_inserts_no_loss_or_dup(tmp_path):
    root, db_path = _mk_hive(tmp_path)
    _init_db(db_path)
    n, k = 4, 30
    procs = [_spawn("cli-insert", root, db_path, "alpha", f"w{i}", k) for i in range(n)]
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
    conn = dbmod.open_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == n * k  # 欠落/重複なし
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"  # (a)
        for i in range(n):
            cnt = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE sender=?", (f"w{i}",)
            ).fetchone()[0]
            assert cnt == k
    finally:
        conn.close()


# ---- (3b) message_reads の INSERT OR IGNORE 並行: 重複収束（read_by 再構成の整合前提）-----------
def test_wal_concurrent_reads_insert_or_ignore_dedup(tmp_path):
    root, db_path = _mk_hive(tmp_path)
    _init_db(db_path)
    n, pairs = 4, 40
    procs = [_spawn("reads-insert", root, db_path, pairs) for _ in range(n)]
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
    conn = dbmod.open_db(db_path)
    try:
        # 全ワーカーが同一集合 (0..pairs-1, 'reader') を書く → INSERT OR IGNORE で pairs 行へ収束。
        assert conn.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == pairs
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# ---- (3c) busy_timeout 超過はサイレント欠落でなくエラーで返る------------------------
def test_busy_timeout_exceeded_raises_not_silent(tmp_path):
    root, db_path = _mk_hive(tmp_path)
    _init_db(db_path)
    holder = dbmod.connect(db_path, busy_timeout_ms=5000)
    contender = dbmod.connect(db_path, busy_timeout_ms=0)  # 待たない
    try:
        holder.execute(_INSERT_MSG, ("holder",))  # 書込ロックを取得したまま未 commit で保持
        assert holder.in_transaction
        with pytest.raises(sqlite3.OperationalError):
            # busy_timeout=0 のため待たずに SQLITE_BUSY → 例外（サイレント欠落にしない）。
            contender.execute(_INSERT_MSG, ("contender",))
            contender.commit()
        holder.commit()
    finally:
        holder.close()
        contender.close()
    conn = dbmod.open_db(db_path)
    try:
        # holder の 1 件のみ確定（contender はエラーで弾かれ欠落を隠さない）。
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        conn.close()


# ---- (3d) 書込昇格の SQLITE_BUSY 再試行が busy_timeout 内で成立する------------------
def test_busy_timeout_retry_succeeds(tmp_path):
    root, db_path = _mk_hive(tmp_path)
    _init_db(db_path)
    holder = dbmod.connect(db_path, busy_timeout_ms=5000)
    holder.execute(_INSERT_MSG, ("holder",))  # 書込ロック保持（未 commit）
    result: dict[str, object] = {}

    def contend() -> None:
        conn2 = dbmod.connect(db_path, busy_timeout_ms=3000)  # ロック解放まで待つ
        try:
            conn2.execute(_INSERT_MSG, ("contender",))
            conn2.commit()
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001 - 失敗内容を可視化
            result["err"] = repr(exc)
        finally:
            conn2.close()

    t = threading.Thread(target=contend)
    t.start()
    time.sleep(0.3)
    holder.commit()  # ロック解放 → contender の再試行が成立する
    t.join(timeout=15)
    holder.close()
    assert result.get("ok") is True, result

    conn = dbmod.open_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    finally:
        conn.close()
