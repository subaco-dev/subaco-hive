"""models モジュールのテスト（dataclass の健全性）。"""

from __future__ import annotations

from subaco_hive import models


def test_member_defaults():
    m = models.Member(
        team="t", name="agent-1", vendor="claude-code", joined_at="2026-07-08T00:00:00Z"
    )
    assert m.trust_level == models.TRUST_UNTRUSTED
    assert m.token_hash is None
    assert m.id is None


def test_message_broadcast_default():
    msg = models.Message(team="t", sender="a", body="hi", created_at="2026-07-08T00:00:00Z")
    assert msg.recipient is None  # ブロードキャスト
    assert msg.via == models.VIA_CLI


def test_memory_status_default():
    mem = models.Memory(
        id="doc-1",
        team="t",
        author="a",
        kind="finding",
        created_at="2026-07-08T00:00:00Z",
        source_trust=1,
    )
    assert mem.status == models.MEMORY_PENDING


def test_scan_result_reason_codes_dedup():
    findings = [
        models.SecretFinding("aws_access_key_id", "secret.aws_access_key", "high", 0, 10),
        models.SecretFinding("aws_access_key_id", "secret.aws_access_key", "high", 20, 30),
        models.SecretFinding("high_entropy", "secret.high_entropy", "entropy", 40, 60),
    ]
    result = models.ScanResult(verdict=models.VERDICT_REJECT, redacted_text="x", findings=findings)
    assert result.reason_codes() == ["secret.aws_access_key", "secret.high_entropy"]


def test_trust_constants():
    assert (models.TRUST_UNTRUSTED, models.TRUST_NORMAL, models.TRUST_HIGH) == (0, 1, 2)
