"""環境設定の解決。

重要な設計原則:
  - **チーム名の導出はここでは行わない。** 導出（NFC 正規化・小文字化・置換・64 字上限）は
    devShell の `hive-team` ヘルパーが唯一実装し `.hive/team` に永続化する。
    subaco_hive は `HIVE_TEAM`（環境変数）か `.hive/team`（ファイル）を**読むだけ**。
  - ただし team / name は「検証」する（文字集合 `[a-z0-9_-]`・NFC・64 字上限）。
    検証は導出ではなく、不正値を弾くための入力バリデーション。
  - `HIVE_DB_PATH` 未設定時は CWD から上位へ既存の `.hive/` を探索し、
    git リポジトリルートまたは $HOME で打ち切る。見つからなければエラー（暗黙作成しない）。
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

# 名義の文字集合制約。導出規則の結果と同一の文字集合を「検証」に用いる。
MAX_NAME_LEN = 64
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

DEFAULT_LOG_LEVEL = "info"
_VALID_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})


class ConfigError(Exception):
    """設定解決の失敗（未設定・不在・不正値）。呼び出し側で fail-closed に扱う。"""


# --- team / name の検証（導出ではない）--------------------------------------------------
def is_valid_name(value: str) -> bool:
    """team / name が文字集合制約（NFC・`[a-z0-9_-]`・1..64 字）を満たすか。"""
    if not value:
        return False
    if unicodedata.normalize("NFC", value) != value:
        # macOS の NFD ファイル名等との照合差異を弾く（NFC を採用）。
        return False
    return bool(_NAME_RE.match(value))


def validate_name(value: str, *, kind: str = "name") -> str:
    """検証に通れば value をそのまま返し、通らなければ ConfigError。

    `kind` はエラーメッセージ用のラベル（"team" / "name" 等）。
    ここでは正規化（導出）を行わない — 不正値は「弾く」のが方針。
    """
    if not is_valid_name(value):
        raise ConfigError(
            f"{kind} 名が不正です: {value!r}（許可: NFC・小文字英数と `_` `-`・1〜{MAX_NAME_LEN} 文字）。"
            " 正規化は devShell の hive-team が行う（subaco_hive は検証のみ）。"
        )
    return value


# --- .hive ルート探索--------------------------------------------------------------
def find_hive_root(start: Path | None = None) -> Path | None:
    """`start`（既定 CWD）から上位方向へ既存の `.hive/` ディレクトリを探索して返す。

    打ち切り境界: git リポジトリルート（`.git` を含むディレクトリ）または $HOME。
    これらのディレクトリ自身は探索対象に含め（`.hive` があれば返す）、その後は上位へ進まない。
    見つからなければ None（呼び出し側が暗黙作成せずエラーにする）。
    """
    cur = (start or Path.cwd()).resolve()
    try:
        home = Path.home().resolve()
    except (RuntimeError, OSError):
        home = None

    while True:
        candidate = cur / ".hive"
        if candidate.is_dir():
            return candidate
        # 境界に達したら（このディレクトリを検査し終えた後）打ち切る。
        at_git_root = (cur / ".git").exists()
        at_home = home is not None and cur == home
        at_fs_root = cur.parent == cur
        if at_git_root or at_home or at_fs_root:
            return None
        cur = cur.parent


def resolve_hive_root() -> Path:
    """有効な `.hive/` ルートを確定して返す。

    HIVE_DB_PATH が設定されていればその親ディレクトリを、無ければ探索結果を用いる。
    見つからなければ ConfigError（暗黙作成しない）。
    """
    db_env = os.environ.get("HIVE_DB_PATH")
    if db_env:
        return Path(db_env).expanduser().resolve().parent
    root = find_hive_root()
    if root is None:
        raise ConfigError(
            ".hive/ が見つかりません（CWD から git ルート/$HOME まで探索）。"
            " HIVE_DB_PATH を設定するか、.envrc / bootstrap で .hive/ を初期化してください"
            "（subaco_hive は暗黙に作成しません）。"
        )
    return root


def resolve_db_path() -> Path:
    """SQLite messages.db の絶対パスを解決する（既定 `<.hive>/messages.db`）。

    HIVE_DB_PATH があればそれを、無ければ探索で確定した `.hive/` 直下の messages.db。
    """
    db_env = os.environ.get("HIVE_DB_PATH")
    if db_env:
        return Path(db_env).expanduser().resolve()
    return resolve_hive_root() / "messages.db"


# --- team の解決（読むだけ）------------------------------------------------------------
def resolve_team(hive_root: Path | None = None) -> str:
    """チーム名を解決する。導出はせず、HIVE_TEAM か `.hive/team` を読むだけ。

    優先順位:
      1. 環境変数 HIVE_TEAM
      2. `<hive_root>/team` ファイルの内容（hive-team ヘルパーが生成）
    いずれも無ければ ConfigError（暗黙生成しない）。読んだ値は検証する。
    """
    team_env = os.environ.get("HIVE_TEAM")
    if team_env:
        return validate_name(team_env.strip(), kind="team")

    root = hive_root if hive_root is not None else resolve_hive_root()
    team_file = root / "team"
    if team_file.is_file():
        content = team_file.read_text(encoding="utf-8").strip()
        if content:
            return validate_name(content, kind="team")

    raise ConfigError(
        f"HIVE_TEAM が未設定で {team_file} も不在/空です。"
        " .envrc / bootstrap が hive-team でチーム名を生成・永続化する前提です。"
    )


# --- ログレベル--------------------------------------------------------------
def resolve_log_level() -> str:
    """HIVE_LOG_LEVEL を解決する（既定 info）。未知値は info にフォールバック（fail-open な診断）。"""
    raw = os.environ.get("HIVE_LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().lower()
    if raw not in _VALID_LOG_LEVELS:
        return DEFAULT_LOG_LEVEL
    return raw
