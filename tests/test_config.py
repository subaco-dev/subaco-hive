"""config モジュールのテスト（stdlib のみ、外部依存なし）。"""

from __future__ import annotations

import pytest

from subaco_hive import config


# --- 名義検証（導出ではなく検証）------------------------------------------------------
@pytest.mark.parametrize("name", ["subaco", "my-team_1", "a", "x" * 64])
def test_valid_names(name):
    assert config.is_valid_name(name)
    assert config.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",  # 空
        "Subaco",  # 大文字
        "my team",  # 空白
        "team!",  # 記号
        "x" * 65,  # 65 文字（上限超過）
        "café",  # 非 ASCII（NFC でも文字集合外）
    ],
)
def test_invalid_names(name):
    assert not config.is_valid_name(name)
    with pytest.raises(config.ConfigError):
        config.validate_name(name, kind="team")


def test_nfd_rejected():
    # NFD（合成前）は NFC != 自身になるため弾かれる。
    nfd = "café"  # e + combining acute
    assert not config.is_valid_name(nfd)


# --- .hive ルート探索--------------------------------------------------------------
def test_find_hive_root_walks_up(tmp_path):
    root = tmp_path / "proj"
    (root / ".hive").mkdir(parents=True)
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    found = config.find_hive_root(start=deep)
    assert found == (root / ".hive").resolve()


def test_find_hive_root_stops_at_git_root(tmp_path):
    # git ルートで打ち切る: .hive はさらに上位にあり、探索は届かない。
    outer = tmp_path / "outer"
    (outer / ".hive").mkdir(parents=True)
    gitroot = outer / "repo"
    (gitroot / ".git").mkdir(parents=True)
    deep = gitroot / "src"
    deep.mkdir(parents=True)
    # gitroot 自身に .hive は無く、境界（.git）で打ち切るため None。
    assert config.find_hive_root(start=deep) is None


def test_find_hive_root_at_git_root_itself(tmp_path):
    # git ルート自身に .hive があれば返す（境界ディレクトリも検査対象）。
    gitroot = tmp_path / "repo"
    (gitroot / ".git").mkdir(parents=True)
    (gitroot / ".hive").mkdir()
    deep = gitroot / "src"
    deep.mkdir()
    assert config.find_hive_root(start=deep) == (gitroot / ".hive").resolve()


def test_find_hive_root_none_when_absent(tmp_path):
    (tmp_path / ".git").mkdir()  # 境界を用意して無限探索を防ぐ
    sub = tmp_path / "x"
    sub.mkdir()
    assert config.find_hive_root(start=sub) is None


# --- DB パス / team の解決 --------------------------------------------------------------------
def test_resolve_db_path_env(monkeypatch, tmp_path):
    db = tmp_path / "custom" / "messages.db"
    monkeypatch.setenv("HIVE_DB_PATH", str(db))
    assert config.resolve_db_path() == db.resolve()


def test_resolve_db_path_from_search(monkeypatch, tmp_path):
    monkeypatch.delenv("HIVE_DB_PATH", raising=False)
    root = tmp_path / "proj"
    (root / ".hive").mkdir(parents=True)
    (root / ".git").mkdir()
    monkeypatch.chdir(root / ".hive")  # .hive 内から上位探索
    # cwd から探索: root/.hive が見つかる。
    assert config.resolve_db_path() == (root / ".hive" / "messages.db").resolve()


def test_resolve_db_path_error_when_no_hive(monkeypatch, tmp_path):
    monkeypatch.delenv("HIVE_DB_PATH", raising=False)
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(config.ConfigError):
        config.resolve_db_path()


def test_resolve_team_from_env(monkeypatch):
    monkeypatch.setenv("HIVE_TEAM", "team-x")
    assert config.resolve_team() == "team-x"


def test_resolve_team_from_env_invalid(monkeypatch):
    monkeypatch.setenv("HIVE_TEAM", "BAD NAME")
    with pytest.raises(config.ConfigError):
        config.resolve_team()


def test_resolve_team_from_file(monkeypatch, tmp_path):
    monkeypatch.delenv("HIVE_TEAM", raising=False)
    hive = tmp_path / ".hive"
    hive.mkdir()
    (hive / "team").write_text("myteam\n", encoding="utf-8")
    assert config.resolve_team(hive_root=hive) == "myteam"


def test_resolve_team_error_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("HIVE_TEAM", raising=False)
    hive = tmp_path / ".hive"
    hive.mkdir()
    with pytest.raises(config.ConfigError):
        config.resolve_team(hive_root=hive)


# --- ログレベル -------------------------------------------------------------------------------
def test_log_level_default(monkeypatch):
    monkeypatch.delenv("HIVE_LOG_LEVEL", raising=False)
    assert config.resolve_log_level() == "info"


def test_log_level_from_env(monkeypatch):
    monkeypatch.setenv("HIVE_LOG_LEVEL", "DEBUG")
    assert config.resolve_log_level() == "debug"


def test_log_level_unknown_falls_back(monkeypatch):
    monkeypatch.setenv("HIVE_LOG_LEVEL", "verbose")
    assert config.resolve_log_level() == "info"
