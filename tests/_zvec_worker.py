"""Zvec 統合テスト用のワーカースクリプト（pytest からは subprocess で起動）。

`test_` で始まらないため pytest は収集しない。`import subaco_hive` が解決できるよう、
呼び出し側は env["PYTHONPATH"] にリポジトリルートを設定する。

使い方:
    python _zvec_worker.py hold <root> <collection> <dim>
        コレクションを書き込みモードで開いて 1 件 insert し、"READY" を stdout へ出して待機する
        （親が SIGKILL する。フェイルオーバー前提の検証用）。

    python _zvec_worker.py open-write <root> <collection>
        コレクションを書き込みモードで開けるかだけを試し、"OPENED" / "FAILED <理由>" を出して終了する。
"""

from __future__ import annotations

import sys
import time


def _backend(root: str):
    from subaco_hive.memory import ZvecBackend

    return ZvecBackend(root=root)


def hold(root: str, collection: str, dim: int) -> int:
    be = _backend(root)
    be.insert(
        collection,
        id="held",
        vector=[0.1] * dim,
        text="子プロセスが書いた本文",
        kind="note",
        author="child",
        trust=1,
    )
    print("READY", flush=True)
    while True:
        time.sleep(0.2)


def open_write(root: str, collection: str) -> int:
    be = _backend(root)
    try:
        be.get_text(collection, "held")
    except Exception as exc:
        print(f"FAILED {type(exc).__name__}: {exc}", flush=True)
        return 1
    print("OPENED", flush=True)
    return 0


def main(argv: list[str]) -> int:
    action = argv[1]
    if action == "hold":
        return hold(argv[2], argv[3], int(argv[4]))
    if action == "open-write":
        return open_write(argv[2], argv[3])
    raise SystemExit(f"unknown action: {action}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
