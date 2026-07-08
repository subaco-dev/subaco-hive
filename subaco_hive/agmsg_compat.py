"""agmsg 互換の読み取り専用ビュー／エクスポート。

`messages` 実表を agmsg のルーム概念へ読み替え可能な**読み取り専用ビュー**として提供する。
書き込み可能ビューは INSTEAD OF トリガを要するため採らない。CLI の書き込みは実表
`messages` への直接 INSERT、既読は `message_reads` への INSERT OR IGNORE のみ（別モジュール／runbook 側）。

対応表（agmsg 期待スキーマ ← subaco-hive）:
  | agmsg 列   | 由来                                             | 備考 |
  |-----------|--------------------------------------------------|------|
  | id        | messages.id                                      | |
  | team      | messages.team                                    | agmsg の room 相当 |
  | sender    | messages.sender                                  | |
  | recipient | messages.recipient（NULL=ブロードキャスト）      | |
  | body      | messages.body                                    | |
  | created_at| messages.created_at                              | |
  | read_by   | message_reads を json_group_array で再構成       | JSON 配列文字列。正規化テーブルから復元 |

**`via` 列は投影しない**（via は trust 解決に使わない自己申告値であり agmsg 面には出さない）。
ビュー名は実表 `messages` と衝突しないよう `agmsg_messages` とする（別名または別ファイル DB）。
無改変 agmsg バイナリとの相互運用可否・最終的な名称／配置は agmsg spike の結果に従う（TODO）。

stdlib sqlite3 のみ。json_group_array（JSON1）が無い環境向けに Python 側再構成の export も持つ。
"""

from __future__ import annotations

import json
import sqlite3

# 実表 messages と名前衝突しないビュー名。
AGMSG_VIEW_NAME = "agmsg_messages"

# via を投影しない読み取り専用ビュー。read_by は message_reads から復元。
_VIEW_SQL = f"""
CREATE VIEW IF NOT EXISTS {AGMSG_VIEW_NAME} AS
SELECT
  m.id         AS id,
  m.team       AS team,
  m.sender     AS sender,
  m.recipient  AS recipient,
  m.body       AS body,
  m.created_at AS created_at,
  COALESCE(
    (SELECT json_group_array(r.member)
       FROM message_reads r
      WHERE r.message_id = m.id),
    json_array()
  ) AS read_by
FROM messages m;
"""


def create_view(conn: sqlite3.Connection) -> None:
    """agmsg 互換ビューを作成する（冪等）。via 列は投影しない。"""
    conn.executescript(_VIEW_SQL)
    conn.commit()


def drop_view(conn: sqlite3.Connection) -> None:
    """互換ビューを削除する（スキーマ再構成・テスト用）。"""
    conn.execute(f"DROP VIEW IF EXISTS {AGMSG_VIEW_NAME}")
    conn.commit()


def export_rows(conn: sqlite3.Connection, *, team: str | None = None) -> list[dict]:
    """agmsg 互換の行を dict のリストで返す（read_by は Python 側で再構成）。

    JSON1 拡張の有無に依存しない堅牢経路。ビュー（json_group_array）が使えない環境でも
    同じ形（via 非投影・read_by は member のリスト）を返す。read_by は member 名の list[str]。
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

    # message_reads を一括取得して message_id -> [member,...] に再構成。
    reads: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT message_id, member FROM message_reads ORDER BY message_id, member"
    ):
        reads.setdefault(r["message_id"], []).append(r["member"])

    out: list[dict] = []
    for m in msg_rows:
        out.append(
            {
                "id": m["id"],
                "team": m["team"],
                "sender": m["sender"],
                "recipient": m["recipient"],
                "body": m["body"],
                "created_at": m["created_at"],
                "read_by": reads.get(m["id"], []),
            }
        )
    return out


def export_json(conn: sqlite3.Connection, *, team: str | None = None) -> str:
    """export_rows を JSON 文字列で返す（read_by は JSON 配列）。"""
    return json.dumps(export_rows(conn, team=team), ensure_ascii=False)
