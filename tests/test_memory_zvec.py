"""Zvec 実体を要する記憶系の統合テスト（`importorskip` でガード）。

`zvec` wheel が入っていない環境（このマシン / Intel Mac / 多くの CI）では **skip** する。
記憶系の SQLite 状態機械（二段書き・孤児掃除・固定フィルタ、source_trust=0 / 降格著者を recall が
返さない 等）そのものは、依存ゼロで動く `InMemoryVectorBackend` を用いた `tests/test_memory.py` で
既に緑に検証済み。本ファイルは「zvec が実在するときにベクタバックエンドが期待どおり結線されるか」を
補助的に確認する層で、CI の macOS(arm64) ジョブが zvec wheel 有り時に実行する。

TODO(Zvec spike): `ZvecBackend` の CollectionSchema / insert / hybrid search / スナップショットは
spike 確定後に実装する。実装後は本ファイルを「実 ZvecBackend で source_trust=0 / 降格著者を recall が
返さない」パリティテストへ拡張する（現状 `ZvecBackend` は NotImplemented の骨子 — memory.py 参照）。
"""

from __future__ import annotations

import pytest

# zvec 未導入なら以降を丸ごと skip（wheel 無ければ skip）。
zvec = pytest.importorskip("zvec")

from subaco_hive.memory import ZvecBackend  # noqa: E402 - importorskip の後に import する


def test_zvec_importable_and_backend_constructs():
    """zvec が実在する環境で ZvecBackend が構築でき、遅延 import が解決すること。"""
    be = ZvecBackend()
    assert be is not None
    # 遅延 import が成立する（未確定 API に触れずに import 経路だけ確認）。
    assert be._ensure_zvec() is zvec


def test_zvec_backend_methods_pending_m1_4():
    """v0 骨子: 具体操作は spike 確定まで NotImplemented（回帰検出の番人）。

    ここが NotImplementedError を投げなくなったら（＝実装が入ったら）、本テストを実バックエンドの
    パリティテスト（source_trust=0 / 降格著者を recall が返さない）へ差し替える合図とする。
    """
    be = ZvecBackend()
    with pytest.raises(NotImplementedError):
        be.has_collection("hive_alpha")
    with pytest.raises(NotImplementedError):
        be.create_collection("hive_alpha", 384)
