# subaco-hive タスクランナー（just で test / lint / fmt を提供）。
# stdlib のみで動く層は外部依存なしで `just test` が緑になる。

# 既定: 一覧表示
default:
    @just --list

# テスト（オフラインでは uv キャッシュの pytest を使う。--no-project でビルドバックエンド不要）。
test:
    uv run --python 3.11 --with pytest --no-project pytest

# lint（ruff）
lint:
    uv run --python 3.11 --with ruff --no-project ruff check .

# format（ruff format）
fmt:
    uv run --python 3.11 --with ruff --no-project ruff format .

# 構文のみの軽量チェック（依存取得なし）
compile:
    python3 -m compileall -q subaco_hive tests
