"""並行性テスト用のワーカースクリプト（pytest からは subprocess で起動）。

pytest のテストモジュールを multiprocessing の spawn で再 import させる罠を避けるため、
ワーカーは独立した実行可能スクリプトとして分離する（`test_` で始まらないため pytest は収集しない）。
`import subaco_hive` が解決できるよう、呼び出し側は env["PYTHONPATH"] にリポジトリルートを設定する。

使い方:
    python _concurrency_worker.py <action> <hive_root> <db_path> [extra...]

action:
  lock-race  <start_file> <hold_s>   : start_file 出現後に writer.lock の flock を試み、
                                        取得可否を stdout に "GOT"/"MISS" で出す（GOT は hold_s 保持）。
  writer-serve <ready_file>          : Endpoint でライターに昇格し ready_file を書いて serve（kill 待ち）。
  cli-insert <team> <sender> <count> : messages への CLI 直書き（mcp_posts 非記録）を count 回。
  reads-insert <count_pairs>         : message_reads へ INSERT OR IGNORE（全ワーカーが同一集合を書く）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


def _init_if_missing(db_path: Path, team: str) -> None:
    from subaco_hive import db as dbmod

    if not db_path.exists():
        c = dbmod.init_db(db_path, team, embedding_model="fake", embedding_dim=16)
        c.close()


def _build_endpoint(hive_root: Path, db_path: Path, team: str):
    """メッセージング専用（記憶系無効）の Endpoint を組む。埋め込み依存に触れない。"""
    from subaco_hive import db as dbmod
    from subaco_hive import server
    from subaco_hive.writer import Endpoint

    def factory():
        conn = dbmod.open_db(db_path)
        ctx = server.Context(
            conn=conn,
            default_team=team,
            trusted_agents={},
            store=None,
            memory_enabled=False,
        )

        def handler(req):
            return server.dispatch_tool(ctx, req)

        return ctx, handler

    return Endpoint(hive_root, factory)


def _lock_race(hive_root: Path, start_file: Path, hold_s: float) -> None:
    from subaco_hive import writer

    # 全ワーカーの起動をそろえる（先着解放→後着取得の逐次成功を避け、真の同時競合にする）。
    deadline = time.monotonic() + 10.0
    while not start_file.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    fh = writer.try_acquire_lock(hive_root)
    if fh is None:
        print("MISS", flush=True)
        return
    print("GOT", flush=True)
    time.sleep(hold_s)  # 勝者は全員が試行し終えるまで保持し続ける
    writer.release_lock(fh)


def _writer_serve(hive_root: Path, db_path: Path, team: str, ready_file: Path) -> None:
    ep = _build_endpoint(hive_root, db_path, team)
    got = ep.acquire()
    if not got:
        print("NOT_WRITER", flush=True)
        sys.exit(3)
    ready_file.write_text("ready")
    print("WRITER_READY", flush=True)
    ep.serve()  # kill されるまでブロック


def _cli_insert(db_path: Path, team: str, sender: str, count: int) -> None:
    from subaco_hive import db as dbmod

    conn = dbmod.connect(db_path)  # WAL + busy_timeout（既定 5s）で競合吸収
    try:
        for i in range(count):
            conn.execute(
                "INSERT INTO messages(team, sender, recipient, body, created_at, via)"
                " VALUES(?,?,?,?,?,?)",
                (team, sender, None, f"{sender}-{i}", "2026-01-01T00:00:00+00:00", "cli"),
            )
            conn.commit()
    finally:
        conn.close()


def _reads_insert(db_path: Path, count_pairs: int) -> None:
    from subaco_hive import db as dbmod

    conn = dbmod.connect(db_path)
    try:
        for i in range(count_pairs):
            conn.execute(
                "INSERT OR IGNORE INTO message_reads(message_id, member, read_at) VALUES(?,?,?)",
                (i, "reader", "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    action = argv[0]
    hive_root = Path(argv[1])
    db_path = Path(argv[2])
    team = "alpha"

    if action == "lock-race":
        _lock_race(hive_root, Path(argv[3]), float(argv[4]))
    elif action == "writer-serve":
        _init_if_missing(db_path, team)
        _writer_serve(hive_root, db_path, team, Path(argv[3]))
    elif action == "cli-insert":
        _cli_insert(db_path, argv[3], argv[4], int(argv[5]))
    elif action == "reads-insert":
        _reads_insert(db_path, int(argv[3]))
    else:  # pragma: no cover - 呼び出し側の誤用
        print(f"unknown action: {action}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
