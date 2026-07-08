"""writer 層のテスト（flock・UNIX ソケット・NDJSON をプロセス内で検証）。

マルチプロセスのフェイルオーバー統合は pytest 統合テスト（ubuntu/macos）で別途カバーする。
ここでは OS 依存機構（flock 排他・SO_PEERCRED/LOCAL_PEERCRED・NDJSON フレーミング・stale 掃除）の
基本経路をプロセス内で確認する。
"""

from __future__ import annotations

import os
import threading

from subaco_hive import writer


def _hive(tmp_path):
    root = tmp_path / ".hive"
    root.mkdir(mode=0o700)
    return root


def test_flock_is_exclusive(tmp_path):
    root = _hive(tmp_path)
    fh1 = writer.try_acquire_lock(root)
    assert fh1 is not None
    assert writer.try_acquire_lock(root) is None  # 2 本目は取れない
    writer.release_lock(fh1)
    fh2 = writer.try_acquire_lock(root)  # 解放後は取れる
    assert fh2 is not None
    writer.release_lock(fh2)


def test_cleanup_stale_socket(tmp_path):
    root = _hive(tmp_path)
    # ソケットでない普通のファイルを置く → 接続不可 → 掃除される。
    path = writer.socket_path(root)
    path.write_text("stale")
    assert writer.cleanup_stale_socket(root) is True
    assert not path.exists()


def test_writer_proxy_ndjson_roundtrip(tmp_path):
    root = _hive(tmp_path)
    fh = writer.try_acquire_lock(root)  # ライター正当性の根拠
    assert fh is not None
    srv = writer.WriterServer(root, handler=lambda req: {"echo": req.get("args")})
    srv.bind()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        proxy = writer.ProxyClient(root, timeout=2.0)
        resp = proxy.request({"request_id": "r1", "tool": "ping", "args": {"x": 1}})
        assert resp["ok"] and resp["result"]["echo"] == {"x": 1}
    finally:
        srv.close()
        writer.release_lock(fh)


def test_writer_dispatch_error_is_framed(tmp_path):
    root = _hive(tmp_path)
    fh = writer.try_acquire_lock(root)

    def handler(req):
        raise ValueError("boom")

    srv = writer.WriterServer(root, handler=handler)
    srv.bind()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        resp = writer.ProxyClient(root, timeout=2.0).request(
            {"request_id": "r2", "tool": "x", "args": {}}
        )
        assert resp["ok"] is False and resp["error"]["type"] == "ValueError"
    finally:
        srv.close()
        writer.release_lock(fh)


def test_endpoint_local_dispatch(tmp_path):
    root = _hive(tmp_path)
    ep = writer.Endpoint(root, lambda: (None, lambda req: {"ok_from_handler": req["tool"]}))
    assert ep.acquire() is True and ep.is_writer
    try:
        resp = ep.dispatch({"request_id": "r3", "tool": "ping", "args": {}})
        assert resp["ok"] and resp["result"]["ok_from_handler"] == "ping"
    finally:
        ep.close()


def test_check_same_uid_local(tmp_path):
    root = _hive(tmp_path)
    fh = writer.try_acquire_lock(root)
    srv = writer.WriterServer(root, handler=lambda req: {})
    srv.bind()
    c = writer.connect_socket(root, timeout=2.0)
    try:
        # 同一 UID からの接続（peer_uid 取得可否に関わらず True になる）。
        uid = writer.peer_uid(c)
        assert uid is None or uid == os.getuid()
        assert writer.check_same_uid(c) is True
    finally:
        c.close()
        srv.close()
        writer.release_lock(fh)
