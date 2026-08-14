"""長期記憶（Zvec 統合）のロジック。

hive_remember の二段書き・起動時孤児掃除・hive_recall の固定フィルタ・出所ラベル・本文デリミタを実装する。
ベクタ検索は `VectorBackend` インターフェース越しに行い、Zvec を**遅延 import**する `ZvecBackend` を既定にする
（Zvec 未導入でも本モジュールは import 可能）。テスト／dev 用に純 Python の
`InMemoryVectorBackend`（総当たりコサイン）も同梱し、SQLite 側の状態機械（pending→committed・孤児掃除・
固定フィルタ）を外部依存なしで実行・検証可能にする。

二段書きの整合:
  (1) memories へ status='pending' で INSERT（コミット）
  (2) Zvec insert（ベクタ＋スカラー kind/author/trust/text）
  (3) status='committed' 更新と processed_requests 記録を**同一トランザクション**で確定（pending 時は記録しない）
起動時の孤児掃除は「pending 行＋対応ベクタの削除。processed_requests に残さない」（プロキシ再送の受付開始前に完了）。

recall の固定フィルタ: source_trust>=1 かつ 著者の現在 members.trust_level>=1 の AND。v0 は未信頼記憶の取得手段なし。
正典は SQLite（memories）。Zvec スカラーは検索効率のための重複保持。

TODO（Zvec spike）: 本文 text を Zvec に保存・全文取得できない構成が判明した場合は memories に `body`
カラムを追加し SQLite を本文の正典に切り替える（本モジュールの `_read_text` を memories.body 参照へ差し替える）。
Zvec の具体 API（CollectionSchema / insert / hybrid search）は spike で確定するまで ZvecBackend 内に隔離する。
"""

from __future__ import annotations

import re
import shutil
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import config, db
from .logging import get_logger
from .messaging import Session, member_trust, wrap_body
from .models import MEMORY_COMMITTED, MEMORY_PENDING, TRUST_NORMAL
from .secrets import allowed_for_write

_log = get_logger(__name__)

#: ベクタコレクションを置く `.hive/` 直下のディレクトリ名。
#: `hive admin backup` / `restore` がスナップショット対象とする単位でもあるため、
#: レイアウトの正典はここに一本化する（cli はこの定数を import する）。
MEMORY_DIRNAME = "memory"


def _now_iso(now: str | None = None) -> str:
    return now if now is not None else datetime.now(UTC).isoformat()


# ---- ベクタバックエンド抽象 --------------------------------------------------------------------
@dataclass(slots=True)
class SearchHit:
    """ベクタ検索の 1 ヒット（正典照合前の候補）。id は memories.id と一致。"""

    id: str
    score: float
    text: str
    kind: str
    author: str
    trust: int


@runtime_checkable
class VectorBackend(Protocol):
    """Zvec 等のベクタストアに対する最小インターフェース（reembed の ReembedStore も満たす）。"""

    def has_collection(self, name: str) -> bool: ...
    def create_collection(self, name: str, dim: int) -> None: ...
    def drop_collection(self, name: str) -> None: ...
    def list_collections(self) -> list[str]: ...
    def insert(
        self,
        name: str,
        *,
        id: str,
        vector: list[float],
        text: str,
        kind: str,
        author: str,
        trust: int,
    ) -> None: ...
    def delete(self, name: str, ids: list[str]) -> None: ...
    def get_text(self, name: str, id: str) -> str | None: ...
    def search(
        self, name: str, *, vector: list[float], top_k: int, kind: str | None = None
    ) -> list[SearchHit]: ...


class ZvecBackend:
    """Zvec 実装（**遅延 import**）。zvec の具体 API との差異を本クラスに隔離する。

    Zvec spike（計画書 §5 第一）で確定した実挙動（zvec 0.6 / macOS arm64・manylinux wheel）:

    - コレクションは**ディレクトリ**（`zvec.create_and_open(path, schema)` / `zvec.open(path)`）。
      本バックエンドは `<.hive>/{MEMORY_DIRNAME}/<name>` に置き、`name` はコレクション名と同一にする
      （`hive admin backup` / `restore` がこのディレクトリ丸ごとをスナップショットする）。
    - コレクション名の制約は `[A-Za-z0-9_-]{3,64}`（`.` `/`・非 ASCII は不可）。
      `hive_{team}` と reembed 一時名がこの上限に収まることは `db.collection_name_for` が保証する。
    - 本文（`text`）は STRING スカラーとして保存でき、`query(output_fields=...)` / `fetch()` で
      **全文を欠落なく取得**できる（2000 字の日本語で往復確認済み）。よって memories への
      `body` 追加（設計書 §4.2 の分岐）は**不要**。
    - 既定メトリックは IP。`MetricType.COSINE` を指定した場合 `Doc.score` は**コサイン距離**
      （小さいほど近い）で返るため、`SearchHit.score` へは `1 - distance` の**類似度**に変換する
      （`InMemoryVectorBackend` と昇順・降順の向きを揃える）。
    - `filter` は SQL 風の式（`kind = 'note'`。`==` は構文エラー）。文字列リテラルのエスケープ手段が
      無い（`''` も構文エラー）ため、押し下げは `_kind_filter` が安全な値に限り、絞り込みの正しさは
      Python 側の突き合わせで担保する。
    - 診断ログは **stderr** へ出る（stdout は JSON-RPC 専有という MCP の前提と両立する）。
      `zvec.init()` は呼ばない（既定 `log_dir='./logs'` でプロジェクトに `logs/` を作らせないため）。
    - プロセスが SIGKILL されても `LOCK` は OS が解放し、別プロセスが即座に書き込みモードで
      再オープンできる（フェイルオーバーの前提が成立する。手動の残留ロック解放は不要）。
    - 逆に**稼働中のライターがいる間は、別プロセスからの read-only オープンも失敗する**。
      バックアップは常駐ライター経由（管理チャネル）で行うという設計書 §4.6 の前提が必須。
    """

    #: zvec のコレクション名制約（spike で実測）。
    NAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")

    def __init__(self, root: str | None = None) -> None:
        self._zvec = None
        self._root = root  # None なら初回利用時に <.hive>/memory（MEMORY_DIRNAME）を解決する
        self._collections: dict[str, object] = {}

    def _ensure_zvec(self):
        if self._zvec is None:
            try:
                import zvec  # 遅延 import
            except ImportError as exc:  # pragma: no cover - 依存未導入時のみ
                raise RuntimeError(
                    "zvec が未導入です。`pip install 'subaco-hive[memory]'` で記憶系を有効化してください。"
                ) from exc
            self._zvec = zvec
        return self._zvec

    # -- パス解決 -------------------------------------------------------------------------------
    def _root_path(self) -> Path:
        if self._root is None:
            self._root = str(config.resolve_hive_root() / MEMORY_DIRNAME)
        root = Path(self._root)
        # `.hive/` と同じく 0700（同一 UID 以外に読ませない）。
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def _path(self, name: str) -> str:
        return str(self._root_path() / self._checked_name(name))

    @classmethod
    def _checked_name(cls, name: str) -> str:
        """zvec のコレクション名制約を満たすか検証する（パス要素にもなるため区切り文字も弾く）。"""
        if not cls.NAME_RE.match(name):
            raise ValueError(
                f"コレクション名が Zvec の制約に反します: {name!r}"
                "（許可: 英数と `_` `-`・3〜64 文字）。"
            )
        return name

    # -- コレクション操作 -----------------------------------------------------------------------
    def _schema(self, name: str, dim: int):
        z = self._ensure_zvec()
        return z.CollectionSchema(
            name=name,
            fields=[
                # 本文。検索対象ではなく取得対象なのでインデックスは張らない。
                z.FieldSchema("text", z.DataType.STRING),
                # kind / trust は絞り込みに使うため転置インデックスを張る。
                z.FieldSchema("kind", z.DataType.STRING, index_param=z.InvertIndexParam()),
                z.FieldSchema("author", z.DataType.STRING),
                z.FieldSchema("trust", z.DataType.INT32, index_param=z.InvertIndexParam()),
            ],
            vectors=[
                z.VectorSchema(
                    "vector",
                    z.DataType.VECTOR_FP32,
                    dimension=int(dim),
                    index_param=z.HnswIndexParam(metric_type=z.MetricType.COSINE),
                )
            ],
        )

    def _open(self, name: str):
        """開いているコレクションを返す（未オープンなら開く）。存在しなければ KeyError。"""
        cached = self._collections.get(name)
        if cached is not None:
            return cached
        z = self._ensure_zvec()
        path = self._path(name)
        if not Path(path).is_dir():
            raise KeyError(f"コレクションが存在しません: {name}")
        coll = z.open(path)
        self._collections[name] = coll
        return coll

    def has_collection(self, name: str) -> bool:
        try:
            return Path(self._path(name)).is_dir()
        except ValueError:
            return False

    def create_collection(self, name: str, dim: int) -> None:
        z = self._ensure_zvec()
        path = self._path(name)
        self._collections[name] = z.create_and_open(path, self._schema(name, dim))

    def drop_collection(self, name: str) -> None:
        coll = self._collections.pop(name, None)
        if coll is None and self.has_collection(name):
            try:
                coll = self._open(name)
                self._collections.pop(name, None)
            except Exception:  # pragma: no cover - 破損コレクションは削除だけ試みる
                coll = None
        if coll is not None:
            try:
                coll.destroy()
            except Exception as exc:  # pragma: no cover - 実体依存
                _log.warning("コレクション destroy に失敗（ディレクトリ削除で継続）: %s", exc)
            del coll
        shutil.rmtree(Path(self._path(name)), ignore_errors=True)

    def list_collections(self) -> list[str]:
        return sorted(
            p.name for p in self._root_path().iterdir() if p.is_dir() and self.NAME_RE.match(p.name)
        )

    # -- ドキュメント操作 -----------------------------------------------------------------------
    def insert(self, name, *, id, vector, text, kind, author, trust) -> None:
        z = self._ensure_zvec()
        coll = self._open(name)
        # upsert: フェイルオーバー再送で同一 id が再投入されても重複させない（冪等）。
        status = coll.upsert(
            z.Doc(
                id=id,
                vectors={"vector": [float(x) for x in vector]},
                fields={
                    "text": text,
                    "kind": kind,
                    "author": author,
                    "trust": int(trust),
                },
            )
        )
        _raise_if_failed(status, f"insert({name}, id={id})")
        # 二段書きの (2) を耐クラッシュにする: committed 更新前にベクタを永続化しておく。
        coll.flush()

    def delete(self, name: str, ids: list[str]) -> None:
        if not ids:
            return
        coll = self._open(name)
        statuses = coll.delete(list(ids))
        if not isinstance(statuses, list):
            statuses = [statuses]
        for st in statuses:
            # 孤児掃除では「そもそも入っていない」が正常（NOT_FOUND は無視する）。
            if not st.ok() and "NOT_FOUND" not in str(st.code()):
                raise RuntimeError(f"Zvec delete に失敗しました（{name}）: {st.message()}")
        coll.flush()

    def get_text(self, name: str, id: str) -> str | None:
        try:
            coll = self._open(name)
        except KeyError:
            return None
        docs = coll.fetch([id], output_fields=["text"], include_vector=False)
        doc = docs.get(id)
        return None if doc is None else doc.fields.get("text")

    def search(self, name, *, vector, top_k, kind=None) -> list[SearchHit]:
        z = self._ensure_zvec()
        try:
            coll = self._open(name)
        except KeyError:
            return []
        top_k = max(0, int(top_k))
        if top_k == 0:
            return []
        query = z.Query("vector", vector=[float(x) for x in vector])
        zfilter = _kind_filter(kind)
        # 押し下げできない kind（式に埋め込めない文字——日本語 kind 等）は Python 側で突き合わせるが、
        # 単発の topk=top_k 取得では「kind 一致の上位 top_k」ではなく「全体上位 top_k の中の一致分」に
        # なり取りこぼす（多数派 kind が上位を占めると、実在する記憶が 0 件で返る）。一致が top_k 件
        # 揃うか全件を読み尽くすまで topk を段階拡大し、InMemoryVectorBackend と再現率を揃える。
        need_escalation = kind is not None and zfilter is None
        fetch_n = top_k * 4 if need_escalation else top_k
        while True:
            docs = coll.query(
                query,
                topk=fetch_n,
                filter=zfilter,
                include_vector=False,
                output_fields=["text", "kind", "author", "trust"],
            )
            hits = [
                SearchHit(
                    id=d.id,
                    # COSINE の score は距離。類似度（大きいほど近い）へ揃える。
                    score=1.0 - float(d.score),
                    text=d.fields.get("text") or "",
                    kind=d.fields.get("kind") or "",
                    author=d.fields.get("author") or "",
                    trust=int(d.fields.get("trust") or 0),
                )
                for d in docs
            ]
            # kind はエージェント入力のため、押し下げの有無によらず Python 側でも必ず突き合わせる
            # （式インジェクション対策——_kind_filter 参照）。
            if kind is not None:
                hits = [h for h in hits if h.kind == kind]
            exhausted = len(docs) < fetch_n
            if not need_escalation or len(hits) >= top_k or exhausted:
                return hits[:top_k]
            fetch_n *= 4

    def close(self) -> None:
        """開いているコレクションを閉じて LOCK を解放する（フェイルオーバー・テスト用）。"""
        self._collections.clear()


def _raise_if_failed(status, what: str) -> None:
    """zvec の Status（`ok()` / `code()` / `message()` はメソッド）を検査して失敗なら送出する。"""
    items = status if isinstance(status, list) else [status]
    for st in items:
        if not st.ok():
            raise RuntimeError(f"Zvec {what} に失敗しました: {st.code()} {st.message()}")


#: filter 式へ**そのまま**埋め込んでよい値の文字集合（引用符・空白・演算子を含まない）。
_PUSHDOWN_SAFE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _kind_filter(kind: str | None) -> str | None:
    """kind を Zvec の filter 式へ押し下げる。押し下げられない値なら None を返す。

    kind はエージェント入力であり、そのまま連結すると filter 式を壊せる（式インジェクション）。
    Zvec の filter 方言には移植性のあるエスケープが無く、`''` による二重化は構文エラーになる
    （spike で実測）。そこで**安全な文字集合のときだけ押し下げ**、それ以外は押し下げを諦めて
    呼び出し側の Python 突き合わせに委ねる。押し下げは最適化であり、正しさの根拠ではない。
    """
    if kind is None or not _PUSHDOWN_SAFE_RE.match(kind):
        return None
    return f"kind = '{kind}'"


class InMemoryVectorBackend:
    """純 Python の総当たりコサイン検索（dev / テスト用）。Zvec 未導入でも記憶系の SQLite 状態機械を動かせる。

    永続化しない（プロセス内のみ）。本番は ZvecBackend。「stdlib 層は実行可能」を記憶層でも満たす。
    """

    def __init__(self) -> None:
        # name -> id -> record dict
        self._store: dict[str, dict[str, dict]] = {}

    def has_collection(self, name: str) -> bool:
        return name in self._store

    def create_collection(self, name: str, dim: int) -> None:
        self._store.setdefault(name, {})

    def drop_collection(self, name: str) -> None:
        self._store.pop(name, None)

    def list_collections(self) -> list[str]:
        return list(self._store.keys())

    def insert(self, name, *, id, vector, text, kind, author, trust) -> None:
        self._store.setdefault(name, {})[id] = {
            "vector": list(map(float, vector)),
            "text": text,
            "kind": kind,
            "author": author,
            "trust": int(trust),
        }

    def delete(self, name: str, ids: list[str]) -> None:
        coll = self._store.get(name, {})
        for i in ids:
            coll.pop(i, None)

    def get_text(self, name: str, id: str) -> str | None:
        rec = self._store.get(name, {}).get(id)
        return rec["text"] if rec else None

    def search(self, name, *, vector, top_k, kind=None) -> list[SearchHit]:
        coll = self._store.get(name, {})
        hits: list[SearchHit] = []
        for mem_id, rec in coll.items():
            if kind is not None and rec["kind"] != kind:
                continue
            hits.append(
                SearchHit(
                    id=mem_id,
                    score=_cosine(vector, rec["vector"]),
                    text=rec["text"],
                    kind=rec["kind"],
                    author=rec["author"],
                    trust=rec["trust"],
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[: max(0, int(top_k))]


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ---- 結果型 -----------------------------------------------------------------------------------
@dataclass(slots=True)
class RememberResult:
    accepted: bool
    memory_id: str | None
    reused: bool
    verdict: str
    warnings: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    args_summary: str = ""


@dataclass(slots=True)
class RecallEntry:
    memory_id: str
    header: str
    body: str  # 乱数タグ付きデリミタで囲んだ本文
    score: float
    kind: str
    author: str
    trust: int


@dataclass(slots=True)
class RecallResult:
    entries: list[RecallEntry]
    args_summary: str


def memory_header(*, author: str, trust: int, kind: str, date: str) -> str:
    """`[memory: author=… trust=… kind=… date=…]`（出所ラベル）。"""
    return f"[memory: author={author} trust={int(trust)} kind={kind} date={date}]"


# ---- MemoryStore ------------------------------------------------------------------------------
class MemoryStore:
    """記憶系の操作をまとめる。conn（SQLite 正典）＋provider（埋め込み）＋backend（ベクタ）を束ねる。

    embedding.reembed が要求する ReembedStore（create_collection/drop_collection/iter_committed_bodies/
    insert_vectors）も本クラスが満たす。
    """

    def __init__(self, conn, provider, *, backend: VectorBackend | None = None) -> None:
        self.conn = conn
        self.provider = provider
        self.backend: VectorBackend = backend if backend is not None else ZvecBackend()

    # -- コレクション ---------------------------------------------------------------------------
    def collection(self) -> str:
        """実行時コレクション名（固定導出せず hive_meta を読む）。"""
        return db.active_collection(self.conn)

    def ensure_collection(self) -> str:
        name = self.collection()
        if not self.backend.has_collection(name):
            self.backend.create_collection(name, self.provider.dim)
        return name

    # -- hive_remember（二段書き）------------------------------------------------
    def hive_remember(
        self,
        session: Session,
        *,
        kind: str,
        text: str,
        request_id: str,
        now: str | None = None,
    ) -> RememberResult:
        from . import audit  # 遅延（audit は軽いが循環回避のためローカル）

        ts = _now_iso(now)

        # 冪等: 再送は二重書きしない。
        row = self.conn.execute(
            "SELECT tool FROM processed_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is not None and row["tool"] == "hive_remember":
            return RememberResult(
                True,
                None,
                reused=True,
                verdict="ok",
                args_summary=audit.build_summary(tool="hive_remember", reused=True),
            )

        # 秘密パターン検査（書込ポリシー）。reject は書かない。
        allowed, scan = allowed_for_write(text)
        reason_codes = scan.reason_codes()
        if not allowed:
            summary = audit.build_summary(
                tool="hive_remember",
                kind=kind,
                verdict=scan.verdict,
                reason_codes=reason_codes,
                accepted=False,
            )
            return RememberResult(
                False,
                None,
                reused=False,
                verdict=scan.verdict,
                warnings=["秘密パターン検査により記憶書き込みを拒否しました。"],
                reason_codes=reason_codes,
                args_summary=summary,
            )
        stored_text = scan.redacted_text
        warnings: list[str] = []
        if scan.verdict != "ok":
            warnings.append("高エントロピー箇所を赤塗りして記憶しました。")

        author = session.name
        source_trust = member_trust(self.conn, session.team, author)
        mem_id = uuid.uuid4().hex
        collection = self.ensure_collection()

        # (1) pending 挿入（コミット）。ここでは processed_requests に記録しない。
        with self.conn:
            self.conn.execute(
                "INSERT INTO memories(id, team, author, kind, created_at, source_trust, status)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (mem_id, session.team, author, kind, ts, source_trust, MEMORY_PENDING),
            )

        # (2) Zvec insert（ベクタ＋スカラー）。失敗時は pending のまま残り、起動時孤児掃除で回収される。
        vector = self.provider.embed_query(stored_text)
        self.backend.insert(
            collection,
            id=mem_id,
            vector=vector,
            text=stored_text,
            kind=kind,
            author=author,
            trust=source_trust,
        )

        # (3) committed 更新＋processed_requests 記録を同一 tx。
        with self.conn:
            self.conn.execute(
                "UPDATE memories SET status = ? WHERE id = ?", (MEMORY_COMMITTED, mem_id)
            )
            self.conn.execute(
                "INSERT INTO processed_requests(request_id, tool, created_at) VALUES(?, ?, ?)",
                (request_id, "hive_remember", ts),
            )

        # 呼び出し元 trust=0 は既定 recall に現れない旨を警告。
        if source_trust < TRUST_NORMAL:
            warnings.append(
                "あなたの現在 trust は 0 のため、この記憶は既定の hive_recall には現れません"
                "（source_trust>=1 かつ現在 trust>=1 の記憶のみ返す固定フィルタ）。"
            )

        summary = audit.build_summary(
            tool="hive_remember",
            kind=kind,
            body_len=len(text),
            source_trust=source_trust,
            verdict=scan.verdict,
            reason_codes=reason_codes,
        )
        return RememberResult(
            True,
            mem_id,
            reused=False,
            verdict=scan.verdict,
            warnings=warnings,
            reason_codes=reason_codes,
            args_summary=summary,
        )

    # -- hive_recall（固定フィルタ）------------------------------------------------------
    def hive_recall(
        self,
        session: Session,
        *,
        query: str,
        kind: str | None = None,
        top_k: int = 5,
    ) -> RecallResult:
        from . import audit

        collection = self.collection()
        if not self.backend.has_collection(collection):
            return RecallResult([], audit.build_summary(tool="hive_recall", count=0, top_k=top_k))

        qvec = self.provider.embed_query(query)
        # 粗フィルタは top_k を余裕を持って引くが、**固定取得だと低 trust 記憶が候補枠を占有して
        # 正当な記憶が埋没する**（例: 未信頼記憶 21 件が上位を占めると top_k*4=20 の枠内に
        # 正当な 1 件が入らず 0 件になる——レビューで実機再現）。固定フィルタの正典は SQLite で
        # あり、Zvec 側 trust スカラーは restamp で陳腐化し得るため検索前の押し下げはしない。
        # 代わりに、有効件数が top_k 揃うかバックエンドを読み尽くすまで取得数を段階拡大する。
        fetch_n = max(top_k * 4, top_k)
        entries: list[RecallEntry] = []
        while True:
            raw = self.backend.search(collection, vector=qvec, top_k=fetch_n, kind=kind)
            entries = []
            for hit in raw:
                row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (hit.id,)).fetchone()
                if row is None or row["status"] != MEMORY_COMMITTED:
                    continue  # 孤児（pending・メタなし）は結果に出さない
                # 固定フィルタ: source_trust>=1 かつ 著者の現在 trust>=1 の AND。
                if int(row["source_trust"]) < TRUST_NORMAL:
                    continue
                current = member_trust(self.conn, session.team, row["author"])
                if current < TRUST_NORMAL:
                    continue
                text = self._read_text(collection, hit)
                header = memory_header(
                    author=row["author"], trust=current, kind=row["kind"], date=row["created_at"]
                )
                entries.append(
                    RecallEntry(
                        memory_id=hit.id,
                        header=header,
                        body=wrap_body(text),
                        score=hit.score,
                        kind=row["kind"],
                        author=row["author"],
                        trust=current,
                    )
                )
                if len(entries) >= top_k:
                    break
            exhausted = len(raw) < fetch_n
            if len(entries) >= top_k or exhausted:
                break
            fetch_n *= 4

        summary = audit.build_summary(
            tool="hive_recall", count=len(entries), top_k=top_k, kind=kind
        )
        return RecallResult(entries=entries, args_summary=summary)

    def _read_text(self, collection: str, hit: SearchHit) -> str:
        """本文を得る。既定は Zvec の text（検索ヒットに載る）。取得不可構成では SQLite の body（TODO）。"""
        if hit.text:
            return hit.text
        stored = self.backend.get_text(collection, hit.id)
        if stored is not None:
            return stored
        # TODO: Zvec 本文保存不可構成では memories.body（追加案）を本文正典として読む。
        return "(本文を取得できませんでした)"

    # -- 起動時孤児掃除----------------------------------------------------------
    def cleanup_orphans(self) -> int:
        """pending の記憶行と対応ベクタを削除する（processed_requests には残さない）。削除件数を返す。

        プロキシ再送の受付開始前に完了させること（「昇格時の孤児掃除」）。
        あわせて reembed 中断で残った一時コレクションの残骸も除去する。
        """
        pending = self.conn.execute(
            "SELECT id FROM memories WHERE status = ?", (MEMORY_PENDING,)
        ).fetchall()
        ids = [r["id"] for r in pending]
        if ids:
            try:
                self.backend.delete(self.collection(), ids)
            except Exception as exc:  # pragma: no cover - backend 依存
                _log.warning("孤児ベクタの削除に失敗（SQLite 側は掃除継続）: %s", exc)
            with self.conn:
                self.conn.executemany("DELETE FROM memories WHERE id = ?", [(i,) for i in ids])
            _log.info("孤児掃除: pending 記憶 %d 件を削除しました。", len(ids))

        # 一時 reembed コレクションの残骸掃除（active 以外の __reembed_ を削除）。
        try:
            active = db.active_collection(self.conn)
            for name in self.backend.list_collections():
                if "__reembed_" in name and name != active:
                    self.backend.drop_collection(name)
                    _log.info("孤児掃除: 一時コレクション %s を削除しました。", name)
        except Exception:  # pragma: no cover - backend 依存
            pass
        return len(ids)

    # -- ReembedStore 実装（embedding.reembed から呼ばれる）------------------------------------
    def create_collection(self, name: str, dim: int) -> None:
        self.backend.create_collection(name, dim)

    def drop_collection(self, name: str) -> None:
        self.backend.drop_collection(name)

    def iter_committed_bodies(self) -> Iterator[tuple[str, str, str, str, int]]:
        """committed 記憶の (id, text, kind, author, source_trust) を列挙する（reembed の読み出し源）。"""
        current = self.collection()
        for row in self.conn.execute(
            "SELECT id, kind, author, source_trust FROM memories WHERE status = ?",
            (MEMORY_COMMITTED,),
        ):
            text = self.backend.get_text(current, row["id"]) or ""
            yield row["id"], text, row["kind"], row["author"], int(row["source_trust"])

    def insert_vectors(self, name: str, records: Iterable[tuple]) -> None:
        for mem_id, vec, text, kind, author, trust in records:
            self.backend.insert(
                name, id=mem_id, vector=vec, text=text, kind=kind, author=author, trust=trust
            )
