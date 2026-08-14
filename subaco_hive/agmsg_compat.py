"""agmsg 形エクスポート面（読み取り専用ビュー／エクスポート）。

`messages` 実表を **agmsg の実スキーマの形**で読み出せる読み取り専用ビューとして提供する。
役割は「無改変 agmsg との相互運用面」ではなく **エクスポート面（人間・外部ツール向け）**である
——agmsg spike（06_spike結果）で、無改変 agmsg とは**同一 DB ファイルの共有が不成立**
（agmsg はテーブル名 `messages`・列名をハードコードし、上流は「SQLite ファイルは契約外・
scripts/ が安定面」と明言）と確定した。相互運用が必要になった場合は別ファイル DB＋
scripts 経由ブリッジ（send.sh / api.sh のラッパ）として将来判断する（設計書 §8）。

書き込み可能ビューは INSTEAD OF トリガを要するため採らない。CLI の書き込みは実表
`messages` への直接 INSERT、既読は `message_reads` への INSERT OR IGNORE のみ（別モジュール／runbook 側）。

対応表（agmsg 実スキーマ v1.1.13 ← subaco-hive。正典は 06_spike結果 §3）:
  | agmsg 列    | 由来                                            | 備考 |
  |------------|-------------------------------------------------|------|
  | id         | messages.id                                     | |
  | team       | messages.team                                   | |
  | from_agent | messages.sender                                 | |
  | to_agent   | messages.recipient                              | agmsg は NOT NULL（1 行=1 宛先）。**NULL＝ブロードキャストは hive 拡張**で agmsg には存在しない |
  | body       | messages.body                                   | |
  | created_at | messages.created_at                             | agmsg は秒精度 `Z`、hive は μ秒 `+00:00`（形式差は 06_spike結果 §3） |
  | read_at    | 1:1 行のみ message_reads の当該宛先行から投影   | agmsg の既読は行単位タイムスタンプ。`read_by` は agmsg に**存在しない** |

**`via` 列は投影しない**（via は trust 解決に使わない自己申告値であり agmsg 面には出さない）。
ビュー名は実表 `messages` と衝突しない `agmsg_messages` で確定（06_spike結果 §4.3）。

stdlib sqlite3 のみ（JSON1 拡張にも依存しない）。
"""

from __future__ import annotations

import json
import sqlite3

# 実表 messages と名前衝突しないビュー名（spike で確定——06_spike結果 §4.3）。
AGMSG_VIEW_NAME = "agmsg_messages"

# via を投影しない読み取り専用ビュー。read_at は 1:1 行のみ当該宛先の既読時刻を投影し、
# ブロードキャスト行（to_agent NULL——hive 拡張）は NULL とする。
# DROP → CREATE で定義を常に最新へ移行する（IF NOT EXISTS だけだと旧定義
# 〔sender/recipient/read_by〕の既存ビューが残り続ける——レビュー指摘）。読み取り専用の
# 導出ビューなので drop/recreate に失うものはない。
_VIEW_SQL = f"""
DROP VIEW IF EXISTS {AGMSG_VIEW_NAME};
CREATE VIEW {AGMSG_VIEW_NAME} AS
SELECT
  m.id         AS id,
  m.team       AS team,
  m.sender     AS from_agent,
  m.recipient  AS to_agent,
  m.body       AS body,
  m.created_at AS created_at,
  CASE
    WHEN m.recipient IS NULL THEN NULL
    ELSE (SELECT r.read_at
            FROM message_reads r
           WHERE r.message_id = m.id AND r.member = m.recipient)
  END AS read_at
FROM messages m;
"""


def create_view(conn: sqlite3.Connection) -> None:
    """agmsg 形エクスポートビューを作成する（冪等）。via 列は投影しない。"""
    conn.executescript(_VIEW_SQL)
    conn.commit()


def drop_view(conn: sqlite3.Connection) -> None:
    """エクスポートビューを削除する（スキーマ再構成・テスト用）。"""
    conn.execute(f"DROP VIEW IF EXISTS {AGMSG_VIEW_NAME}")
    conn.commit()


def export_rows(conn: sqlite3.Connection, *, team: str | None = None) -> list[dict]:
    """agmsg 形の行を dict のリストで返す（ビューと同一の投影を Python 側で構成する）。

    鍵はビュー列と同じ（id / team / from_agent / to_agent / body / created_at / read_at）。
    ブロードキャスト行（to_agent None——hive 拡張）の read_at は None。
    将来 agmsg ブリッジを実装する場合、api.sh の `message_sent` イベント形
    （from / to / at 鍵）への変換はブリッジ側で行う（06_spike結果 §4.3）。
    """
    where = ""
    params: tuple = ()
    if team is not None:
        where = "WHERE m.team = ?"
        params = (team,)
    msg_rows = conn.execute(
        f"SELECT m.id, m.team, m.sender, m.recipient, m.body, m.created_at"
        f" FROM messages m {where} ORDER BY m.id",
        params,
    ).fetchall()

    # message_reads を一括取得して (message_id, member) -> read_at に再構成。
    reads: dict[tuple[int, str], str] = {}
    for r in conn.execute("SELECT message_id, member, read_at FROM message_reads"):
        reads[(r["message_id"], r["member"])] = r["read_at"]

    out: list[dict] = []
    for m in msg_rows:
        recipient = m["recipient"]
        out.append(
            {
                "id": m["id"],
                "team": m["team"],
                "from_agent": m["sender"],
                "to_agent": recipient,
                "body": m["body"],
                "created_at": m["created_at"],
                "read_at": None if recipient is None else reads.get((m["id"], recipient)),
            }
        )
    return out


def export_json(conn: sqlite3.Connection, *, team: str | None = None) -> str:
    """export_rows を JSON 文字列で返す。"""
    return json.dumps(export_rows(conn, team=team), ensure_ascii=False)
