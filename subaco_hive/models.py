"""ドメイン型（データモデルに対応する dataclass 群）。

stdlib のみで import 可能。DB 行 <-> Python オブジェクトの受け渡しに用いる。
値の意味づけ（trust 3 値・memory status 2 値・via 2 値・秘密検査 3 値）は
モジュール定数として集約し、DB DDL / 検査ロジックと共有する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# --- trust_level: 昇格は人間の管理操作のみ。v0 の機械判定は全て >=1 -----------------
TRUST_UNTRUSTED = 0  # 未信頼（既定）
TRUST_NORMAL = 1  # 通常
TRUST_HIGH = 2  # 高信頼（v0 では 1 と機械的に同等。運用上の区分）

TrustLevel = Literal[0, 1, 2]

# --- memories.status（二段書き整合）------------------------------------------------
MEMORY_PENDING = "pending"  # memories 挿入済み・Zvec 未 commit
MEMORY_COMMITTED = "committed"  # Zvec insert 完了・recall 対象

MemoryStatus = Literal["pending", "committed"]

# --- messages.via: 自己申告の参考情報。trust 解決には使わない（mcp_posts が正）------
VIA_MCP = "mcp"
VIA_CLI = "cli"

Via = Literal["mcp", "cli"]

# --- 秘密パターン検査の判定（secrets.py と共有）------------------------------------
VERDICT_OK = "ok"  # 検出なし
VERDICT_REDACT = "redact"  # 高エントロピー等 — 赤塗りして書込／読出、警告
VERDICT_REJECT = "reject"  # 確度の高い一致 — 書込拒否（読出時は赤塗り）

SecretVerdict = Literal["ok", "redact", "reject"]


@dataclass(slots=True)
class Member:
    """members 行。token_hash はメンバートークンのハッシュ（名義同一性確認）。"""

    team: str
    name: str
    vendor: str | None
    joined_at: str
    trust_level: int = TRUST_UNTRUSTED
    token_hash: str | None = None
    id: int | None = None


@dataclass(slots=True)
class Message:
    """messages 行。recipient=None はブロードキャスト。"""

    team: str
    sender: str
    body: str
    created_at: str
    recipient: str | None = None  # None はブロードキャスト
    via: str = VIA_CLI  # 参考情報（自己申告）。trust 解決に使わない
    id: int | None = None


@dataclass(slots=True)
class Memory:
    """memories 行。本文とベクタは Zvec 側、trust フィルタの正典はこのテーブル。"""

    id: str  # Zvec Doc id と一致
    team: str
    author: str
    kind: str  # decision / finding / task / convention 等
    created_at: str
    source_trust: int  # 書込時点の members.trust_level
    status: str = MEMORY_PENDING


@dataclass(slots=True)
class MessageRead:
    """message_reads 行。本文既読の正規化記録。"""

    message_id: int
    member: str
    read_at: str


@dataclass(slots=True)
class MessageNotified:
    """message_notified 行。未信頼メタデータの再通知抑止記録。"""

    message_id: int
    member: str
    notified_at: str


@dataclass(slots=True)
class ProcessedRequest:
    """processed_requests 行。冪等キーによる重複実行抑止。"""

    request_id: str
    tool: str
    created_at: str


@dataclass(slots=True)
class AuditEntry:
    """audit 行。args_summary は本文を含めない非本文属性のみ。"""

    team: str
    tool: str
    created_at: str
    member: str | None = None
    args_summary: str | None = None
    id: int | None = None


@dataclass(slots=True)
class SecretFinding:
    """秘密パターン検査の 1 検出（secrets.py）。本文そのものは保持しない（span のみ）。"""

    kind: str  # 例: aws_access_key_id / github_token / high_entropy
    reason_code: str  # audit.args_summary に載せる非本文コード
    confidence: str  # "high"（=reject 相当）/ "entropy"（=redact 相当）
    start: int
    end: int


@dataclass(slots=True)
class ScanResult:
    """秘密パターン検査の結果。判定＋赤塗り本文＋監査用の理由コード。"""

    verdict: str  # VERDICT_OK / VERDICT_REDACT / VERDICT_REJECT
    redacted_text: str
    findings: list[SecretFinding]

    def reason_codes(self) -> list[str]:
        """audit へ記録する非本文の理由コード一覧（重複除去・出現順維持）。"""
        seen: dict[str, None] = {}
        for f in self.findings:
            seen.setdefault(f.reason_code, None)
        return list(seen.keys())
