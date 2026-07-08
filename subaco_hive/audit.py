"""監査ログ（「監査ログの記録範囲」）。

**最重要の不変条件: audit.args_summary には本文（body / text）を一切含めない。**
記録するのはツール名・recipient・kind・本文長・秘密パターン検査の理由コード等の
「非本文属性」のみ（検査が拒否した資格情報が audit 経由で messages.db に残存するのを防ぐ）。

audit テーブルは hive-mcp 専有（CLI 経路境界を維持）。全 MCP ツール呼び出しと
管理操作（admin:set-trust 等）が本モジュールで記録される。stdlib sqlite3 のみで動作する。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

# args_summary に絶対に載せない本文系キー（防御的ガード。呼び出し側の事故を機械的に弾く）。
_FORBIDDEN_KEYS = frozenset({"body", "text", "content", "message", "token", "redacted_text"})


def _now_iso(now: str | None = None) -> str:
    """UTC ISO8601 タイムスタンプ（テストのため注入可能）。"""
    return now if now is not None else datetime.now(UTC).isoformat()


def build_summary(**fields: Any) -> str:
    """非本文属性から audit.args_summary 用の JSON 文字列を組み立てる。

    - None 値は落とす（ノイズ削減）。
    - 本文系キー（body/text/content/...）が渡された場合は ValueError で弾く
      （「本文を記録しない」不変条件をコードで保証する）。
    - 値は json でシリアライズ可能なものに限る（呼び出し側が非本文の要約だけを渡す前提）。
    """
    payload: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _FORBIDDEN_KEYS:
            raise ValueError(
                f"audit.args_summary に本文系キー {key!r} は載せられません。本文長等の要約を渡してください。"
            )
        if value is None:
            continue
        payload[key] = value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def record(
    conn: sqlite3.Connection,
    *,
    team: str,
    tool: str,
    member: str | None = None,
    args_summary: str | None = None,
    now: str | None = None,
) -> int:
    """audit へ 1 行記録して id を返す。

    args_summary は build_summary が生成した非本文 JSON、または既に非本文と分かっている文字列。
    呼び出しの成否にかかわらず記録する想定（全ツール呼び出し・管理操作を追跡）。
    独立したコミットで確定する（ツール本体の tx とは分離。監査は本体失敗時も残せるようにする）。
    """
    cur = conn.execute(
        "INSERT INTO audit(team, member, tool, args_summary, created_at) VALUES(?, ?, ?, ?, ?)",
        (team, member, tool, args_summary, _now_iso(now)),
    )
    conn.commit()
    rowid = cur.lastrowid
    return int(rowid) if rowid is not None else -1
