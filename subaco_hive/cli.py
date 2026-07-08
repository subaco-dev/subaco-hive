"""ops/admin CLI（console_scripts `hive`）。

サブコマンド:
  - hive admin set-trust <team> <name> <level> [--restamp]
  - hive admin reset-token <team> <name>
  - hive admin backup <path> / hive admin restore <path>
  - hive reembed
  - hive stats
  - hive migrate               （v0 は schema_version 照合のみ。runner は将来対応）

管理操作の経路:
  - 常駐ライターがいれば hive.sock の**管理チャネル**経由で依頼（flock は取れない → プロキシ転送）。
  - いなければ**一時ライター**として flock を取得して実行（一回限り・hive.sock は作成せず flock 解放で終了）。
  いずれの経路も audit へ記録する（server.dispatch_tool 側 or 本 CLI が直接記録）。
TTY 確認は摩擦（enforce ではない）。PTY 偽装で迂回可能なため defense-in-depth に留める。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from pathlib import Path

from . import audit, config, db, messaging
from .logging import get_logger, setup_logging

_log = get_logger(__name__)


# ---- 経路選択（常駐ライター管理チャネル or 一時ライター）--------------------------------------
def _via_resident_writer(hive_root: Path, tool: str, args: dict) -> dict:
    """常駐ライターの hive.sock 管理チャネルへ admin 要求を送る（flock を取れなかった場合）。"""
    from .writer import ProxyClient

    req = {"tool": tool, "args": args, "session": None, "request_id": uuid.uuid4().hex}
    resp = ProxyClient(hive_root).request(req)
    if not resp.get("ok", False):
        err = resp.get("error", {})
        raise SystemExit(f"管理操作に失敗: {err.get('type')}: {err.get('message')}")
    return resp.get("result", {})


def _run_admin(hive_root: Path, db_path: Path, tool: str, args: dict):
    """admin 操作を実行する。一時ライター（flock 取得）優先、取れなければ常駐ライターへ転送。

    handler(conn) を受け取り、一時ライター経路では自分で実行し audit も記録する。
    """
    from .writer import release_lock, try_acquire_lock

    fh = try_acquire_lock(hive_root)
    if fh is None:
        # 常駐ライターが flock 保持中 → 管理チャネル経由（server 側で audit 記録される）。
        _log.info("常駐ライターを検出。管理チャネル（hive.sock）経由で %s を依頼します。", tool)
        return _via_resident_writer(hive_root, tool, args)
    try:
        _log.info(
            "一時ライターとして flock を取得し %s を実行します（hive.sock は作成しません）。",
            tool,
        )
        conn = db.open_db(db_path)
        try:
            return _admin_local(conn, tool, args)
        finally:
            conn.close()
    finally:
        release_lock(fh)


def _admin_local(conn, tool: str, args: dict) -> dict:
    """一時ライター経路の admin 実処理＋audit 記録。"""
    if tool == "admin:set-trust":
        summary = messaging.admin_set_trust(
            conn,
            team=args["team"],
            name=args["name"],
            level=int(args["level"]),
            restamp=bool(args.get("restamp", False)),
        )
        audit.record(
            conn,
            team=args["team"],
            tool="admin:set-trust",
            member=None,
            args_summary=audit.build_summary(**summary),
        )
        return summary
    if tool == "admin:reset-token":
        token, summary = messaging.admin_reset_token(conn, team=args["team"], name=args["name"])
        audit.record(
            conn,
            team=args["team"],
            tool="admin:reset-token",
            member=None,
            args_summary=audit.build_summary(**summary),
        )
        return {"token": token, **summary}
    raise SystemExit(f"未知の admin ツール: {tool}")


# ---- サブコマンド実装 --------------------------------------------------------------------------
def cmd_set_trust(a: argparse.Namespace) -> int:
    hive_root = config.resolve_hive_root()
    db_path = config.resolve_db_path()
    result = _run_admin(
        hive_root,
        db_path,
        "admin:set-trust",
        {"team": a.team, "name": a.name, "level": a.level, "restamp": a.restamp},
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_reset_token(a: argparse.Namespace) -> int:
    hive_root = config.resolve_hive_root()
    db_path = config.resolve_db_path()
    result = _run_admin(hive_root, db_path, "admin:reset-token", {"team": a.team, "name": a.name})
    token = result.get("token")
    if token:
        print(f"新しいメンバートークン（安全に保管し、当該エージェントへ渡してください）:\n{token}")
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_stats(a: argparse.Namespace) -> int:
    from .server import compute_stats

    db_path = config.resolve_db_path()
    team = config.resolve_team()
    conn = db.open_db(db_path)
    try:
        print(json.dumps(compute_stats(conn, team), ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_migrate(a: argparse.Namespace) -> int:
    """v0 は schema_version 照合のみ（前方マイグレーション runner は将来対応）。"""
    db_path = config.resolve_db_path()
    conn = db.open_db(db_path)  # open_db が verify_schema_version を内包
    try:
        db.verify_schema_version(conn)
        stored = db.get_meta(conn, db.META_SCHEMA_VERSION)
        print(f"schema_version={stored} ok（v0 は照合のみ。runner は将来対応）")
    finally:
        conn.close()
    return 0


def cmd_reembed(a: argparse.Namespace) -> int:
    """モデル切替の原子的スワップ。一時ライターとして flock を取得して実行する。"""
    from .writer import release_lock, try_acquire_lock

    hive_root = config.resolve_hive_root()
    db_path = config.resolve_db_path()
    fh = try_acquire_lock(hive_root)
    if fh is None:
        raise SystemExit(
            "常駐ライターが稼働中です。reembed は単一ライターで行うため、"
            "先に MCP サーバー（subaco-hive）を停止してください。"
        )
    try:
        from . import embedding
        from .memory import MemoryStore

        conn = db.open_db(db_path)
        try:
            provider = embedding.build_provider(a.provider, model=a.model)
            store = MemoryStore(conn, provider)
            new = embedding.reembed(conn, store, provider)
            audit.record(
                conn,
                team=config.resolve_team(hive_root),
                tool="admin:reembed",
                member=None,
                args_summary=audit.build_summary(
                    tool="admin:reembed",
                    model=provider.model_name,
                    dim=provider.dim,
                    active_collection=new,
                ),
            )
            print(f"reembed 完了: active_collection={new} model={provider.model_name}")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - zvec/fastembed 未導入等を分かりやすく
        raise SystemExit(f"reembed に失敗しました（記憶系依存が必要）: {exc}") from exc
    finally:
        release_lock(fh)
    return 0


# ---- backup / restore------------------------------------------------------------------
_MANIFEST_NAME = "manifest.json"
_MEMORY_DIRNAME = "memory"


def _read_manifest(meta_source_conn) -> dict:
    """バックアップマニフェスト（整合照合用の非本文メタ）を構成する。"""
    return {
        "schema_version": db.get_meta(meta_source_conn, db.META_SCHEMA_VERSION),
        "embedding_model": db.get_meta(meta_source_conn, db.META_EMBEDDING_MODEL),
        "embedding_dim": db.get_meta(meta_source_conn, db.META_EMBEDDING_DIM),
        "active_collection": db.get_meta(meta_source_conn, db.META_ACTIVE_COLLECTION),
    }


def cmd_backup(a: argparse.Namespace) -> int:
    """SQLite `.backup` + Zvec コレクションディレクトリのスナップショット。

    常駐ライターがいれば flock は取れないが SQLite `.backup` は WAL 下でオンライン安全。
    Zvec スナップショットの静止は一時ライター flock 取得で担保する（取れない場合は警告 — TODO 管理チャネル quiesce）。
    """
    from .writer import release_lock, try_acquire_lock

    hive_root = config.resolve_hive_root()
    db_path = config.resolve_db_path()
    dest = Path(a.path)
    dest.mkdir(parents=True, exist_ok=True)

    fh = try_acquire_lock(hive_root)
    if fh is None:
        _log.warning(
            "常駐ライター稼働中。SQLite はオンライン .backup で安全ですが、Zvec スナップショットは"
            "静止できない可能性があります（TODO: 管理チャネル quiesce）。"
        )
    try:
        conn = db.open_db(db_path)
        try:
            manifest = _read_manifest(conn)
            # SQLite の .backup API（オンライン安全）。
            bkp = db.connect(dest / "messages.db")
            with bkp:
                conn.backup(bkp)
            bkp.close()
            # Zvec コレクションディレクトリのスナップショット（存在すれば）。
            mem_src = hive_root / _MEMORY_DIRNAME
            if mem_src.exists():
                mem_dst = dest / _MEMORY_DIRNAME
                if mem_dst.exists():
                    shutil.rmtree(mem_dst)
                shutil.copytree(mem_src, mem_dst)
            (dest / _MANIFEST_NAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            audit.record(
                conn,
                team=config.resolve_team(hive_root),
                tool="admin:backup",
                member=None,
                args_summary=audit.build_summary(tool="admin:backup", dest=str(dest)),
            )
            print(f"backup 完了: {dest}（messages.db + {_MEMORY_DIRNAME}/ + {_MANIFEST_NAME}）")
        finally:
            conn.close()
    finally:
        release_lock(fh)
    return 0


def cmd_restore(a: argparse.Namespace) -> int:
    """バックアップ組を同一時点で差し替える。整合照合に失敗したら fail-closed で拒否する。

    hive-mcp を停止した状態で行う（常駐ライターが flock 保持中なら拒否）。schema_version / embedding /
    active_collection の整合が取れる組み合わせであることを照合してから受理する。
    """
    from .writer import release_lock, try_acquire_lock

    hive_root = config.resolve_hive_root()
    db_path = config.resolve_db_path()
    src = Path(a.path)
    src_db = src / "messages.db"
    manifest_path = src / _MANIFEST_NAME
    if not src_db.exists():
        raise SystemExit(f"バックアップに messages.db がありません: {src_db}")

    fh = try_acquire_lock(hive_root)
    if fh is None:
        raise SystemExit(
            "常駐ライターが稼働中です。restore は hive-mcp 停止状態で行ってください。"
        )
    try:
        # 整合照合（fail-closed）: バックアップ DB のメタとマニフェスト・Zvec スナップショットの一致。
        bkp_conn = db.connect(src_db)
        try:
            db.verify_schema_version(bkp_conn)  # schema_version が現行と非互換なら SchemaError
            meta = _read_manifest(bkp_conn)
        finally:
            bkp_conn.close()

        if manifest_path.exists():
            declared = json.loads(manifest_path.read_text(encoding="utf-8"))
            for key in ("schema_version", "embedding_model", "embedding_dim", "active_collection"):
                if declared.get(key) != meta.get(key):
                    raise SystemExit(
                        f"整合照合に失敗（fail-closed）: manifest.{key}={declared.get(key)!r} "
                        f"!= DB.{key}={meta.get(key)!r}。"
                    )
        # Zvec スナップショットの active_collection ディレクトリが揃っているか（存在時のみ照合）。
        active = meta.get("active_collection")
        mem_src = src / _MEMORY_DIRNAME
        if mem_src.exists() and active and not (mem_src / active).exists():
            _log.warning(
                "Zvec スナップショットに active_collection ディレクトリ %s が見当たりません"
                "（Zvec のレイアウト依存。照合は Zvec spike で精緻化）。",
                active,
            )

        # 差し替え（照合通過後）。
        shutil.copyfile(src_db, db_path)
        for suffix in ("-wal", "-shm"):  # WAL 副産物は古いものを除去してクリーンに再構成させる
            side = Path(str(db_path) + suffix)
            if side.exists():
                side.unlink()
        if mem_src.exists():
            mem_dst = hive_root / _MEMORY_DIRNAME
            if mem_dst.exists():
                shutil.rmtree(mem_dst)
            shutil.copytree(mem_src, mem_dst)

        conn = db.open_db(db_path)
        try:
            audit.record(
                conn,
                team=config.resolve_team(hive_root),
                tool="admin:restore",
                member=None,
                args_summary=audit.build_summary(tool="admin:restore", src=str(src)),
            )
        finally:
            conn.close()
        print(f"restore 完了: {src} を受理しました（整合照合通過）。")
    finally:
        release_lock(fh)
    return 0


# ---- argparse ---------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hive", description="Subaco hive ops/admin CLI。")
    sub = p.add_subparsers(dest="cmd", required=True)

    admin = sub.add_parser("admin", help="管理操作（人間オペレータ用）。")
    asub = admin.add_subparsers(dest="admin_cmd", required=True)

    st = asub.add_parser("set-trust", help="trust_level を設定（昇格/降格）。")
    st.add_argument("team")
    st.add_argument("name")
    st.add_argument("level", type=int, choices=[0, 1, 2])
    st.add_argument(
        "--restamp",
        action="store_true",
        help="当該著者の memories.source_trust も新値へ更新（未信頼記憶の可視化）。",
    )
    st.set_defaults(func=cmd_set_trust)

    rt = asub.add_parser("reset-token", help="メンバートークンを再発行（紛失時復旧）。")
    rt.add_argument("team")
    rt.add_argument("name")
    rt.set_defaults(func=cmd_reset_token)

    bk = asub.add_parser("backup", help="SQLite + Zvec のスナップショット。")
    bk.add_argument("path")
    bk.set_defaults(func=cmd_backup)

    rs = asub.add_parser("restore", help="バックアップ組を整合照合の上で差し替え。")
    rs.add_argument("path")
    rs.set_defaults(func=cmd_restore)

    re = sub.add_parser("reembed", help="モデル切替の原子的再埋め込み。")
    re.add_argument("--provider", default=None, help="fastembed（既定）/ openai。")
    re.add_argument("--model", default=None, help="埋め込みモデル名。")
    re.set_defaults(func=cmd_reembed)

    stt = sub.add_parser("stats", help="利用統計（hive_stats 相当）。")
    stt.set_defaults(func=cmd_stats)

    mg = sub.add_parser("migrate", help="schema_version 照合（v0。runner は将来対応）。")
    mg.set_defaults(func=cmd_migrate)

    return p


def main(argv: list[str] | None = None) -> int:
    """console_scripts `hive` のエントリ。"""
    setup_logging(config.resolve_log_level())
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (config.ConfigError, db.DbError, messaging.MessagingError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
