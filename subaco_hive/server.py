"""MCP サーバー（公式 Python SDK・stdio）とツール登録。

**`import subaco_hive` は mcp SDK 無しでも成功する必要がある**。そのため mcp の import は
`main()`／`run_stdio()` の中でのみ行う（本モジュール top-level では import しない）。ツールの実処理は
`dispatch_tool()`（純関数・stdlib のみ）に集約し、外部依存なしで単体テスト可能にする。

責務:
  - 全ツール呼び出しで audit 記録（args_summary は非本文）。
  - 書込時 secret 検査は messaging.hive_post / memory.hive_remember が内包（reject は書かない）。
  - 読出時赤塗りは hive_inbox(include_untrusted)/hive_history が内包。
  - first-writer-wins: writer.Endpoint 経由で「ライター 1 つ・他はプロキシ転送」を担保する。

登録ツール: hive_join / hive_post / hive_inbox / hive_members / hive_history /
hive_remember / hive_recall / hive_stats。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import audit, config, db, embedding, messaging
from .logging import get_logger, setup_logging
from .memory import MemoryStore
from .messaging import Session

_log = get_logger(__name__)

# 書込系ツール（request_id を付与して冪等化する）。
_WRITE_TOOLS = frozenset({"hive_join", "hive_post", "hive_remember"})


# ---- 許可リスト（trusted_agents。リポジトリ外・エージェント書換不能）-----------
def trusted_agents_path(team: str) -> Path:
    """`~/.config/subaco/<team>/trusted_agents`。"""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "subaco" / team / "trusted_agents"


def load_trusted_agents(team: str) -> dict[str, str | None]:
    """許可リストを読む。各行 `name` または `name <token_hash>`。`#` 以降はコメント。

    返り値は name -> 事前共有トークンのハッシュ（トークン非併記は None）。ファイル不在は空 dict。
    bootstrap はこのファイルを生成しない（作成手順の案内のみ）。
    """
    path = trusted_agents_path(team)
    result: dict[str, str | None] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        name = parts[0]
        result[name] = parts[1] if len(parts) > 1 else None
    return result


# ---- サーバーコンテキスト（ライター資源）------------------------------------------------------
@dataclass
class Context:
    """ライタープロセスが保持する資源。dispatch_tool はこれと要求 dict だけで動く（テスト可能）。"""

    conn: sqlite3.Connection
    default_team: str
    trusted_agents: dict[str, str | None]
    store: MemoryStore | None
    memory_enabled: bool
    memory_error: str | None = None


def _open_writer_conn(db_path: Path) -> sqlite3.Connection:
    """ライター用接続を開く。socket 受付スレッドと MCP スレッドから触るため check_same_thread=False。

    db.connect と同じ PRAGMA（WAL/busy_timeout/FK）を適用する。アクセスは dispatch のロックで直列化する。
    """
    conn = sqlite3.connect(
        str(db_path), timeout=db.DEFAULT_BUSY_TIMEOUT_MS / 1000.0, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(f"PRAGMA busy_timeout={db.DEFAULT_BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def build_context(db_path: Path, team: str) -> Context:
    """ライター資源を構築する。DB 未初期化なら init_db、既存なら open して schema 照合する。

    埋め込み構成を照合し、不一致・依存未導入なら記憶系のみ無効化する（メッセージング系は維持）。
    """
    provider = embedding.build_provider()
    memory_enabled = True
    memory_error: str | None = None
    store: MemoryStore | None = None

    if not db_path.exists():
        # 初回: 埋め込みモデル名/次元を確定して init（dim 解決に依存が要る場合は既知表で回避 — embedding.py）。
        try:
            model, dim = provider.model_name, provider.dim
        except Exception as exc:  # noqa: BLE001 - 依存未導入等
            model, dim, memory_enabled, memory_error = "unconfigured", 0, False, str(exc)
        conn = db.init_db(db_path, team, embedding_model=model, embedding_dim=dim)
        conn.close()

    conn = _open_writer_conn(db_path)
    db.verify_schema_version(conn)

    if memory_enabled:
        try:
            embedding.verify(conn, provider)  # 不一致は db.SchemaError（記憶系のみ拒否）
            store = MemoryStore(conn, provider)
            store.cleanup_orphans()  # 昇格時の孤児掃除。プロキシ再送受付の前に完了。
        except db.SchemaError as exc:
            memory_enabled, memory_error = False, str(exc)
        except Exception as exc:  # noqa: BLE001 - zvec 未導入等
            memory_enabled, memory_error = False, str(exc)

    return Context(
        conn=conn,
        default_team=team,
        trusted_agents=load_trusted_agents(team),
        store=store,
        memory_enabled=memory_enabled,
        memory_error=memory_error,
    )


# ---- 応答レンダリング（プロキシ/ライターで一貫させる）------------------------------------------
def _render_entries(entries) -> str:
    """inbox/history エントリをテキスト化（ヘッダ＋乱数タグ本文。未信頼はメタデータのみ）。"""
    lines: list[str] = []
    for e in entries:
        if e.body is None:
            lines.append(e.header + "  (本文非配送: 未信頼。include_untrusted で取得可)")
        else:
            lines.append(e.header + "\n" + e.body)
    return "\n\n".join(lines) if lines else "(新着なし)"


def _render_recall(entries) -> str:
    if not entries:
        return "(該当する記憶なし)"
    return "\n\n".join(f"{e.header}\n{e.body}" for e in entries)


# ---- 中核ディスパッチャ（純関数・stdlib のみ）-------------------------------------------------
def dispatch_tool(ctx: Context, req: dict[str, Any]) -> dict[str, Any]:
    """要求 dict を処理して結果 dict を返す（ライタープロセス内で実行）。全ツールで audit 記録。

    req = {"tool": str, "args": {..}, "session": {"team":..,"name":..} | None, "request_id": str}
    例外（TokenError/UnknownRecipientError 等）は上位（Endpoint/WriterServer）がワイヤのエラーに変換する。
    """
    tool = req["tool"]
    args = req.get("args") or {}
    sess_dict = req.get("session")
    request_id = req.get("request_id") or uuid.uuid4().hex
    session = Session(**sess_dict) if sess_dict else None
    member = session.name if session else None
    conn = ctx.conn

    def _audit(summary: str, team: str) -> None:
        try:
            audit.record(conn, team=team, tool=tool, member=member, args_summary=summary)
        except Exception as exc:  # noqa: BLE001 - 監査失敗は本処理を妨げない
            _log.warning("audit 記録に失敗: %s", exc)

    if tool == "hive_join":
        team = args.get("team") or ctx.default_team
        res = messaging.hive_join(
            conn,
            team=team,
            name=args["name"],
            vendor=args.get("vendor"),
            request_id=request_id,
            token=args.get("token"),
            trusted_agents=ctx.trusted_agents,
        )
        _audit(res.args_summary, team)
        return {
            "session": {"team": res.session.team, "name": res.session.name},
            "trust_level": res.trust_level,
            "token": res.token,
            "created": res.created,
            "reused": res.reused,
            "text": (
                f"joined team={res.session.team} name={res.session.name} "
                f"trust={res.trust_level}"
                + (" (token 発行: 安全に保管してください)" if res.token else "")
            ),
        }

    if session is None and not tool.startswith("admin:"):
        # join 以外のツールは未 join を拒否（セッション束縛）。管理チャネル（admin:*）は session 不要。
        raise messaging.MessagingError("未 join です。先に hive_join を呼んでください。")

    if tool == "hive_post":
        res = messaging.hive_post(
            conn,
            session,
            body=args["body"],
            request_id=request_id,
            recipient=args.get("recipient"),
        )
        _audit(res.args_summary, session.team)
        return {
            "accepted": res.accepted,
            "message_id": res.message_id,
            "verdict": res.verdict,
            "redacted": res.redacted,
            "reused": res.reused,
            "warnings": res.warnings,
            "text": ("投稿しました。" if res.accepted else "秘密パターン検査により拒否しました。")
            + ("".join(f" [警告] {w}" for w in res.warnings)),
        }

    if tool == "hive_inbox":
        res = messaging.hive_inbox(
            conn,
            session,
            since=args.get("since"),
            include_untrusted=bool(args.get("include_untrusted", False)),
        )
        _audit(res.args_summary, session.team)
        return {
            "count": len(res.entries),
            "entries": [
                {
                    "message_id": e.message_id,
                    "trust": e.trust,
                    "via": e.via,
                    "delivered": e.delivered,
                    "header": e.header,
                    "body": e.body,
                }
                for e in res.entries
            ],
            "text": _render_entries(res.entries),
        }

    if tool == "hive_history":
        res = messaging.hive_history(conn, session, limit=int(args.get("limit", 50)))
        _audit(res.args_summary, session.team)
        return {"count": len(res.entries), "text": _render_entries(res.entries)}

    if tool == "hive_members":
        members = messaging.hive_members(conn, session)
        _audit(audit.build_summary(tool="hive_members", count=len(members)), session.team)
        return {
            "members": [
                {
                    "name": m.name,
                    "vendor": m.vendor,
                    "trust_level": m.trust_level,
                    "joined_at": m.joined_at,
                }
                for m in members
            ],
            "text": "\n".join(
                f"{m.name} vendor={m.vendor or '?'} trust={m.trust_level} joined={m.joined_at}"
                for m in members
            )
            or "(メンバーなし)",
        }

    if tool == "hive_remember":
        if not ctx.memory_enabled or ctx.store is None:
            raise messaging.MessagingError(
                f"記憶系は無効です（{ctx.memory_error or '埋め込み/Zvec 構成の不一致'}）。"
            )
        res = ctx.store.hive_remember(
            session, kind=args["kind"], text=args["text"], request_id=request_id
        )
        _audit(res.args_summary, session.team)
        return {
            "accepted": res.accepted,
            "memory_id": res.memory_id,
            "reused": res.reused,
            "verdict": res.verdict,
            "warnings": res.warnings,
            "text": ("記憶しました。" if res.accepted else "秘密パターン検査により拒否しました。")
            + ("".join(f" [警告] {w}" for w in res.warnings)),
        }

    if tool == "hive_recall":
        if not ctx.memory_enabled or ctx.store is None:
            raise messaging.MessagingError(
                f"記憶系は無効です（{ctx.memory_error or '埋め込み/Zvec 構成の不一致'}）。"
            )
        res = ctx.store.hive_recall(
            session, query=args["query"], kind=args.get("kind"), top_k=int(args.get("top_k", 5))
        )
        _audit(res.args_summary, session.team)
        return {"count": len(res.entries), "text": _render_recall(res.entries)}

    if tool == "hive_stats":
        stats = compute_stats(conn, session.team)
        _audit(audit.build_summary(tool="hive_stats"), session.team)
        return {"stats": stats, "text": "\n".join(f"{k}={v}" for k, v in stats.items())}

    if tool == "admin:set-trust":
        summary = messaging.admin_set_trust(
            conn,
            team=args["team"],
            name=args["name"],
            level=int(args["level"]),
            restamp=bool(args.get("restamp", False)),
        )
        _audit(audit.build_summary(**summary), args["team"])
        return {"result": summary, "text": f"set-trust {args['name']} -> {args['level']}"}

    if tool == "admin:reset-token":
        token, summary = messaging.admin_reset_token(conn, team=args["team"], name=args["name"])
        _audit(audit.build_summary(**summary), args["team"])
        return {
            "token": token,
            "text": f"reset-token {args['name']}（新トークンを応答で返しました）",
        }

    raise messaging.MessagingError(f"未知のツール: {tool!r}")


def compute_stats(conn: sqlite3.Connection, team: str) -> dict[str, int]:
    """hive_stats: recall 利用回数・メッセージ往復数等（計測用）。"""

    def _count(sql: str, params: tuple = ()) -> int:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    return {
        "messages": _count("SELECT COUNT(*) FROM messages WHERE team = ?", (team,)),
        "mcp_messages": _count(
            "SELECT COUNT(*) FROM messages m JOIN mcp_posts p ON p.message_id = m.id"
            " WHERE m.team = ?",
            (team,),
        ),
        "members": _count("SELECT COUNT(*) FROM members WHERE team = ?", (team,)),
        "memories_committed": _count(
            "SELECT COUNT(*) FROM memories WHERE team = ? AND status = 'committed'", (team,)
        ),
        "recall_calls": _count(
            "SELECT COUNT(*) FROM audit WHERE team = ? AND tool = 'hive_recall'", (team,)
        ),
        "post_calls": _count(
            "SELECT COUNT(*) FROM audit WHERE team = ? AND tool = 'hive_post'", (team,)
        ),
    }


# ---- MCP プロセス（プロキシ or ライター）------------------------------------------------------
class HiveMcpProcess:
    """1 つの MCP stdio プロセス。writer.Endpoint 経由で要求を処理し、自セッションを保持する。"""

    def __init__(self, hive_root: Path, db_path: Path, team: str) -> None:
        from .writer import Endpoint  # writer は stdlib のみだが遅延で依存境界を明示

        self.hive_root = hive_root
        self.db_path = db_path
        self.team = team
        self.session: dict[str, str] | None = None
        self._lock = threading.Lock()  # ライター資源への直列アクセス（単一ライター）

        def resources_factory():
            ctx = build_context(db_path, team)

            def handler(req: dict[str, Any]) -> dict[str, Any]:
                with self._lock:  # socket 受付スレッドと MCP スレッドを直列化
                    return dispatch_tool(ctx, req)

            return ctx, handler

        self.endpoint = Endpoint(hive_root, resources_factory)

    def start(self) -> None:
        """flock 取得を試み、ライターなら受付ループを別スレッドで回す（プロキシなら何もしない）。"""
        self.endpoint.acquire()
        if self.endpoint.is_writer:
            t = threading.Thread(target=self.endpoint.serve, name="hive-writer", daemon=True)
            t.start()

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """ツールを 1 回呼ぶ。書込系は request_id を付与し冪等化する。"""
        req: dict[str, Any] = {
            "tool": tool,
            "args": args,
            "session": self.session,
            "request_id": uuid.uuid4().hex if tool in _WRITE_TOOLS else None,
        }
        resp = self.endpoint.dispatch(req)
        if not resp.get("ok", False):
            err = resp.get("error", {})
            raise RuntimeError(f"{err.get('type', 'Error')}: {err.get('message', '不明なエラー')}")
        result = resp.get("result", {})
        if tool == "hive_join" and isinstance(result, dict) and result.get("session"):
            self.session = result["session"]  # 以後のツール呼び出しに束縛
        return result

    def close(self) -> None:
        self.endpoint.close()


# ---- エントリポイント（console_scripts: subaco-hive → main）------------------------------------
def run_stdio(proc: HiveMcpProcess) -> None:
    """公式 MCP Python SDK（FastMCP）で stdio サーバーを起動しツールを登録する（**mcp を遅延 import**）。

    stdout は JSON-RPC 専有（ログは stderr）。mcp 未導入なら明示エラーで終了する。
    """
    try:
        from mcp.server.fastmcp import FastMCP  # 遅延 import
    except ImportError as exc:
        raise SystemExit(
            "mcp SDK が未導入です。`pip install 'subaco-hive[mcp]'` を実行してください。"
        ) from exc

    app = FastMCP("subaco-hive")

    @app.tool()
    def hive_join(
        name: str, vendor: str | None = None, team: str | None = None, token: str | None = None
    ) -> str:
        """コミュニティに参加する（members 登録・メンバートークン発行/照合・セッション束縛）。"""
        return proc.call(
            "hive_join", {"name": name, "vendor": vendor, "team": team, "token": token}
        )["text"]

    @app.tool()
    def hive_post(body: str, recipient: str | None = None) -> str:
        """メッセージを送信する（recipient 省略でブロードキャスト。秘密検査あり）。"""
        return proc.call("hive_post", {"body": body, "recipient": recipient})["text"]

    @app.tool()
    def hive_inbox(since: str | None = None, include_untrusted: bool = False) -> str:
        """未読を取得する（既定は trust>=1 本文＋未信頼メタデータ一度きり）。"""
        return proc.call("hive_inbox", {"since": since, "include_untrusted": include_untrusted})[
            "text"
        ]

    @app.tool()
    def hive_members() -> str:
        """登録メンバー一覧（name/vendor/trust/joined）。"""
        return proc.call("hive_members", {})["text"]

    @app.tool()
    def hive_history(limit: int = 50) -> str:
        """ルーム履歴の再生（ブロードキャストのみ・文脈シード用）。"""
        return proc.call("hive_history", {"limit": limit})["text"]

    @app.tool()
    def hive_remember(kind: str, text: str) -> str:
        """長期記憶に書き込む（二段書き・秘密検査あり）。"""
        return proc.call("hive_remember", {"kind": kind, "text": text})["text"]

    @app.tool()
    def hive_recall(query: str, kind: str | None = None, top_k: int = 5) -> str:
        """セマンティック検索（固定フィルタ source_trust>=1 かつ現在 trust>=1）。"""
        return proc.call("hive_recall", {"query": query, "kind": kind, "top_k": top_k})["text"]

    @app.tool()
    def hive_stats() -> str:
        """利用統計（recall 回数・メッセージ往復数等）。"""
        return proc.call("hive_stats", {})["text"]

    app.run()  # stdio トランスポート（既定）


def main() -> None:
    """console_scripts `subaco-hive` のエントリ。stdio MCP サーバーを起動する。"""
    setup_logging(config.resolve_log_level())
    db_path = config.resolve_db_path()
    hive_root = db_path.parent
    team = config.resolve_team(hive_root)
    _log.info("subaco-hive 起動: team=%s db=%s", team, db_path)
    proc = HiveMcpProcess(hive_root, db_path, team)
    proc.start()
    try:
        run_stdio(proc)
    finally:
        proc.close()


if __name__ == "__main__":  # pragma: no cover
    main()
