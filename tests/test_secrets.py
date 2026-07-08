"""secrets モジュールのテスト（stdlib のみ）。"""

from __future__ import annotations

import pytest

from subaco_hive import secrets
from subaco_hive.models import VERDICT_OK, VERDICT_REDACT, VERDICT_REJECT


# --- 確度の高い一致 → reject（書込拒否）--------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "key is AKIAIOSFODNN7EXAMPLE here",
        "token ghp_012345678901234567890123456789012345 leaked",
        "anthropic sk-ant-abcdef0123456789ABCDEF_- used",
        "openai sk-proj-abcdef0123456789ABCDEFGH used",
        "google AIzaSyDaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe x",
        "slack xoxb-1234567890-abcdefghijkl here",
        "stripe sk_live_abcdefghijklmnop0123 leaked",
        "-----BEGIN RSA PRIVATE KEY-----",
        "shim e2b_0123456789abcdef0123456789abcdef here",
    ],
)
def test_high_confidence_reject(text):
    result = secrets.scan(text)
    assert result.verdict == VERDICT_REJECT
    allowed, r2 = secrets.allowed_for_write(text)
    assert allowed is False
    assert r2.reason_codes()  # 監査用の理由コードが付く


def test_reject_redacts_body_for_read():
    text = "here is AKIAIOSFODNN7EXAMPLE end"
    result = secrets.scan(text)
    assert secrets.REDACTION_MASK in result.redacted_text
    assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_text
    # 読出経路は reject でも本文を返す（赤塗り済み）。
    rr = secrets.redact_for_read(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in rr.redacted_text


# --- 高エントロピー → redact（赤塗り＋警告）-----------------------------------------------------
def test_high_entropy_redact():
    # プロバイダパターンに一致しないランダム英数（32 hex 相当）。
    text = "value=" + "a9F3k8Lm2Qr7Xz1Bp6Yt4Nw0Vc5Hs8" + " done"
    result = secrets.scan(text)
    assert result.verdict == VERDICT_REDACT
    assert secrets.REDACTION_MASK in result.redacted_text
    allowed, _ = secrets.allowed_for_write(text)
    assert allowed is True  # redact は書込許容（赤塗り本文で）


# --- 検出なし → ok ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "普通の日本語メッセージです。秘密はありません。",
        "the quick brown fox jumps over the lazy dog",
        "let's meet at 3pm to discuss the design doc",
        "short abc123 token",  # 長さ不足で高エントロピー扱いにならない
    ],
)
def test_ok_plain_text(text):
    result = secrets.scan(text)
    assert result.verdict == VERDICT_OK
    assert result.redacted_text == text
    assert result.findings == []


def test_prose_not_flagged_as_entropy():
    # 長い英文（空白区切り）はトークン長が短いため誤検知しない。
    text = "we should refactor the memory plane to reduce coupling between modules"
    assert secrets.scan(text).verdict == VERDICT_OK


def test_multiple_findings_all_redacted():
    text = "a=AKIAIOSFODNN7EXAMPLE b=ghp_012345678901234567890123456789012345"
    result = secrets.scan(text)
    assert result.verdict == VERDICT_REJECT
    assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_text
    assert "ghp_012345678901234567890123456789012345" not in result.redacted_text
    assert len(result.findings) >= 2
