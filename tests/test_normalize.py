"""team / name の文字集合検証のテスト（stdlib のみ）。

**重要:** subaco_hive はチーム名を「導出（正規化）しない」。導出は devShell の `hive-team`
ヘルパーが唯一実装する。ここで検証するのは「不正値を弾く入力バリデーション」であり、
正規化規則（NFC・小文字化・`[a-z0-9_-]`・64 字上限）の結果と同一の文字集合を受理条件とする。
"""

from __future__ import annotations

import unicodedata

import pytest

from subaco_hive import messaging
from subaco_hive.config import (
    MAX_NAME_LEN,
    ConfigError,
    is_valid_name,
    validate_name,
)


# ---- 受理される値（正規化済みの結果に一致する形）---------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "alpha",
        "team-1",
        "a_b_c",
        "x",
        "0123456789",
        "my-product_2",
        "a" * MAX_NAME_LEN,  # 64 字ちょうどは可
    ],
)
def test_valid_names_accepted(value):
    assert is_valid_name(value) is True
    assert validate_name(value, kind="team") == value  # 導出せずそのまま返す


# ---- 弾かれる値 -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "",  # 空
        "UPPER",  # 大文字（小文字化は hive-team の責務。ここでは弾く）
        "Mixed_Case",
        "with space",
        "dot.name",  # `.` は許可外
        "slash/name",
        "emoji😀",
        "日本語チーム",  # 非 ASCII は許可外
        "tab\tname",
        "newline\nname",
        "a" * (MAX_NAME_LEN + 1),  # 65 字は上限超過
    ],
)
def test_invalid_names_rejected(value):
    assert is_valid_name(value) is False
    with pytest.raises(ConfigError):
        validate_name(value, kind="name")


def test_error_message_includes_kind_label():
    """kind ラベル（team / name）がエラーメッセージに載る（呼び出し側の診断用）。"""
    with pytest.raises(ConfigError) as ei:
        validate_name("BAD NAME", kind="team")
    assert "team" in str(ei.value)


def test_nfd_form_is_rejected_but_ascii_is_nfc_stable():
    """NFC 明記の意図（macOS の NFD ファイル名照合差異の吸収）を確認する。

    NFD（分解形）の合成文字は NFC != value となり弾かれる。ASCII 名は NFC で不変のため受理される。
    """
    nfd = unicodedata.normalize("NFD", "がぎ")  # 濁点が分解された非 NFC 文字列
    assert unicodedata.normalize("NFC", nfd) != nfd
    assert is_valid_name(nfd) is False  # 非 NFC は弾く（そもそも非 ASCII でもある）
    assert unicodedata.normalize("NFC", "alpha") == "alpha"
    assert is_valid_name("alpha") is True


# ---- hive_join が検証を適用する------------------------------------------------
def test_hive_join_rejects_invalid_name(conn):
    with pytest.raises(ConfigError):
        messaging.hive_join(conn, team="alpha", name="Bad Name", vendor="x", request_id="J1")
    # 不正 name では members 行を作らない。
    assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0


def test_hive_join_rejects_invalid_team(conn):
    with pytest.raises(ConfigError):
        messaging.hive_join(conn, team="Bad Team", name="w", vendor="x", request_id="J1")
