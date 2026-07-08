"""秘密パターン検査。

判定は 3 値:
  - reject : 確度の高い正規表現一致（主要 API キー/トークン）。書込は拒否する。
  - redact : 高エントロピー検出。誤検知を考慮して該当部分を赤塗りして書込・読出、警告を返す。
  - ok     : 検出なし。

用途:
  - **書込時**（hive_remember / hive_post）: reject なら書込拒否、redact なら赤塗り本文で書込＋警告。
  - **読出時**（hive_inbox の include_untrusted 本文 / hive_history）: CLI 直書きは書込検査を迂回するため、
    確度一致・高エントロピーいずれも赤塗りして返す（reject でも読出はブロックせず赤塗り）。
本モジュールは判定と赤塗り本文を返すだけで、書込/読出どちらのポリシーを適用するかは呼出側が決める。

audit には本文を残さない。呼出側は ScanResult.reason_codes() のみ記録する。

TODO: パターン網羅とエントロピー閾値は v0 の初期値。運用計測でチューニングする。
      誤検知/取りこぼしの調整は _HIGH_CONFIDENCE_PATTERNS と _ENTROPY_* 定数で行う。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from .models import (
    VERDICT_OK,
    VERDICT_REDACT,
    VERDICT_REJECT,
    ScanResult,
    SecretFinding,
)

# 赤塗り時に本文へ挿入するマスク文字列。理由コードは findings 側に残す（本文には出所を書かない）。
REDACTION_MASK = "[REDACTED]"

# --- 確度の高いパターン（一致＝reject 相当）------------------------------------------
# (kind, reason_code, regex) の並び。reason_code は audit に残す非本文コード。
_HIGH_CONFIDENCE_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    # AWS アクセスキー ID（AKIA/ASIA/AGPA/AIDA/AROA/AIPA/ANPA/ANVA 等の 4 文字プレフィクス + 16 英数）
    (
        "aws_access_key_id",
        "secret.aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b"),
    ),
    # GitHub トークン各種（ghp_/gho_/ghu_/ghs_/ghr_ + 36 文字以上）
    ("github_token", "secret.github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    # GitHub fine-grained PAT
    ("github_pat", "secret.github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    # Anthropic API キー（sk-ant-...）— openai 汎用より先に評価して優先一致させる
    ("anthropic_api_key", "secret.anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    # OpenAI 系 API キー（sk-... / sk-proj-...）
    ("openai_api_key", "secret.openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    # Google API キー（AIza + 35 文字）
    ("google_api_key", "secret.google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # Slack トークン（xoxb-/xoxa-/xoxp-/xoxr-/xoxs-）
    ("slack_token", "secret.slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    # Slack incoming webhook
    (
        "slack_webhook",
        "secret.slack_webhook",
        re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_+-]{20,}"),
    ),
    # Stripe シークレット/リストキー（live）
    ("stripe_secret_key", "secret.stripe_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b")),
    # 秘密鍵 PEM ブロック
    (
        "private_key_block",
        "secret.private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    ),
    # E2B / cube-shim トークン（e2b_<hex32>）
    ("e2b_token", "secret.e2b_token", re.compile(r"\be2b_[0-9a-f]{32}\b")),
    # JWT（3 セグメントの base64url。ヘッダは eyJ で始まる）
    (
        "jwt",
        "secret.jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
)

# --- 高エントロピー検出（一致＝redact 相当）------------------------------------------
# 秘密になり得る連続トークンの候補（base64/hex/トークン文字の連なり）。
_ENTROPY_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
# Shannon エントロピー（bits/char）の下限。ランダム性の高い文字列を拾う。
_ENTROPY_MIN_BITS = 3.5
# 高エントロピーとみなす最小長（英数混在の場合）。
_ENTROPY_MIN_LEN_MIXED = 24
# 16 進のみの場合の最小長（md5/sha 断片等）。
_ENTROPY_MIN_LEN_HEX = 32
_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _shannon_entropy(s: str) -> float:
    """文字列の Shannon エントロピー（bits/char）。"""
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_secretish(token: str) -> bool:
    """トークンが「秘密っぽい」構造か（誤検知抑制のためのゲート）。"""
    is_hex = all(c in _HEX_CHARS for c in token)
    if is_hex and len(token) >= _ENTROPY_MIN_LEN_HEX:
        return True
    has_lower = any(c.islower() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_digit = any(c.isdigit() for c in token)
    classes = sum((has_lower, has_upper, has_digit))
    # 英数の 2 クラス以上が混在し、十分に長い場合のみ候補にする（通常の英単語列を弾く）。
    return len(token) >= _ENTROPY_MIN_LEN_MIXED and classes >= 2


def _merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """[start,end) 区間を昇順ソートして重なりを併合する（赤塗りの二重適用を避ける）。"""
    ordered = sorted(spans)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _apply_redaction(text: str, spans: list[tuple[int, int]]) -> str:
    """併合済み span を後ろから REDACTION_MASK で置換する。"""
    if not spans:
        return text
    out = text
    for start, end in sorted(spans, reverse=True):
        out = out[:start] + REDACTION_MASK + out[end:]
    return out


def scan(text: str) -> ScanResult:
    """本文を検査し、判定（ok/redact/reject）・赤塗り本文・検出一覧を返す。

    - 確度の高いパターン一致が 1 つでもあれば verdict=reject（書込側はこれで拒否）。
    - reject が無く高エントロピー検出があれば verdict=redact。
    - どちらも無ければ verdict=ok。
    - redacted_text は **確度一致・高エントロピーの両方**をマスクした本文
      （読出経路はこの本文を使う。reject でも読出はブロックせず赤塗りする）。
    """
    findings: list[SecretFinding] = []
    high_spans: list[tuple[int, int]] = []

    # 1) 確度の高いパターン。
    for kind, reason_code, pattern in _HIGH_CONFIDENCE_PATTERNS:
        for m in pattern.finditer(text):
            findings.append(
                SecretFinding(
                    kind=kind,
                    reason_code=reason_code,
                    confidence="high",
                    start=m.start(),
                    end=m.end(),
                )
            )
            high_spans.append((m.start(), m.end()))

    merged_high = _merge_spans(high_spans)

    # 2) 高エントロピー検出（確度一致の span と重なるものは二重計上しない）。
    entropy_spans: list[tuple[int, int]] = []
    for m in _ENTROPY_CANDIDATE_RE.finditer(text):
        span = (m.start(), m.end())
        if _covered_by(span, merged_high):
            continue
        token = m.group(0)
        if _looks_secretish(token) and _shannon_entropy(token) >= _ENTROPY_MIN_BITS:
            findings.append(
                SecretFinding(
                    kind="high_entropy",
                    reason_code="secret.high_entropy",
                    confidence="entropy",
                    start=span[0],
                    end=span[1],
                )
            )
            entropy_spans.append(span)

    # 判定（書込セマンティクス基準）。
    if any(f.confidence == "high" for f in findings):
        verdict = VERDICT_REJECT
    elif findings:
        verdict = VERDICT_REDACT
    else:
        verdict = VERDICT_OK

    redacted = _apply_redaction(text, _merge_spans(high_spans + entropy_spans))
    return ScanResult(verdict=verdict, redacted_text=redacted, findings=findings)


def _covered_by(span: tuple[int, int], covers: list[tuple[int, int]]) -> bool:
    """span が covers のいずれかと重なるか（重なれば True）。"""
    s, e = span
    return any(s < ce and cs < e for cs, ce in covers)


# --- 呼出側向けの薄いヘルパー（ポリシー適用の分岐を明示）----------------------------------------
def allowed_for_write(text: str) -> tuple[bool, ScanResult]:
    """書込可否と検査結果を返す（書込ポリシー）。

    - reject → (False, result)。呼出側は書込を拒否し、result.reason_codes() を audit へ。
    - redact → (True, result)。呼出側は result.redacted_text を書き込み、警告を返す。
    - ok     → (True, result)。原文を書き込む。
    """
    result = scan(text)
    return (result.verdict != VERDICT_REJECT, result)


def redact_for_read(text: str) -> ScanResult:
    """読出経路（include_untrusted 本文 / hive_history）で使う。常に赤塗り本文を得る。

    返り値の redacted_text をそのまま返却本文に使う（reject 相当でも読出はブロックしない）。
    """
    return scan(text)
