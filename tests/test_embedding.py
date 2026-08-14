"""埋め込み層（subaco_hive.embedding）の純関数テスト（依存ゼロで動く）。"""

from __future__ import annotations

import subaco_hive.embedding as emb


def test_temp_collection_name_avoids_current_active(monkeypatch):
    """同一秒内の reembed 再実行で new == old にならないこと（+1 秒ずらしで回避）。

    active_collection が既に一時名（前回 reembed の産物）のとき、サフィックスは秒解像度の
    ため同名を生成し得る。その場合 create が既存パスで失敗する（fail-closed だが不透明な
    エラー——レビューで実機再現）。avoid 指定で衝突を回避することを固定する。
    """
    monkeypatch.setattr(emb.time, "time", lambda: 1_700_000_000)
    first = emb.temp_collection_name("hive_alpha")
    assert first == "hive_alpha__reembed_1700000000"
    # 現 active と同名になる場合は +1 秒ずらす。
    assert emb.temp_collection_name("hive_alpha", avoid=first) == "hive_alpha__reembed_1700000001"
    # 衝突しなければ時刻どおりの名を返す。
    assert emb.temp_collection_name("hive_alpha", avoid="hive_alpha") == first
