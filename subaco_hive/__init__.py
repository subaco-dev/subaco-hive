"""subaco_hive — Subaco メモリプレーンの MCP サーバー＋ops CLI。

段階1（基盤）で提供する、外部依存なし（stdlib のみ）で import・動作する層:
  - config   : HIVE_DB_PATH / HIVE_TEAM / HIVE_LOG_LEVEL の解決と team/name 検証
  - logging  : stderr 専用の診断ログ（stdout は JSON-RPC 専有）
  - db       : SQLite スキーマ（全テーブル）と WAL/busy_timeout 初期化
  - secrets  : 秘密パターン検査
  - models   : Member / Message / Memory 等の型（dataclass）

mcp SDK / zvec / fastembed を要する層（server / cli / 記憶系）は段階2以降で追加し、
いずれも「依存が import できなくてもモジュール自体は import 可能」（遅延 import）を守る。
"""

from __future__ import annotations

from ._version import __version__

__all__ = ["__version__"]
