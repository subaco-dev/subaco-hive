"""SQLite スキーマと接続管理。

- WAL モード＋busy_timeout で CLI 直書き（messages / message_reads）と MCP 経由書込の競合を吸収する。
- 全テーブルを DDL として保持する（members / messages / mcp_posts / message_reads /
  message_notified / processed_requests / memories / hive_meta / audit）。
- 初期スキーマ書込時に hive_meta へ schema_version・active_collection（初期値 `hive_{team}`）・
  embedding_model・embedding_dim を保存する（後から遡及付与できないため v0 初版から必ず書く）。
- 起動（open）時に schema_version を照合する（不一致は fail-closed）。

stdlib sqlite3 のみで動作する（Zvec / 埋め込みには依存しない）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# スキーマ版。段階的変更のたびに増やす。前方マイグレーション runner は将来対応（v0 は照合のみ）。
SCHEMA_VERSION = "1"

# busy_timeout の既定（ミリ秒）。WAL 下での競合を待つ上限。超過は SQLITE_BUSY として呼出側へ返る
# （サイレント欠落にしない。並行性テスト (3)(c)）。
DEFAULT_BUSY_TIMEOUT_MS = 5000

# hive_meta のキー名。
META_SCHEMA_VERSION = "schema_version"
META_ACTIVE_COLLECTION = "active_collection"
META_EMBEDDING_MODEL = "embedding_model"
META_EMBEDDING_DIM = "embedding_dim"


class DbError(Exception):
    """DB 操作の一般エラー。"""


class SchemaError(DbError):
    """スキーマ未初期化・schema_version 不一致（fail-closed で扱う）。"""


# DDL をそのまま採用（冪等な初期化のため IF NOT EXISTS を付す）。
_SCHEMA_SQL = """
-- チームとメンバー（agmsg の team / agent 名と対応）
CREATE TABLE IF NOT EXISTS members (
  id INTEGER PRIMARY KEY,
  team TEXT NOT NULL,
  name TEXT NOT NULL,
  vendor TEXT,                -- claude-code / codex / gemini / human 等
  trust_level INTEGER NOT NULL DEFAULT 0,  -- 0:未信頼 1:通常 2:高信頼（昇格は人間の管理操作のみ）
  token_hash TEXT,            -- メンバートークンのハッシュ（名義の同一性確認）
  joined_at TEXT NOT NULL,
  UNIQUE(team, name)
);

-- メッセージ（agmsg 互換の宛先モデル）
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  team TEXT NOT NULL,
  sender TEXT NOT NULL,
  recipient TEXT,             -- NULL はブロードキャスト
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  via TEXT NOT NULL DEFAULT 'cli'  -- デバッグ用の参考情報（自己申告値。trust 解決には使わない）
);

-- hive-mcp 自身が INSERT したメッセージの台帳（trust 解決の基準）
CREATE TABLE IF NOT EXISTS mcp_posts (
  message_id INTEGER PRIMARY KEY REFERENCES messages(id)
);

-- 既読管理（JSON 配列カラムの read-modify-write による lost update を避けるため正規化）
CREATE TABLE IF NOT EXISTS message_reads (
  message_id INTEGER NOT NULL,
  member TEXT NOT NULL,
  read_at TEXT NOT NULL,
  PRIMARY KEY (message_id, member)
);

-- メタデータ通知の配送記録（未信頼メッセージの再通知抑止）
CREATE TABLE IF NOT EXISTS message_notified (
  message_id INTEGER NOT NULL,
  member TEXT NOT NULL,
  notified_at TEXT NOT NULL,
  PRIMARY KEY (message_id, member)
);

-- 冪等キー（フェイルオーバー時の重複実行抑止）
CREATE TABLE IF NOT EXISTS processed_requests (
  request_id TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- 長期記憶のメタデータ（本文とベクタは Zvec 側。trust フィルタの正典はこのテーブル）
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,        -- Zvec Doc id と一致
  team TEXT NOT NULL,
  author TEXT NOT NULL,
  kind TEXT NOT NULL,         -- decision / finding / task / convention 等
  created_at TEXT NOT NULL,
  source_trust INTEGER NOT NULL,  -- 書込時点の members.trust_level を記録
  status TEXT NOT NULL DEFAULT 'pending'  -- pending / committed（Zvec との二段書きの整合用）
);

-- サーバー構成メタ（埋め込みモデルの整合検査・schema_version・active_collection に使用）
CREATE TABLE IF NOT EXISTS hive_meta (
  key TEXT PRIMARY KEY,       -- 'embedding_model' / 'embedding_dim' / 'active_collection' / 'schema_version' 等
  value TEXT NOT NULL
);

-- 監査ログ（hive-mcp 専有。全 MCP ツール呼び出しと管理操作を記録）
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  team TEXT NOT NULL,
  member TEXT,
  tool TEXT NOT NULL,         -- hive_post / hive_remember / admin:set-trust 等
  args_summary TEXT,          -- 非本文属性のみ（本文は記録しない）
  created_at TEXT NOT NULL
);
"""

# 全テーブル一覧（テスト・ドキュメント・schema 検査で参照）。
TABLES = (
    "members",
    "messages",
    "mcp_posts",
    "message_reads",
    "message_notified",
    "processed_requests",
    "memories",
    "hive_meta",
    "audit",
)

_PathLike = str | Path


def collection_name_for(team: str) -> str:
    """active_collection の初期値。実行時は固定導出せず hive_meta の active_collection を読む。"""
    return f"hive_{team}"


def connect(
    db_path: _PathLike,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """WAL・busy_timeout・外部キーを有効化した接続を返す。

    row_factory=sqlite3.Row でカラム名アクセスを可能にする。DDL/初期化は呼び出し側の責務。
    """
    path = str(db_path)
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    # WAL: 読み取り並行＋単一ライター（Zvec の並行モデルと整合）。
    conn.execute("PRAGMA journal_mode=WAL;")
    # busy_timeout: 競合待ちの上限。超過は SQLITE_BUSY として呼出側へエラーで返る。
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """全テーブルを作成する（冪等）。"""
    conn.executescript(_SCHEMA_SQL)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """hive_meta の値を取得（無ければ None）。"""
    row = conn.execute("SELECT value FROM hive_meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return row["value"] if isinstance(row, sqlite3.Row) else row[0]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """hive_meta の値を upsert する。"""
    conn.execute(
        "INSERT INTO hive_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def _set_meta_if_absent(conn: sqlite3.Connection, key: str, value: str) -> None:
    """hive_meta に未設定のときだけ書く（既存値は温存 — active_collection は reembed が更新するため）。"""
    conn.execute(
        "INSERT INTO hive_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
        (key, value),
    )


def init_db(
    db_path: _PathLike,
    team: str,
    *,
    embedding_model: str,
    embedding_dim: int,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """スキーマを作成し、hive_meta の初期値を書き込んで接続を返す。

    初期スキーマ書込時に必ず書く（後から遡及付与できないため）:
      - schema_version   = SCHEMA_VERSION
      - active_collection = `hive_{team}`（以後 reembed が更新。実行時はこの値を読む）
      - embedding_model  / embedding_dim（起動時照合の基準 — 段階4 で verify）
    既存 DB に対しては未設定のメタのみ補完し、既存値（reembed 後の active_collection 等）は温存する。

    注意: embedding_model / embedding_dim は「記憶系（Zvec）を使う際の照合基準」であり、
    段階1 では値の授受のみを担う。埋め込みプロバイダの実体は段階4で接続する。
    """
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
    create_schema(conn)
    _set_meta_if_absent(conn, META_SCHEMA_VERSION, SCHEMA_VERSION)
    _set_meta_if_absent(conn, META_ACTIVE_COLLECTION, collection_name_for(team))
    _set_meta_if_absent(conn, META_EMBEDDING_MODEL, str(embedding_model))
    _set_meta_if_absent(conn, META_EMBEDDING_DIM, str(int(embedding_dim)))
    conn.commit()
    return conn


def verify_schema_version(conn: sqlite3.Connection) -> None:
    """schema_version を照合し、未初期化・不一致なら SchemaError（fail-closed）。"""
    # hive_meta テーブル自体の存在確認（未初期化 DB を明示エラーに）。
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hive_meta'"
    ).fetchone()
    if exists is None:
        raise SchemaError(
            "hive_meta テーブルがありません（DB が未初期化）。init_db で初期化してください。"
        )
    stored = get_meta(conn, META_SCHEMA_VERSION)
    if stored is None:
        raise SchemaError("schema_version が未記録です（DB が未初期化）。")
    if stored != SCHEMA_VERSION:
        raise SchemaError(
            f"schema_version 不一致: DB={stored!r} != 期待={SCHEMA_VERSION!r}。"
            " 前方マイグレーション runner は未提供（将来対応）。"
        )


def verify_embedding(conn: sqlite3.Connection, *, model: str, dim: int) -> None:
    """埋め込みモデル/次元を照合し、不一致なら SchemaError。

    段階4（記憶系）が起動時に呼ぶ想定。不一致時はメッセージング系を維持したまま
    記憶系ツールのみ拒否する運用のため、呼び出し側はこの例外を捕捉して分岐する。
    モデル切替は環境変数の変更ではなく `hive reembed` で行う。
    """
    stored_model = get_meta(conn, META_EMBEDDING_MODEL)
    stored_dim = get_meta(conn, META_EMBEDDING_DIM)
    if stored_model != str(model) or stored_dim != str(int(dim)):
        raise SchemaError(
            "埋め込み構成が不一致です: "
            f"DB=({stored_model!r},{stored_dim!r}) != 現行=({model!r},{int(dim)!r})。"
            " モデル切替は `hive reembed` を使ってください。"
        )


def open_db(
    db_path: _PathLike,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """既存 DB を開き、schema_version を照合して接続を返す（不一致・未初期化は SchemaError）。

    暗黙初期化はしない（初期化は init_db の責務。「暗黙作成しない」方針）。
    """
    if not Path(str(db_path)).exists():
        raise SchemaError(
            f"DB が存在しません: {db_path}。init_db で初期化してください（暗黙作成しません）。"
        )
    conn = connect(db_path, busy_timeout_ms=busy_timeout_ms)
    verify_schema_version(conn)
    return conn


def active_collection(conn: sqlite3.Connection) -> str:
    """実行時のコレクション名を hive_meta から解決する（固定導出しない）。"""
    name = get_meta(conn, META_ACTIVE_COLLECTION)
    if name is None:
        raise SchemaError("active_collection が未記録です（DB が未初期化）。")
    return name
