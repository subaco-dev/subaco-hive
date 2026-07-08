"""共有フィクスチャ（stdlib のみ）。

記憶層テスト用に、fastembed / zvec を使わない決定的な埋め込みプロバイダと、
純 Python の InMemoryVectorBackend を使う（外部依存なしで記憶系の SQLite 状態機械を検証する）。
"""

from __future__ import annotations

import pytest

from subaco_hive import db


class FakeProvider:
    """決定的な埋め込み（語トークンを固定次元へハッシュ）。EmbeddingProvider を duck-typing で満たす。"""

    def __init__(self, dim: int = 16) -> None:
        self._dim = dim

    @property
    def model_name(self) -> str:
        return "fake"

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)

    def _vec(self, text: str):
        v = [0.0] * self._dim
        for tok in text.lower().split():
            v[hash(tok) % self._dim] += 1.0
        return v


@pytest.fixture
def conn(tmp_path):
    """初期化済み SQLite 接続（embedding_model=fake, dim=16）。"""
    c = db.init_db(tmp_path / "messages.db", "alpha", embedding_model="fake", embedding_dim=16)
    yield c
    c.close()


@pytest.fixture
def provider():
    return FakeProvider(dim=16)
