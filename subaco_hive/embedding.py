"""埋め込みプロバイダ抽象と整合管理（「埋め込みモデルの整合」）。

- `EmbeddingProvider` ABC に 2 実装: `FastEmbedProvider`（既定・ローカル、fastembed を**遅延 import**）と
  `OpenAICompatProvider`（OpenAI 互換 /v1/embeddings、stdlib urllib で HTTP）。
- モデル名／次元を hive_meta へ永続化し（db.init_db が初期書込、本モジュールが再確認）、ライター起動時に照合する。
  不一致時は「メッセージング系は維持・記憶系ツールのみ拒否」（呼び出し側が db.SchemaError を捕捉して分岐）。
- モデル切替は環境変数ではなく `hive reembed` の原子的スワップで行う。本モジュールに `reembed` を持つ。

**依存が import できなくてもモジュール自体は import 可能**。fastembed / ネットワークは遅延・実行時のみ触れる。
次元は既知モデルの `KNOWN_DIMS` で解決し、未知モデルのみ実ロードで probe する（init 時に重い依存を避ける）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from . import db
from .logging import get_logger

_log = get_logger(__name__)

# 既定ローカルモデル（多言語小型）。日本語主体のため多言語対応を選ぶ（最終選定は M1-5 の簡易ベンチで確定）。
#
# Zvec spike と同時に fastembed 0.8 の `TextEmbedding.list_supported_models()` を実測した結果、
# `intfloat/multilingual-e5-small` は **fastembed が対応していない**（-large のみ提供）ため、
# 日本語を扱える小型モデルとして paraphrase-multilingual-MiniLM-L12-v2（384 次元・約 0.22GB）を既定にする。
# fastembed が対応する多言語モデルは実測時点で次の 3 つのみ:
#   sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2  384 次元 / 0.22GB（既定）
#   sentence-transformers/paraphrase-multilingual-mpnet-base-v2  768 次元 / 1.0GB
#   intfloat/multilingual-e5-large                              1024 次元 / 2.24GB
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# 実ロードせず次元を返すための既知モデル表（M1-5 のベンチで確定・追補する）。
# fastembed 側の対応状況は `TextEmbedding.list_supported_models()` が正典。
KNOWN_DIMS: dict[str, int] = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": 768,
    "intfloat/multilingual-e5-large": 1024,
    "BAAI/bge-small-en-v1.5": 384,
    # OpenAI 互換（API 型）
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}


class EmbeddingError(Exception):
    """埋め込み生成の失敗（依存未導入・API エラー等）。記憶系のみ fail する。"""


class EmbeddingProvider(ABC):
    """埋め込みプロバイダの共通インターフェース。実装は fastembed / OpenAI 互換 API。"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """hive_meta へ永続化し起動時照合に使うモデル識別子。"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """ベクタ次元。コレクション作成に用いる。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """複数テキストの密ベクタを返す（順序保存）。"""

    def embed_query(self, text: str) -> list[float]:
        """単一クエリの密ベクタ（recall 用の薄いラッパ）。"""
        return self.embed([text])[0]


class FastEmbedProvider(EmbeddingProvider):
    """ローカル埋め込み（fastembed）。fastembed は**遅延 import**（未導入でも本クラスは import 可能）。"""

    def __init__(
        self, model_name: str = DEFAULT_FASTEMBED_MODEL, *, dim: int | None = None
    ) -> None:
        self._model_name = model_name
        self._dim = dim if dim is not None else KNOWN_DIMS.get(model_name)
        self._model = None  # 遅延ロード

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        if self._dim is None:
            # 既知でないモデルは実ロードして probe（重い依存に初めて触れる）。
            self._dim = len(self.embed(["dimension probe"])[0])
        return int(self._dim)

    def _ensure_model(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding  # 遅延 import
            except ImportError as exc:  # pragma: no cover - 依存未導入時のみ
                raise EmbeddingError(
                    "fastembed が未導入です。`pip install 'subaco-hive[memory]'` で記憶系を有効化してください。"
                ) from exc
            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        # fastembed は generator を返す。list[float] へ正規化する。
        return [list(map(float, vec)) for vec in model.embed(list(texts))]


class OpenAICompatProvider(EmbeddingProvider):
    """OpenAI 互換 /v1/embeddings 実装（stdlib urllib で HTTP）。API キーは環境変数から取得。

    base_url 既定は OpenAI 本家。互換エンドポイント（自ホスト等）に向ける場合は base_url を渡す。
    切替は環境変数の直変更ではなく `hive reembed` を経由すること（ここは provider 実体のみ）。
    """

    def __init__(
        self,
        model_name: str = "text-embedding-3-small",
        *,
        dim: int | None = None,
        base_url: str = "https://api.openai.com/v1",
        api_key_env: str = "OPENAI_API_KEY",
        timeout: float = 30.0,
    ) -> None:
        self._model_name = model_name
        self._dim = dim if dim is not None else KNOWN_DIMS.get(model_name)
        self._base_url = base_url.rstrip("/")
        self._api_key_env = api_key_env
        self._timeout = timeout

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return int(self._dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise EmbeddingError(
                f"環境変数 {self._api_key_env} が未設定です（OpenAI 互換埋め込みに必要）。"
            )
        payload = json.dumps({"model": self._model_name, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - ネットワーク依存
            raise EmbeddingError(f"埋め込み API 呼び出しに失敗しました: {exc}") from exc
        items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        return [list(map(float, item["embedding"])) for item in items]


# ---- ファクトリ / 整合 -------------------------------------------------------------------------
def build_provider(
    kind: str | None = None,
    *,
    model: str | None = None,
    dim: int | None = None,
    **kwargs,
) -> EmbeddingProvider:
    """プロバイダを構築する。既定は fastembed（ローカル）。kind='openai' で API 型。

    kind 未指定時は環境変数 SUBACO_EMBEDDING_PROVIDER（あれば）、無ければ 'fastembed'。
    モデル切替の正規手段は `hive reembed`。ここはあくまで provider 実体の生成のみ。
    """
    kind = (kind or os.environ.get("SUBACO_EMBEDDING_PROVIDER") or "fastembed").lower()
    if kind in ("openai", "openai-compat", "api"):
        return OpenAICompatProvider(model or "text-embedding-3-small", dim=dim, **kwargs)
    return FastEmbedProvider(model or DEFAULT_FASTEMBED_MODEL, dim=dim)


def persist_model(conn, provider: EmbeddingProvider) -> None:
    """モデル名／次元を hive_meta へ書き込む（init_db が初期書込。ここは明示再書込用）。"""
    db.set_meta(conn, db.META_EMBEDDING_MODEL, provider.model_name)
    db.set_meta(conn, db.META_EMBEDDING_DIM, str(int(provider.dim)))


def verify(conn, provider: EmbeddingProvider) -> None:
    """起動時照合。不一致は db.SchemaError（呼び出し側が記憶系のみ拒否に分岐する）。"""
    db.verify_embedding(conn, model=provider.model_name, dim=provider.dim)


# ---- reembed（原子的スワップ）----------------------------------------------------------
@runtime_checkable
class ReembedStore(Protocol):
    """reembed が要求する Zvec コレクション操作の最小インターフェース（memory 層が実装）。

    循環 import を避けるため memory の具象 store を Protocol として受け取る。
    """

    def create_collection(self, name: str, dim: int) -> None: ...
    def drop_collection(self, name: str) -> None: ...
    def iter_committed_bodies(self):  # -> Iterable[tuple[memory_id, text, kind, author, trust]]
        ...
    def insert_vectors(self, name: str, records) -> None: ...


def temp_collection_name(base: str, avoid: str | None = None) -> str:
    """reembed 用の一時コレクション名（例 `hive_team__reembed_1700000000`）。

    サフィックスは秒解像度のため、同一秒内の再実行では直前の一時名（= 現 active_collection）と
    衝突して new == old になり得る。avoid と一致する間は +1 秒ずらして回避する
    （サフィックスは db.collection_name_for が確保する 21 字予算に収まったまま）。
    """
    ts = int(time.time())
    name = f"{base}__reembed_{ts}"
    while name == avoid:
        ts += 1
        name = f"{base}__reembed_{ts}"
    return name


def reembed(conn, store: ReembedStore, provider: EmbeddingProvider) -> str:
    """モデル切替の原子的スワップ。新 active_collection 名を返す。

    手順:
      (1) 一時コレクションを新モデル次元で構築し、SQLite の committed 本文を新モデルで再埋め込みして insert。
      (2) 完了後に hive_meta.active_collection を一時名へアトミック切替＋embedding_model/dim を更新。
      (3) 切替後に旧コレクションを削除。
    失敗時は hive_meta を更新しない（ロールバック）。中断の一時コレクション残骸はライター起動時の孤児掃除で除去。
    本文を Zvec から読めない構成では SQLite（memories.body 追加案）を本文正典として読む。
    """
    old = db.active_collection(conn)
    new = temp_collection_name(f"hive_{_base_team(old)}", avoid=old)
    _log.info(
        "reembed 開始: old=%s new=%s model=%s dim=%s", old, new, provider.model_name, provider.dim
    )

    # (1) 一時コレクション構築＋再埋め込み。
    store.create_collection(new, provider.dim)
    records = []
    for mem_id, text, kind, author, trust in store.iter_committed_bodies():
        vec = provider.embed_query(text)
        records.append((mem_id, vec, text, kind, author, trust))
    if records:
        store.insert_vectors(new, records)

    # (2) hive_meta をアトミックに切替（active_collection + model/dim）。ここまで来て初めて可視化する。
    # set_meta は呼び出しごとに commit するため使わない（3 回の独立 COMMIT になり、途中停止で
    # collection / model / dim が不一致になる——レビュー指摘）。単一トランザクション版を使う。
    db.set_meta_many(
        conn,
        {
            db.META_ACTIVE_COLLECTION: new,
            db.META_EMBEDDING_MODEL: provider.model_name,
            db.META_EMBEDDING_DIM: str(int(provider.dim)),
        },
    )

    # (3) 旧コレクション削除（切替後）。削除失敗は致命ではない（孤児掃除で回収）。
    try:
        store.drop_collection(old)
    except Exception as exc:  # pragma: no cover
        _log.warning("旧コレクション削除に失敗（孤児掃除に委譲）: %s: %s", old, exc)
    _log.info("reembed 完了: active_collection=%s", new)
    return new


def _base_team(collection: str) -> str:
    """`hive_{team}` / `hive_{team}__reembed_…` から team 部分を取り出す（一時名の付け直し用）。"""
    name = collection
    if "__reembed_" in name:
        name = name.split("__reembed_")[0]
    if name.startswith("hive_"):
        name = name[len("hive_") :]
    return name
