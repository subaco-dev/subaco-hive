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

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from . import db
from .logging import get_logger
from .messaging import Session, member_trust, wrap_body
from .models import MEMORY_COMMITTED, MEMORY_PENDING, TRUST_NORMAL
from .secrets import allowed_for_write

_log = get_logger(__name__)


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
    """Zvec 実装（**遅延 import**）。具体 API は Zvec spike で確定するまで本クラスに隔離する（TODO）。"""

    def __init__(self) -> None:
        self._zvec = None
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

    # 以下は骨子。実 API（CollectionSchema/insert/hybrid search/スナップショット）は spike で確定する（TODO）。
    def has_collection(self, name: str) -> bool:  # pragma: no cover - Zvec 実体依存
        raise NotImplementedError(
            "ZvecBackend.has_collection は Zvec spike 確定後に実装する（TODO）。"
        )

    def create_collection(self, name: str, dim: int) -> None:  # pragma: no cover
        raise NotImplementedError(
            "ZvecBackend.create_collection は Zvec spike 確定後に実装する（TODO）。"
        )

    def drop_collection(self, name: str) -> None:  # pragma: no cover
        raise NotImplementedError(
            "ZvecBackend.drop_collection は Zvec spike 確定後に実装する（TODO）。"
        )

    def list_collections(self) -> list[str]:  # pragma: no cover
        raise NotImplementedError(
            "ZvecBackend.list_collections は Zvec spike 確定後に実装する（TODO）。"
        )

    def insert(self, name, *, id, vector, text, kind, author, trust) -> None:  # pragma: no cover
        raise NotImplementedError(
            "ZvecBackend.insert は Zvec spike 確定後に実装する（TODO）。"
        )

    def delete(self, name: str, ids: list[str]) -> None:  # pragma: no cover
        raise NotImplementedError(
            "ZvecBackend.delete は Zvec spike 確定後に実装する（TODO）。"
        )

    def get_text(self, name: str, id: str) -> str | None:  # pragma: no cover
        raise NotImplementedError(
            "ZvecBackend.get_text は Zvec spike 確定後に実装する（TODO）。"
        )

    def search(self, name, *, vector, top_k, kind=None) -> list[SearchHit]:  # pragma: no cover
        raise NotImplementedError(
            "ZvecBackend.search は Zvec spike 確定後に実装する（TODO）。"
        )


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
        # 粗フィルタは top_k を余裕を持って引き、正典（SQLite）側の固定フィルタで確定する。
        raw = self.backend.search(collection, vector=qvec, top_k=max(top_k * 4, top_k), kind=kind)

        entries: list[RecallEntry] = []
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
