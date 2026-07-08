"""メッセージング＆メンバー管理のロジック。

hive_join / hive_post / hive_inbox / hive_members / hive_history と、管理操作
（admin_set_trust / admin_reset_token）の「純ロジック」層。stdlib sqlite3 のみで動作し、
外部依存（mcp SDK / zvec）を持たない — 「stdlib で動く層は依存なしで実行可能」を満たす。

呼び出し方針:
  - 各関数は「既に開かれ schema 照合済みの sqlite3.Connection」と、確定済みの Session（team,name）を受け取る。
  - セッション束縛（未 join のツール呼び出し拒否・プロキシ経由の識別付与）は server / writer 層の責務で、
    本モジュールは「その session が誰か」を引数で受け取るだけ（セッション束縛の分離）。
  - 書き込み操作は request_id を必須とし、processed_requests で冪等化する。単一トランザクションで
    (messages/members/mcp_posts) と processed_requests を確定する（フェイルオーバー後の新ライターも突合可能）。

秘密パターン検査:
  - hive_post（書込）: allowed_for_write。reject は INSERT せず accepted=False で返す。redact は赤塗り本文で INSERT。
  - hive_inbox(include_untrusted) / hive_history（読出）: CLI 直書きが書込検査を迂回するため redact_for_read で赤塗り。

trust 解決: via 列（自己申告・enforce されない）ではなく mcp_posts 台帳を基準にする。
  台帳にある行のみ members 照合で現在 trust を解決し、台帳にない行は via 値に関わらず trust=0・via=cli 表示。
"""

from __future__ import annotations

import hashlib
import secrets as _tokens  # stdlib secrets（乱数トークン）。パッケージ内 secrets とは別（絶対 import）。
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import audit
from .config import validate_name
from .models import (
    TRUST_NORMAL,
    TRUST_UNTRUSTED,
    VIA_CLI,
    VIA_MCP,
    Member,
)
from .secrets import allowed_for_write, redact_for_read


class MessagingError(Exception):
    """メッセージング系の一般エラー（呼び出し側でツールエラー応答に変換する）。"""


class TokenError(MessagingError):
    """メンバートークンの不一致・未提示（join 拒否）。"""


class TeamMismatchError(MessagingError):
    """既存チームと異なる team 名での join（新チームの暗黙作成をしない）。"""


class UnknownRecipientError(MessagingError):
    """recipient 実在検証の失敗（unknown_recipient）。"""


class SquattingRejectedError(TokenError):
    """許可リストにトークン併記された名義への未提示／不一致 join（squatting 封じ）。"""


# ---- Session / 結果型 -------------------------------------------------------------------------
@dataclass(slots=True)
class Session:
    """MCP セッションに束縛された参加者識別（team, name）。sender / 宛先判定に用いる。"""

    team: str
    name: str


@dataclass(slots=True)
class JoinResult:
    """hive_join の結果。token は初回発行／再送再発行時のみ非 None（以後の照合 join は None）。"""

    session: Session
    trust_level: int
    created: bool  # 新規 members 行を作ったか
    reused: bool  # 同一 request_id の再送を検知したか（トークン再発行）
    token: str | None  # クライアントへ返す平文トークン（保存はハッシュのみ）
    args_summary: str  # audit 用（非本文）


@dataclass(slots=True)
class PostResult:
    """hive_post の結果。accepted=False は秘密検査 reject 等で INSERT しなかったケース。"""

    accepted: bool
    message_id: int | None
    verdict: str  # ok / redact / reject（secrets の判定）
    redacted: bool
    reused: bool
    warnings: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    args_summary: str = ""


@dataclass(slots=True)
class InboxEntry:
    """inbox / history の 1 エントリ。delivered=False は「メタデータのみ通知」（未信頼分）。"""

    message_id: int
    header: str
    body: str | None  # None はメタデータのみ通知（本文非配送）
    trust: int
    via: str
    delivered: bool


@dataclass(slots=True)
class InboxResult:
    entries: list[InboxEntry]
    args_summary: str


# ---- 時刻・トークン・ラベルのヘルパー ----------------------------------------------------------
def _now_iso(now: str | None = None) -> str:
    return now if now is not None else datetime.now(UTC).isoformat()


def generate_token() -> str:
    """メンバートークンの平文（URL-safe 乱数）。応答で 1 度だけ返し、保存はハッシュのみ。"""
    return _tokens.token_urlsafe(32)


def hash_token(token: str) -> str:
    """トークンのハッシュ（保存・照合用）。SHA-256 hex。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _random_tag() -> str:
    """本文デリミタ用の乱数タグ（ヘッダ偽装対策）。"""
    return _tokens.token_hex(8)


def wrap_body(body: str) -> str:
    """本文を乱数タグ付きデリミタで囲み、本物のヘッダと機械的に区別可能にする。

    本文中に `[message: …]` / `[memory: …]` 形式を仕込むヘッダ偽装攻撃への防御層。
    タグは呼び出しごとに変わるため、攻撃者は閉じデリミタを事前に埋め込めない。
    """
    tag = _random_tag()
    return f"<<<body:{tag}\n{body}\n{tag}:body>>>"


def _sanitize_label(value: str, *, maxlen: int = 64) -> str:
    """ヘッダに載せる自己申告文字列（未信頼 sender 等）を無害化する。

    ヘッダ構文文字（`]` `[` `=` 改行等）を除去し長さを制限する。CLI 直書きの sender は
    検証を経ないため、自動 inbox 読み上げ経路でヘッダを壊す/偽装するのを防ぐ。
    """
    cleaned = value.replace("\n", " ").replace("\r", " ")
    for ch in ("]", "[", "=", "<", ">"):
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    if len(cleaned) > maxlen:
        cleaned = cleaned[:maxlen] + "…"
    return cleaned or "?"


def message_header(*, sender: str, vendor: str | None, trust: int, via: str, date: str) -> str:
    """`[message: sender=… vendor=… trust=… via=mcp|cli date=…]`（台帳基準の出所ラベル）。"""
    return (
        f"[message: sender={_sanitize_label(sender)} "
        f"vendor={_sanitize_label(vendor or '?')} "
        f"trust={int(trust)} via={via} date={_sanitize_label(date)}]"
    )


# ---- 低レベルの参照ヘルパー --------------------------------------------------------------------
def member_row(conn, team: str, name: str):
    """members 行（sqlite3.Row）を返す。無ければ None。"""
    return conn.execute(
        "SELECT * FROM members WHERE team = ? AND name = ?", (team, name)
    ).fetchone()


def member_exists(conn, team: str, name: str) -> bool:
    return member_row(conn, team, name) is not None


def member_trust(conn, team: str, name: str) -> int:
    """現在の members.trust_level（未登録は 0）。inbox / recall の現在信頼解決に使う。"""
    row = member_row(conn, team, name)
    return int(row["trust_level"]) if row is not None else TRUST_UNTRUSTED


def _distinct_team(conn) -> str | None:
    """members に登録済みのチーム名（v0 は 1 `.hive/`=1 チーム）。無ければ None。"""
    row = conn.execute("SELECT team FROM members LIMIT 1").fetchone()
    return row["team"] if row is not None else None


def _request_processed(conn, request_id: str, tool: str) -> bool:
    """processed_requests に (request_id, tool) が既にあるか（冪等突合）。"""
    row = conn.execute(
        "SELECT tool FROM processed_requests WHERE request_id = ?", (request_id,)
    ).fetchone()
    return row is not None and row["tool"] == tool


# ---- hive_join-----------------------------------------------------------
def hive_join(
    conn,
    *,
    team: str,
    name: str,
    vendor: str | None,
    request_id: str,
    token: str | None = None,
    trusted_agents: Mapping[str, str | None] | None = None,
    now: str | None = None,
) -> JoinResult:
    """コミュニティ参加（members 登録＋セッション束縛）。冪等・トークン・許可リストを扱う。

    引数:
      - team / name : 文字集合検証を適用（NFC・`[a-z0-9_-]`・64 字）。導出はしない。
      - request_id  : 冪等キー（必須）。members 挿入・token_hash・processed_requests を単一 tx。
      - token       : 既存名義への再接続・許可リストのトークン併記名義で必須の照合トークン（平文）。
      - trusted_agents : name -> 事前共有トークンのハッシュ（None=トークン非併記）。members 新規作成時のみ
                         trust=1 を付与（リスト外は trust=0）。既存メンバーの trust は join では変えない。

    冪等・トークンの意味論:
      - 同一 request_id の再送検知 → トークンを再発行し token_hash を更新して新トークンを返す
        （応答喪失による名義ロックアウトを防ぐ。request_id を保持するのは発行先プロキシのみ）。
      - 既存名義への新規 request での join → members.token_hash と照合。不一致／未提示は拒否（再束縛しない）。
      - 新規名義 + 許可リストにトークン併記 → 事前共有トークンの照合必須。不一致／未提示は members 行を作らず拒否
        （squatting 封じ）。照合成功時は token_hash=事前共有トークンのハッシュとし、平文は返さない。
      - 新規名義 + トークン非併記（リスト内/外いずれも）→ 乱数トークンを発行して返す。
    """
    team = validate_name(team, kind="team")
    name = validate_name(name, kind="name")
    ts = _now_iso(now)
    session = Session(team=team, name=name)
    trusted_agents = trusted_agents or {}

    # 異名 team の拒否（新チームの暗黙作成をしない）。
    existing_team = _distinct_team(conn)
    if existing_team is not None and existing_team != team:
        raise TeamMismatchError(
            f"既存チーム {existing_team!r} と異なる team {team!r} での join は拒否します"
            "（v0 は 1 `.hive/`=1 チーム。`.hive/` リセットが唯一のチーム切替）。"
        )

    with conn:  # 単一トランザクション
        # (A) 再送検知: 同一 request_id 済み → トークン再発行。
        if _request_processed(conn, request_id, "hive_join"):
            row = member_row(conn, team, name)
            new_token = generate_token()
            if row is not None:
                conn.execute(
                    "UPDATE members SET token_hash = ? WHERE team = ? AND name = ?",
                    (hash_token(new_token), team, name),
                )
                trust = int(row["trust_level"])
            else:
                # 再送だが行が消えている（孤児掃除等）。防御的に作り直す。
                trust = TRUST_NORMAL if name in trusted_agents else TRUST_UNTRUSTED
                conn.execute(
                    "INSERT INTO members(team, name, vendor, trust_level, token_hash, joined_at)"
                    " VALUES(?, ?, ?, ?, ?, ?)",
                    (team, name, vendor, trust, hash_token(new_token), ts),
                )
            summary = audit.build_summary(
                tool="hive_join", name=name, vendor=vendor, trust=trust, reused=True
            )
            return JoinResult(
                session, trust, created=False, reused=True, token=new_token, args_summary=summary
            )

        row = member_row(conn, team, name)

        # (B) 既存名義への新規 request（再接続）: トークン照合必須。
        if row is not None:
            stored = row["token_hash"]
            if stored is not None and (not token or hash_token(token) != stored):
                raise TokenError(
                    f"名義 {name!r} は登録済みです。正しいメンバートークンの提示が必要です"
                    "（不一致・未提示は再接続を拒否。復旧は `hive admin reset-token`）。"
                )
            # token_hash が None の異常行は復旧的に新トークンを発行（通常発生しない）。
            issued: str | None = None
            if stored is None:
                issued = generate_token()
                conn.execute(
                    "UPDATE members SET token_hash = ? WHERE team = ? AND name = ?",
                    (hash_token(issued), team, name),
                )
            trust = int(row["trust_level"])  # 再 join で trust は変えない
            conn.execute(
                "INSERT INTO processed_requests(request_id, tool, created_at) VALUES(?, ?, ?)",
                (request_id, "hive_join", ts),
            )
            summary = audit.build_summary(
                tool="hive_join", name=name, vendor=vendor, trust=trust, reconnect=True
            )
            return JoinResult(
                session, trust, created=False, reused=False, token=issued, args_summary=summary
            )

        # (C) 新規名義の作成。許可リストで trust とトークン要件を決める。
        listed = name in trusted_agents
        preshared_hash = trusted_agents.get(name) if listed else None
        trust = TRUST_NORMAL if listed else TRUST_UNTRUSTED
        returned_token: str | None
        if preshared_hash is not None:
            # トークン併記名義: 事前共有トークンの照合必須。不一致／未提示は行を作らず拒否（squatting 封じ）。
            if not token or hash_token(token) != preshared_hash:
                raise SquattingRejectedError(
                    f"許可リストで {name!r} に事前共有トークンが設定されています。"
                    "初回 join でも照合が必要で、未提示・不一致は members 行を作らず拒否します。"
                )
            token_hash = preshared_hash  # 事前共有トークンをそのままメンバートークンにする
            returned_token = None  # クライアントは既に保持しているため返さない
        else:
            issued = generate_token()
            token_hash = hash_token(issued)
            returned_token = issued
        conn.execute(
            "INSERT INTO members(team, name, vendor, trust_level, token_hash, joined_at)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (team, name, vendor, trust, token_hash, ts),
        )
        conn.execute(
            "INSERT INTO processed_requests(request_id, tool, created_at) VALUES(?, ?, ?)",
            (request_id, "hive_join", ts),
        )
        summary = audit.build_summary(
            tool="hive_join", name=name, vendor=vendor, trust=trust, listed=listed, created=True
        )
        return JoinResult(
            session, trust, created=True, reused=False, token=returned_token, args_summary=summary
        )


# ---- hive_post------------------------------------------------------------------
def hive_post(
    conn,
    session: Session,
    *,
    body: str,
    request_id: str,
    recipient: str | None = None,
    via: str = VIA_MCP,
    now: str | None = None,
) -> PostResult:
    """メッセージ送信。recipient 省略でブロードキャスト、指定時は実在検証あり。秘密検査あり。

    単一トランザクションで messages INSERT・mcp_posts 記録・processed_requests 記録を確定する。
    冪等: 同一 request_id の再送は重複 INSERT しない（v0 は processed_requests に message_id を持たないため
    再送応答での message_id 復元は行わない — TODO）。
    """
    ts = _now_iso(now)

    # 冪等: 再送はスキップ（重複 INSERT 抑止）。
    if _request_processed(conn, request_id, "hive_post"):
        summary = audit.build_summary(tool="hive_post", recipient=recipient, reused=True)
        return PostResult(
            accepted=True,
            message_id=None,
            verdict="ok",
            redacted=False,
            reused=True,
            args_summary=summary,
        )

    # recipient 実在検証（unknown_recipient）。INSERT 前に弾く。
    if recipient is not None and not member_exists(conn, session.team, recipient):
        raise UnknownRecipientError(
            f"recipient {recipient!r} は team {session.team!r} に未登録です（unknown_recipient）。"
            " 打ち間違い・未 join 名義宛 DM のサイレント喪失を防ぐため INSERT しません。"
        )

    # 秘密パターン検査（書込ポリシー）。reject は INSERT しない。
    allowed, scan = allowed_for_write(body)
    reason_codes = scan.reason_codes()
    if not allowed:
        summary = audit.build_summary(
            tool="hive_post",
            recipient=recipient,
            verdict=scan.verdict,
            reason_codes=reason_codes,
            accepted=False,
        )
        return PostResult(
            accepted=False,
            message_id=None,
            verdict=scan.verdict,
            redacted=False,
            reused=False,
            warnings=["秘密パターン検査により書き込みを拒否しました。"],
            reason_codes=reason_codes,
            args_summary=summary,
        )

    stored_body = scan.redacted_text  # ok は原文と同一、redact は赤塗り本文
    redacted = scan.verdict != "ok"
    warnings: list[str] = []
    if redacted:
        warnings.append("高エントロピー箇所を赤塗りして書き込みました。")

    with conn:  # 単一トランザクション
        cur = conn.execute(
            "INSERT INTO messages(team, sender, recipient, body, created_at, via)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (session.team, session.name, recipient, stored_body, ts, via),
        )
        message_id = int(cur.lastrowid)
        # hive-mcp 自身が INSERT した台帳（trust 解決の基準）。
        conn.execute("INSERT INTO mcp_posts(message_id) VALUES(?)", (message_id,))
        conn.execute(
            "INSERT INTO processed_requests(request_id, tool, created_at) VALUES(?, ?, ?)",
            (request_id, "hive_post", ts),
        )

    summary = audit.build_summary(
        tool="hive_post",
        recipient=recipient,
        body_len=len(body),
        via=via,
        verdict=scan.verdict,
        reason_codes=reason_codes,
    )
    return PostResult(
        accepted=True,
        message_id=message_id,
        verdict=scan.verdict,
        redacted=redacted,
        reused=False,
        warnings=warnings,
        reason_codes=reason_codes,
        args_summary=summary,
    )


# ---- inbox / history 共通の候補走査 ------------------------------------------------------------
def _candidate_messages(conn, session: Session, *, since: str | None, broadcast_only: bool):
    """宛先述語で messages を走査し、台帳フラグ・送信者現在 trust を付与して返す。

    宛先述語: team 一致 かつ (recipient=自名 OR recipient IS NULL) かつ sender≠自名。
    broadcast_only=True（history）は recipient IS NULL のみに絞る。
    trust は「台帳(mcp_posts)にある行のみ members 照合、台帳外は 0・via=cli」。
    """
    sql = [
        "SELECT m.id AS id, m.sender AS sender, m.recipient AS recipient, m.body AS body,",
        "       m.created_at AS created_at, m.via AS via,",
        "       (mp.message_id IS NOT NULL) AS in_ledger,",
        "       COALESCE(mem.trust_level, 0) AS sender_trust,",
        "       mem.vendor AS sender_vendor",
        "FROM messages m",
        "LEFT JOIN mcp_posts mp ON mp.message_id = m.id",
        "LEFT JOIN members mem ON mem.team = m.team AND mem.name = m.sender",
        "WHERE m.team = ? AND m.sender != ?",
    ]
    params: list[object] = [session.team, session.name]
    if broadcast_only:
        sql.append("AND m.recipient IS NULL")
    else:
        sql.append("AND (m.recipient = ? OR m.recipient IS NULL)")
        params.append(session.name)
    if since:
        sql.append("AND m.created_at > ?")
        params.append(since)
    sql.append("ORDER BY m.id")
    return conn.execute("\n".join(sql), params).fetchall()


def _resolve_trust_via(row) -> tuple[int, str]:
    """行の (配送 trust, 表示 via) を返す。台帳外は trust=0・via=cli 固定。"""
    if row["in_ledger"]:
        return int(row["sender_trust"]), VIA_MCP
    return TRUST_UNTRUSTED, VIA_CLI


def _entry_from_row(row, *, delivered: bool, redact: bool) -> InboxEntry:
    """行から InboxEntry を組む。delivered=False は本文非配送（メタデータのみ）。"""
    trust, via = _resolve_trust_via(row)
    header = message_header(
        sender=row["sender"],
        vendor=row["sender_vendor"],
        trust=trust,
        via=via,
        date=row["created_at"],
    )
    if not delivered:
        return InboxEntry(row["id"], header, None, trust, via, delivered=False)
    body = row["body"]
    if redact:  # 読出時赤塗り（CLI 直書きの書込検査迂回に対応）
        body = redact_for_read(body).redacted_text
    return InboxEntry(row["id"], header, body, trust, via, delivered=True)


# ---- hive_inbox------------------------------------------------------------------------
def hive_inbox(
    conn,
    session: Session,
    *,
    since: str | None = None,
    include_untrusted: bool = False,
    now: str | None = None,
) -> InboxResult:
    """未読取得。既定は trust>=1 の本文＋未信頼分のメタデータ一度きり通知。

    - trust>=1（台帳あり かつ 送信者現在 trust>=1）: 本文を返し message_reads に記録（既読）。
    - trust=0 / cli: 既定はメタデータのみ一度だけ通知して message_notified に記録（本文非配送）。
    - include_untrusted=True: 未信頼本文も返し（読出時赤塗り）message_reads に記録。
    既読・通知記録は返却と同一トランザクションで行う。
    """
    ts = _now_iso(now)
    rows = _candidate_messages(conn, session, since=since, broadcast_only=False)

    read_ids = {
        r["message_id"]
        for r in conn.execute(
            "SELECT message_id FROM message_reads WHERE member = ?", (session.name,)
        )
    }
    notified_ids = {
        r["message_id"]
        for r in conn.execute(
            "SELECT message_id FROM message_notified WHERE member = ?", (session.name,)
        )
    }

    entries: list[InboxEntry] = []
    with conn:  # 返却と既読/通知記録を同一 tx
        for row in rows:
            mid = row["id"]
            if mid in read_ids:
                continue  # 本文既読は再配送しない
            trust, _via = _resolve_trust_via(row)
            trusted = trust >= TRUST_NORMAL

            if trusted:
                entries.append(_entry_from_row(row, delivered=True, redact=False))
                conn.execute(
                    "INSERT OR IGNORE INTO message_reads(message_id, member, read_at)"
                    " VALUES(?, ?, ?)",
                    (mid, session.name, ts),
                )
            elif include_untrusted:
                # 明示取得: 未信頼本文を赤塗りして返し、既読化。
                entries.append(_entry_from_row(row, delivered=True, redact=True))
                conn.execute(
                    "INSERT OR IGNORE INTO message_reads(message_id, member, read_at)"
                    " VALUES(?, ?, ?)",
                    (mid, session.name, ts),
                )
            else:
                if mid in notified_ids:
                    continue  # 未信頼メタデータは一度きり（再通知抑止）
                entries.append(_entry_from_row(row, delivered=False, redact=False))
                conn.execute(
                    "INSERT OR IGNORE INTO message_notified(message_id, member, notified_at)"
                    " VALUES(?, ?, ?)",
                    (mid, session.name, ts),
                )

    summary = audit.build_summary(
        tool="hive_inbox", count=len(entries), include_untrusted=include_untrusted, since=since
    )
    return InboxResult(entries=entries, args_summary=summary)


# ---- hive_history----------------------------------------------------------------------
def hive_history(
    conn,
    session: Session,
    *,
    limit: int = 50,
) -> InboxResult:
    """ルーム履歴の再生（文脈シード用）。ブロードキャストのみ・inbox と同じラベリング／読出赤塗り。

    既読/通知の記録はしない（再生であり配送ではない）。未信頼本文は読出時赤塗りで返す。
    直近 `limit` 件を時系列（古い→新しい）で返す。
    """
    rows = _candidate_messages(conn, session, since=None, broadcast_only=True)
    if limit is not None and limit >= 0:
        rows = rows[-limit:] if limit else []
    entries: list[InboxEntry] = []
    for row in rows:
        trust, _via = _resolve_trust_via(row)
        # 履歴は再生のため本文を出すが、未信頼分は読出時赤塗り（trusted は書込検査済み）。
        entries.append(_entry_from_row(row, delivered=True, redact=trust < TRUST_NORMAL))
    summary = audit.build_summary(tool="hive_history", limit=limit, count=len(entries))
    return InboxResult(entries=entries, args_summary=summary)


# ---- hive_members----------------------------------------------------------------------
def hive_members(conn, session: Session) -> list[Member]:
    """登録メンバー一覧（name / vendor / trust_level / joined_at）。CLI 参加者は現れない。"""
    rows = conn.execute(
        "SELECT * FROM members WHERE team = ? ORDER BY joined_at, name", (session.team,)
    ).fetchall()
    return [
        Member(
            team=r["team"],
            name=r["name"],
            vendor=r["vendor"],
            joined_at=r["joined_at"],
            trust_level=int(r["trust_level"]),
            token_hash=r["token_hash"],
            id=r["id"],
        )
        for r in rows
    ]


# ---- 管理操作（CLI 一時ライター／server 管理チャネルの双方から呼ぶ）------------------------
def admin_set_trust(
    conn,
    *,
    team: str,
    name: str,
    level: int,
    restamp: bool = False,
    now: str | None = None,
) -> dict:
    """trust_level を人間の管理操作で設定する（昇格・降格）。

    再 join では巻き戻らない（trust の変更手段はこれのみ）。--restamp 指定時は当該著者の
    memories.source_trust を新値へ更新する（未信頼期間の記憶を recall 可視化。人間の明示判断に限る）。
    Zvec スカラー属性の同期は memory 層の責務（TODO: reembed 相当の再挿入 — Zvec spike）。
    戻り値は audit 用の非本文サマリ dict。
    """
    validate_name(team, kind="team")
    validate_name(name, kind="name")
    level = int(level)
    with conn:
        cur = conn.execute(
            "UPDATE members SET trust_level = ? WHERE team = ? AND name = ?", (level, team, name)
        )
        changed = cur.rowcount
        restamped = 0
        if restamp:
            rc = conn.execute(
                "UPDATE memories SET source_trust = ? WHERE team = ? AND author = ?",
                (level, team, name),
            )
            restamped = rc.rowcount
    return {
        "tool": "admin:set-trust",
        "name": name,
        "level": level,
        "changed": changed,
        "restamp": restamp,
        "restamped": restamped,
    }


def admin_reset_token(conn, *, team: str, name: str, now: str | None = None) -> tuple[str, dict]:
    """紛失時のメンバートークン再発行。新しい平文トークンと audit サマリを返す。"""
    validate_name(team, kind="team")
    validate_name(name, kind="name")
    if not member_exists(conn, team, name):
        raise MessagingError(
            f"名義 {name!r}（team {team!r}）は未登録のためトークン再発行できません。"
        )
    new_token = generate_token()
    with conn:
        conn.execute(
            "UPDATE members SET token_hash = ? WHERE team = ? AND name = ?",
            (hash_token(new_token), team, name),
        )
    return new_token, {"tool": "admin:reset-token", "name": name}
