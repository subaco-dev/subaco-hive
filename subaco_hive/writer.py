"""first-writer-wins（単一ライター）機構。

ライターの正当性は**ソケットの存在ではなく flock の保持**で担保する。flock 対象は hive.sock とは
別の恒久ロックファイル `.hive/writer.lock`（稼働中は unlink・再作成しない）。hive.sock の unlink・再作成は
現行 flock 保持者のみが行う。これにより「昇格プロセスが旧 inode の flock を保持したまま socket を再作成 →
後続が新 inode の flock を取得」という unlink+flock レースによる二重ライターを排除する。

接続側アルゴリズム: flock 取得を試行 → 失敗ならソケット接続を 100ms 間隔・上限 5 秒でポーリング →
上限で flock 試行からやり直すリトライループ（接続拒否・EOF も同ループ）。EOF 検知後はフェイルオーバー完了を
待って同一 request_id で新ライターへ再送し、それも失敗した場合のみ呼び出し側へエラーを返す。

アクセス制御: `.hive/` 0700・`hive.sock` 0600、接続受付時に SO_PEERCRED（macOS は LOCAL_PEERCRED）で
同一 UID のみ許可。同一 UID の悪意プロセスはスコープ外。

============================================================================================
proxy↔writer フレーミング（1 つに確定）: **改行区切り JSON（NDJSON）** を採用する。
  - 1 メッセージ = UTF-8 JSON オブジェクト + `\n`。SOCK_STREAM 上で行単位に読む。
  - 要求: {"request_id": str, "session": {"team":.., "name":..} | null, "tool": str, "args": {..}}
  - 応答: {"request_id": str, "ok": true, "result": {..}}  /  {"request_id": str, "ok": false,
           "error": {"type": str, "message": str}}
  - request_id は冪等キー。管理チャネル（admin:set-trust 等）も同じフレーミングに相乗りする。
============================================================================================
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .logging import get_logger

_log = get_logger(__name__)

# AF_UNIX のパス長上限（macOS ~104 / Linux ~108 バイト）対策。深いネストの `.hive/hive.sock` が上限を
# 超える場合、親ディレクトリへ一時 chdir して basename で bind/connect する（グローバル chdir はロックで直列化）。
_UNIX_PATH_SAFE_LEN = 90
_CHDIR_LOCK = threading.Lock()


def _bind_unix(sock: socket.socket, path: Path) -> None:
    """AF_UNIX の bind。パスが長すぎる場合は親へ chdir して basename で bind する。"""
    s = str(path)
    if len(s) < _UNIX_PATH_SAFE_LEN:
        sock.bind(s)
        return
    directory, base = os.path.split(s)
    with _CHDIR_LOCK:
        prev = os.getcwd()
        os.chdir(directory)
        try:
            sock.bind(base)
        finally:
            os.chdir(prev)


def _connect_unix(sock: socket.socket, path: Path) -> None:
    """AF_UNIX の connect。パスが長すぎる場合は親へ chdir して basename で connect する。"""
    s = str(path)
    if len(s) < _UNIX_PATH_SAFE_LEN:
        sock.connect(s)
        return
    directory, base = os.path.split(s)
    with _CHDIR_LOCK:
        prev = os.getcwd()
        os.chdir(directory)
        try:
            sock.connect(base)
        finally:
            os.chdir(prev)


SOCKET_NAME = "hive.sock"
WRITER_LOCK_NAME = "writer.lock"
FRAMING = "ndjson"  # 確定値
RETRY_INTERVAL_S = 0.1  # 接続ポーリング間隔
RETRY_TIMEOUT_S = 5.0  # flock 再試行までの上限
_MAX_LINE_BYTES = 8 * 1024 * 1024  # 1 メッセージ上限（防御的）


# ---- flock（ライター正当性の根拠）------------------------------------------------------
def lock_path(hive_root: Path) -> Path:
    return Path(hive_root) / WRITER_LOCK_NAME


def socket_path(hive_root: Path) -> Path:
    return Path(hive_root) / SOCKET_NAME


def connect_socket(hive_root: Path, *, timeout: float = RETRY_TIMEOUT_S) -> socket.socket:
    """hive.sock へ接続済みのクライアントソケットを返す（長パス対応。呼び出し側が close する）。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    _connect_unix(s, socket_path(hive_root))
    return s


def ensure_hive_dir(hive_root: Path) -> None:
    """`.hive/` を 0700 で確保する（自領域が無ければ作る防御）。"""
    p = Path(hive_root)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # pragma: no cover - 権限差異
        os.chmod(p, 0o700)


def try_acquire_lock(hive_root: Path):
    """writer.lock へ非ブロッキング flock を試みる。成功なら開いたファイルオブジェクト、失敗なら None。

    返したファイルオブジェクトは flock を保持し続けるため、呼び出し側が保持し続けること（close で解放）。
    恒久ロックファイルは unlink しない。
    """
    ensure_hive_dir(hive_root)
    path = lock_path(hive_root)
    fh = open(path, "a+")  # noqa: SIM115 - 保持し続けるため with にしない
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            _log.debug("flock 争奪: 既存ライターが保持中（%s）。", path)
            return None
        raise
    _log.debug("flock 争奪: 取得成功（このプロセスがライター候補 — %s）。", path)
    return fh


def release_lock(fh) -> None:
    """flock を解放する（writer.lock は unlink しない）。"""
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# ---- SO_PEERCRED / LOCAL_PEERCRED（同一 UID 制限）---------------------------------------
def peer_uid(sock: socket.socket) -> int | None:
    """接続相手の UID を取得する（Linux: SO_PEERCRED / macOS: LOCAL_PEERCRED）。取得不可なら None。"""
    try:
        if sys.platform.startswith("linux"):
            # struct ucred { pid_t pid; uid_t uid; gid_t gid; } = 3 * int32
            data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", data)
            return uid
        if sys.platform == "darwin":
            # struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }
            # SOL_LOCAL=0, LOCAL_PEERCRED=0x001。cr_uid は先頭 u_int の次（オフセット 4）。
            SOL_LOCAL = 0
            LOCAL_PEERCRED = 0x001
            data = sock.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, 4 + 4)
            _version, uid = struct.unpack("II", data[:8])
            return uid
    except OSError as exc:  # pragma: no cover - プラットフォーム差異
        _log.debug("peer_uid 取得に失敗（fs 権限に委ねる）: %s", exc)
    return None


def check_same_uid(sock: socket.socket) -> bool:
    """接続相手が同一 UID か。取得不可の場合は fs 権限（0700/0600）に委ねて True（防御的）。"""
    uid = peer_uid(sock)
    if uid is None:
        return True  # 0700 ディレクトリ + 0600 ソケットで既にクロス UID 接続は阻止済み
    return uid == os.getuid()


# ---- NDJSON フレーミング------------------------------------------------------------
class Framer:
    """SOCK_STREAM 上の NDJSON 送受信。1 行 = 1 JSON オブジェクト。"""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def send(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
        self._sock.sendall(line)

    def recv(self) -> dict[str, Any] | None:
        """1 メッセージを読む。相手が切断（EOF）したら None。"""
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl])
                del self._buf[: nl + 1]
                if not line.strip():
                    continue
                return json.loads(line.decode("utf-8"))
            if len(self._buf) > _MAX_LINE_BYTES:
                raise ValueError("NDJSON メッセージが上限を超えました。")
            chunk = self._sock.recv(65536)
            if not chunk:
                return None  # EOF
            self._buf.extend(chunk)


# ---- stale socket 掃除-----------------------------------------------------------------
def cleanup_stale_socket(hive_root: Path) -> bool:
    """hive.sock が存在するが誰も listen していない（接続不可）なら unlink する。掃除したら True。

    flock 保持者だけがこれを行う前提。接続できるソケットは正当なライターのものとして残す。
    """
    path = socket_path(hive_root)
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        _connect_unix(probe, path)
        probe.close()
        return False  # 接続できた = 生きたライターがいる
    except OSError:
        probe.close()
        try:
            path.unlink()
            _log.info("stale socket 掃除: %s を削除しました。", path)
            return True
        except FileNotFoundError:
            return False


# ---- ライターサーバー（flock 保持者が起動）----------------------------------------------------
Handler = Callable[[dict[str, Any]], dict[str, Any]]


class WriterServer:
    """flock 保持者が hive.sock を作成して要求を受け、handler へ委譲する（単一ライター）。

    handler は要求 dict を受け取り応答 dict を返す純関数（DB/Zvec 書き込みは handler 内で行う）。
    サーバー自体は単一スレッド・逐次処理（Zvec の単一ライターモデルと整合）。
    """

    def __init__(self, hive_root: Path, handler: Handler) -> None:
        self.hive_root = Path(hive_root)
        self.handler = handler
        self._srv: socket.socket | None = None
        self._closed = False

    def bind(self) -> None:
        """hive.sock を 0600 で作成して listen する（flock 保持者のみが呼ぶ）。"""
        ensure_hive_dir(self.hive_root)
        cleanup_stale_socket(self.hive_root)
        path = socket_path(self.hive_root)
        if path.exists():
            # 現行 flock 保持者のみが再作成する。掃除で残った生存ソケットはここに来ない。
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # 先に umask で 0600 を保証してから bind（bind 後 chmod ではレースがある）。
        old = os.umask(0o177)
        try:
            _bind_unix(srv, path)
        finally:
            os.umask(old)
        with contextlib.suppress(OSError):  # pragma: no cover
            os.chmod(path, 0o600)
        srv.listen(64)
        self._srv = srv
        _log.info("ライター昇格: hive.sock を作成し listen 開始（%s）。", path)

    def serve_forever(self) -> None:
        """接続を逐次処理する。KeyboardInterrupt / close で終了。"""
        assert self._srv is not None, "bind() を先に呼んでください。"
        while not self._closed:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            with conn:
                if not check_same_uid(conn):
                    _log.warning("非同一 UID からの接続を拒否しました。")
                    continue
                self._handle_conn(conn)

    def _handle_conn(self, conn: socket.socket) -> None:
        framer = Framer(conn)
        while True:
            try:
                req = framer.recv()
            except (OSError, ValueError) as exc:
                _log.debug("接続読み取りエラー: %s", exc)
                return
            if req is None:
                return  # クライアント切断
            resp = self._dispatch(req)
            try:
                framer.send(resp)
            except OSError:
                return

    def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        request_id = req.get("request_id")
        try:
            result = self.handler(req)
            return {"request_id": request_id, "ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 - ワイヤへエラー型を載せて返す
            return {
                "request_id": request_id,
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }

    def close(self) -> None:
        self._closed = True
        if self._srv is not None:
            try:
                self._srv.close()
            finally:
                self._srv = None
        # hive.sock は現行保持者（自分）が片付ける。
        with contextlib.suppress(FileNotFoundError):
            socket_path(self.hive_root).unlink()


# ---- プロキシ（flock を取れなかったプロセス）--------------------------------------------------
class ProxyError(Exception):
    """プロキシがライターへ到達できない・フェイルオーバーが完了しない。"""


class ProxyClient:
    """hive.sock へ接続して要求を転送する薄いプロキシ。EOF/接続不可はリトライループで吸収する。"""

    def __init__(
        self,
        hive_root: Path,
        *,
        interval: float = RETRY_INTERVAL_S,
        timeout: float = RETRY_TIMEOUT_S,
    ) -> None:
        self.hive_root = Path(hive_root)
        self.interval = interval
        self.timeout = timeout

    def _connect(self) -> socket.socket | None:
        try:
            return connect_socket(self.hive_root, timeout=self.timeout)
        except OSError:
            return None

    def request(self, req: dict[str, Any]) -> dict[str, Any]:
        """要求を送って応答を得る。接続不可・EOF は interval 間隔でポーリング、timeout でエラー。

        同一 request_id を保持したまま再送するため、フェイルオーバー後の新ライターも processed_requests で
        突合して重複実行を抑止できる。呼び出し側は上位で flock 昇格を試みてよい。
        """
        deadline = time.monotonic() + self.timeout
        request_id = req.get("request_id")
        while time.monotonic() < deadline:
            s = self._connect()
            if s is None:
                time.sleep(self.interval)
                continue
            try:
                framer = Framer(s)
                framer.send(req)
                resp = framer.recv()
                if resp is None:
                    # 送信後 EOF: ライターがフェイルオーバー中。同一 request_id で再送する。
                    _log.info(
                        "再送突合: request_id=%s の応答前に EOF。フェイルオーバー待ちで再送します。",
                        request_id,
                    )
                    time.sleep(self.interval)
                    continue
                return resp
            except OSError:
                time.sleep(self.interval)
                continue
            finally:
                s.close()
        raise ProxyError(
            f"ライターへ {self.timeout}s 以内に到達できませんでした（request_id={request_id}）。"
        )


# ---- first-writer-wins オーケストレーション ---------------------------------------------------
class Endpoint:
    """1 プロセス分の「ライター or プロキシ」の役割を束ねる。

    使い方（server.py 側）:
      ep = Endpoint(hive_root, resources_factory, handler_factory)
      ep.acquire()                # flock 取得を試み、勝者はライター資源を開き socket を張る
      if ep.is_writer: ep.serve() # 別スレッド等でライターサーバーを回す
      resp = ep.dispatch(req)     # ライターならローカル handler、プロキシなら socket 転送
      ep.on_disconnect()          # EOF 検知時に昇格を試みる（フェイルオーバー）

    resources_factory() -> (resources, handler): flock 取得後に SQLite/Zvec を開き、handler を返す。
      昇格時の孤児掃除・残留ロック解放は resources_factory の中で行う。
    """

    def __init__(
        self,
        hive_root: Path,
        resources_factory: Callable[[], tuple[Any, Handler]],
        *,
        interval: float = RETRY_INTERVAL_S,
        timeout: float = RETRY_TIMEOUT_S,
    ) -> None:
        self.hive_root = Path(hive_root)
        self.resources_factory = resources_factory
        self.proxy = ProxyClient(hive_root, interval=interval, timeout=timeout)
        self._lock_fh = None
        self._server: WriterServer | None = None
        self._handler: Handler | None = None
        self._resources: Any = None
        self.is_writer = False

    def acquire(self) -> bool:
        """flock 取得を試みる。勝者はライター資源を開き socket を張って is_writer=True を返す。"""
        fh = try_acquire_lock(self.hive_root)
        if fh is None:
            self.is_writer = False
            return False
        self._lock_fh = fh
        # 勝者: 資源（SQLite/Zvec）を開き handler を得る（孤児掃除もここで）。
        self._resources, self._handler = self.resources_factory()
        self._server = WriterServer(self.hive_root, self._handler)
        self._server.bind()
        self.is_writer = True
        _log.info("フェイルオーバー/起動: このプロセスがライターに昇格しました。")
        return True

    def serve(self) -> None:
        """ライターサーバーの受付ループ（ブロッキング）。プロキシを兼ねるプロセスは別スレッドで回す。"""
        if self._server is None:
            raise ProxyError("acquire() でライターになってから serve() してください。")
        self._server.serve_forever()

    def dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        """要求を処理する。ライターはローカル handler、プロキシは socket 転送（EOF は昇格へ）。"""
        if self.is_writer and self._handler is not None:
            request_id = req.get("request_id")
            try:
                return {"request_id": request_id, "ok": True, "result": self._handler(req)}
            except Exception as exc:  # noqa: BLE001
                return {
                    "request_id": request_id,
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
        # プロキシ: 転送。ProxyError（フェイルオーバー未完）なら昇格を試みる。
        try:
            return self.proxy.request(req)
        except ProxyError:
            if self.acquire():  # 昇格して自分で処理
                return self.dispatch(req)
            raise

    def on_disconnect(self) -> None:
        """ライターとのソケット EOF 検知時に昇格を試みる（敗者は再接続のまま）。"""
        if not self.is_writer:
            self.acquire()

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        release_lock(self._lock_fh)
        self._lock_fh = None
        self.is_writer = False
