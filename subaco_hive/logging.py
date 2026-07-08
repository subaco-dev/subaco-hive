"""診断ログ。

**最重要の制約: stdout は JSON-RPC（MCP stdio トランスポート）専有。ログは stderr に出す。**
標準ロギングの basicConfig は既定で stderr だが、ルートロガーに stdout ハンドラが
混入する事故を避けるため、subaco_hive 専用ロガーを stderr 固定・propagate=False で構成する。

レベルは HIVE_LOG_LEVEL（config.resolve_log_level）で制御する。
必須ログ点＝ライター昇格／フェイルオーバー、flock 争奪・stale socket 掃除、
プロキシ再送と冪等突合、孤児掃除 — は段階2以降の各モジュールがこのロガーで出力する。

注意: このモジュールは stdlib の `logging` を「絶対 import」で参照する
（Python3 の既定は絶対 import のため `import logging` は top-level の stdlib を指し、
本モジュール自身を指さない）。
"""

from __future__ import annotations

import logging as _stdlib_logging
import sys

from .config import resolve_log_level

# パッケージ専用ロガーの名前空間。全モジュールは get_logger(__name__) で子ロガーを得る。
ROOT_LOGGER_NAME = "subaco_hive"

_LEVEL_MAP = {
    "debug": _stdlib_logging.DEBUG,
    "info": _stdlib_logging.INFO,
    "warning": _stdlib_logging.WARNING,
    "error": _stdlib_logging.ERROR,
    "critical": _stdlib_logging.CRITICAL,
}

_configured = False


def _level_to_int(level: str) -> int:
    return _LEVEL_MAP.get(level.strip().lower(), _stdlib_logging.INFO)


def setup_logging(level: str | None = None) -> _stdlib_logging.Logger:
    """subaco_hive ロガーを stderr 固定で構成し、ルートロガーを返す。

    - ハンドラは sys.stderr にのみ出力する（stdout 厳禁 — JSON-RPC 専有）。
    - propagate=False でルートロガー（stdout ハンドラを持ち得る）へ伝播させない。
    - 冪等: 複数回呼んでもハンドラは重複させず、レベルのみ更新する。
    """
    global _configured
    logger = _stdlib_logging.getLogger(ROOT_LOGGER_NAME)
    resolved = level if level is not None else resolve_log_level()
    logger.setLevel(_level_to_int(resolved))
    logger.propagate = False  # ルート（stdout 混入の可能性）へ流さない

    if not _configured:
        handler = _stdlib_logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(
            _stdlib_logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        logger.addHandler(handler)
        _configured = True
    else:
        # 既存ハンドラのストリームが stderr であることを保証（防御的）。
        for h in logger.handlers:
            if isinstance(h, _stdlib_logging.StreamHandler):
                h.setStream(sys.stderr)
    return logger


def get_logger(name: str | None = None) -> _stdlib_logging.Logger:
    """モジュール用ロガーを返す。初回呼び出しで setup_logging を保証する。

    name には通常 __name__ を渡す。"subaco_hive" 名前空間外の名前は同名前空間下へ寄せる。
    """
    if not _configured:
        setup_logging()
    if not name or name == ROOT_LOGGER_NAME:
        return _stdlib_logging.getLogger(ROOT_LOGGER_NAME)
    if name.startswith(ROOT_LOGGER_NAME + "."):
        return _stdlib_logging.getLogger(name)
    # __main__ 等の外部名は専用名前空間の子に寄せる。
    return _stdlib_logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
