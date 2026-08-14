"""Zvec 実体を要する記憶系の統合テスト（`importorskip` でガード）。

`zvec` wheel が入っていない環境（Intel Mac / 記憶系 extra なしの CI ジョブ）では **skip** する。
記憶系の SQLite 状態機械（二段書き・孤児掃除・固定フィルタ 等）そのものは依存ゼロで動く
`InMemoryVectorBackend` を用いた `tests/test_memory.py` で既に検証済み。本ファイルは

  (a) 実 `ZvecBackend` が同じ振る舞い（パリティ）を示すこと、
  (b) Zvec spike（計画書 §5 第一）で実測した前提が回帰しないこと

の 2 点を担う。CI では macOS(arm64) / Linux の記憶系ジョブが実行する。

spike で確定した前提（本ファイルが回帰の番人になる項目）:
  - 本文 text を Zvec に保存し**全文取得**できる（→ memories.body 追加の分岐は不要）
  - COSINE の `Doc.score` は距離なので、`SearchHit.score` は類似度へ反転する
  - ライター SIGKILL 後、別プロセスが**待ちなしで**書き込みモードへ再オープンできる
  - 稼働ライターがいる間は別プロセスからの**オープンが失敗する**（backup は writer 経由が必須）
  - コレクションディレクトリの丸ごとコピーがスナップショットとして復元できる
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

# zvec 未導入なら以降を丸ごと skip（wheel 無ければ skip）。
zvec = pytest.importorskip("zvec")

from subaco_hive import db as dbmod  # noqa: E402 - importorskip の後に import する
from subaco_hive import messaging  # noqa: E402
from subaco_hive.memory import (  # noqa: E402
    InMemoryVectorBackend,
    MemoryStore,
    ZvecBackend,
)
from subaco_hive.messaging import Session  # noqa: E402
from subaco_hive.models import MEMORY_COMMITTED, MEMORY_PENDING  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKER = Path(__file__).resolve().parent / "_zvec_worker.py"

# 日本語の長文（本文の全文取得に欠落がないことを検出するため十分に長くする）。
LONG_JA = "エージェントが合意した設計判断をここに残す。" * 100


@pytest.fixture
def backend(tmp_path):
    be = ZvecBackend(root=str(tmp_path / "zvec"))
    yield be
    be.close()


@pytest.fixture
def store(conn, provider, backend):
    return MemoryStore(conn, provider, backend=backend)


def _join(conn, name, trusted=True):
    ta = {name: None} if trusted else {}
    return messaging.hive_join(
        conn, team="alpha", name=name, vendor="x", request_id=f"j-{name}", trusted_agents=ta
    )


def _env() -> dict[str, str]:
    e = dict(os.environ)
    prev = e.get("PYTHONPATH", "")
    e["PYTHONPATH"] = str(_REPO_ROOT) + (os.pathsep + prev if prev else "")
    return e


# ---- (a) 実バックエンドでのパリティ -------------------------------------------------------------
def test_remember_recall_roundtrip_with_long_japanese_body(conn, store):
    """remember → recall が実 Zvec 経由で成立し、日本語長文が**全文**返ること。

    本文が欠落する構成なら memories に body 列を足して SQLite を本文の正典にする分岐（設計書 §4.2）が
    必要になる。ここが緑である限り、その分岐は不要。
    """
    _join(conn, "alice")
    res = store.hive_remember(
        Session("alpha", "alice"), kind="decision", text=LONG_JA, request_id="m1"
    )
    assert res.accepted and res.memory_id
    assert (
        conn.execute("SELECT status FROM memories WHERE id=?", (res.memory_id,)).fetchone()[
            "status"
        ]
        == MEMORY_COMMITTED
    )

    out = store.hive_recall(Session("alpha", "alice"), query="設計判断の記録")
    assert out.entries, "recall が実 Zvec からヒットを返さなかった"
    assert LONG_JA in out.entries[0].body, "本文が途中で欠落している"
    assert out.entries[0].header.startswith("[memory: author=alice")


def test_recall_hides_untrusted_and_demoted_authors(conn, store):
    """固定フィルタのパリティ: source_trust=0 の記憶も、降格された著者の記憶も返らない。"""
    _join(conn, "alice")
    _join(conn, "eve", trusted=False)  # trust=0

    store.hive_remember(Session("alpha", "eve"), kind="note", text="未信頼の記憶", request_id="e1")
    store.hive_remember(
        Session("alpha", "alice"), kind="note", text="信頼された記憶", request_id="a1"
    )

    got = store.hive_recall(Session("alpha", "alice"), query="記憶", top_k=10)
    assert [e.author for e in got.entries] == ["alice"]

    # alice を降格すると、過去の記憶も既定 recall から消える。
    conn.execute("UPDATE members SET trust_level=0 WHERE team='alpha' AND name='alice'")
    conn.commit()
    assert store.hive_recall(Session("alpha", "alice"), query="記憶", top_k=10).entries == []


def test_recall_kind_filter_and_score_orientation(conn, store):
    """kind 絞り込みが効き、score が「近いほど大きい」類似度で返ること（COSINE 距離の反転）。"""
    _join(conn, "alice")
    s = Session("alpha", "alice")
    store.hive_remember(s, kind="decision", text="デプロイ手順を決めた", request_id="d1")
    store.hive_remember(s, kind="note", text="全く無関係な走り書き", request_id="n1")

    only_decision = store.hive_recall(s, query="デプロイ手順", kind="decision", top_k=10)
    assert [e.kind for e in only_decision.entries] == ["decision"]

    ranked = store.hive_recall(s, query="デプロイ手順を決めた", top_k=10)
    scores = [e.score for e in ranked.entries]
    assert scores == sorted(scores, reverse=True), f"score が降順でない: {scores}"
    assert ranked.entries[0].kind == "decision"


def test_kind_filter_expression_is_not_injectable(conn, store):
    """kind はエージェント入力。filter 式を壊す値でも例外にならず、素通りもしないこと。"""
    _join(conn, "alice")
    s = Session("alpha", "alice")
    store.hive_remember(s, kind="note", text="通常の記憶", request_id="n1")

    # 素朴に連結すると `kind = '' OR '1'='1'` になって全件が漏れる形。
    for hostile in ("' OR '1'='1", "note' OR 'x'='x", '" OR 1=1 --'):
        assert store.hive_recall(s, query="記憶", kind=hostile, top_k=10).entries == []


def test_kind_filter_matches_non_ascii_kind(conn, store):
    """押し下げできない文字（日本語）の kind でも絞り込みは正しく効くこと（Python 側突き合わせ）。"""
    _join(conn, "alice")
    s = Session("alpha", "alice")
    store.hive_remember(s, kind="設計判断", text="日本語 kind の記憶", request_id="j1")
    store.hive_remember(s, kind="note", text="別の記憶", request_id="n1")

    out = store.hive_recall(s, query="記憶", kind="設計判断", top_k=10)
    assert [e.kind for e in out.entries] == ["設計判断"]


def test_cleanup_orphans_removes_pending_vector(conn, store, backend):
    """孤児掃除が pending 行と対応ベクタの両方を落とすこと（実バックエンド）。"""
    _join(conn, "alice")
    collection = store.ensure_collection()
    conn.execute(
        "INSERT INTO memories(id, team, author, kind, created_at, source_trust, status)"
        " VALUES('orphan', 'alpha', 'alice', 'note', '2026-01-01T00:00:00+00:00', 1, ?)",
        (MEMORY_PENDING,),
    )
    conn.commit()
    backend.insert(
        collection,
        id="orphan",
        vector=[0.5] * 16,
        text="孤児",
        kind="note",
        author="alice",
        trust=1,
    )
    assert backend.get_text(collection, "orphan") == "孤児"

    assert store.cleanup_orphans() == 1
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='orphan'").fetchone()[0] == 0
    assert backend.get_text(collection, "orphan") is None


def test_reembed_swaps_collection(conn, store, provider, backend):
    """reembed の一時コレクション名も Zvec の名前制約に収まり、原子的スワップが成立すること。"""
    from subaco_hive import embedding

    _join(conn, "alice")
    s = Session("alpha", "alice")
    store.hive_remember(s, kind="note", text="再埋め込みの対象", request_id="r1")
    old = dbmod.active_collection(conn)

    new = embedding.reembed(conn, store, provider)
    assert new != old and new.startswith(old)
    assert dbmod.active_collection(conn) == new
    assert backend.has_collection(new) and not backend.has_collection(old)
    assert store.hive_recall(s, query="再埋め込み", top_k=5).entries


# ---- (b) spike 前提の回帰ガード ------------------------------------------------------------------
def test_collection_name_constraint_is_enforced(backend):
    """Zvec の名前制約（英数と `_` `-`・3〜64 字）を破る名前は明示的に弾くこと。"""
    for bad in ("ab", "a" * 65, "hive_チーム", "hive/a", "hive.a"):
        with pytest.raises(ValueError):
            backend.create_collection(bad, 16)


def test_collection_name_for_fits_zvec_limit_with_reembed_suffix():
    """team が上限 64 字でも `hive_{team}__reembed_{ts}` が 64 字に収まること。"""
    from subaco_hive import embedding

    for team in ("a", "team-1", "x" * 38, "y" * 64):
        base = dbmod.collection_name_for(team)
        assert ZvecBackend.NAME_RE.match(base), base
        temp = embedding.temp_collection_name(base)
        assert ZvecBackend.NAME_RE.match(temp), temp
    # 決定的（同じ team は常に同じ名前）。
    assert dbmod.collection_name_for("z" * 64) == dbmod.collection_name_for("z" * 64)


def test_writer_sigkill_then_immediate_reopen(tmp_path, backend):
    """ライターを SIGKILL した直後に、別プロセスが書き込みモードで再オープンできること。

    first-writer-wins のフェイルオーバー（プロキシ昇格 → SQLite/Zvec 開き直し）の前提。
    残留 LOCK の手動解放が要るなら、この前提は崩れる。
    """
    root = str(tmp_path / "zvec")
    backend.create_collection("hive_failover", 16)
    backend.close()  # 親は LOCK を手放す

    child = subprocess.Popen(
        [sys.executable, str(_WORKER), "hold", root, "hive_failover", "16"],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "READY", child.stderr.read()[-500:]

        # 稼働中の子がいる間は、別プロセスからのオープンが失敗する（= backup は writer 経由が必須）。
        blocked = subprocess.run(
            [sys.executable, str(_WORKER), "open-write", root, "hive_failover"],
            env=_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert blocked.stdout.startswith("FAILED"), blocked.stdout

        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:  # pragma: no cover - 異常系
            child.kill()
            child.wait(timeout=30)

    # SIGKILL 直後に待ちなしで再オープンできること。
    t0 = time.monotonic()
    reopened = ZvecBackend(root=root)
    try:
        assert reopened.get_text("hive_failover", "held") == "子プロセスが書いた本文"
        reopened.insert(
            "hive_failover",
            id="after",
            vector=[0.2] * 16,
            text="昇格後の書き込み",
            kind="note",
            author="promoted",
            trust=1,
        )
        assert reopened.get_text("hive_failover", "after") == "昇格後の書き込み"
    finally:
        reopened.close()
    assert time.monotonic() - t0 < 10.0, "再オープンに時間がかかりすぎ（残留ロック待ちの疑い）"


def test_default_root_matches_backup_layout(tmp_path, monkeypatch):
    """既定のコレクション配置が `hive admin backup` の対象ディレクトリと一致すること。

    ここがずれると backup は例外を出さずに**ベクタを含まないバックアップ**を作る（静かな欠損）。
    """
    from subaco_hive import cli
    from subaco_hive.memory import MEMORY_DIRNAME

    assert cli._MEMORY_DIRNAME == MEMORY_DIRNAME

    hive_root = tmp_path / ".hive"
    hive_root.mkdir(mode=0o700)
    monkeypatch.setenv("HIVE_DB_PATH", str(hive_root / "messages.db"))
    be = ZvecBackend()
    try:
        be.create_collection("hive_layout", 16)
        assert (hive_root / MEMORY_DIRNAME / "hive_layout").is_dir()
    finally:
        be.close()


def test_directory_snapshot_restores(tmp_path, backend):
    """コレクションディレクトリの丸ごとコピーがスナップショットとして復元できること（§4.6 backup の前提）。"""
    backend.create_collection("hive_snapshot", 16)
    for i in range(20):
        backend.insert(
            "hive_snapshot",
            id=f"m{i}",
            vector=[0.01 * i] * 16,
            text=f"本文-{i}" * 20,
            kind="note",
            author="alice",
            trust=1,
        )
    src = Path(backend._path("hive_snapshot"))
    dst_root = tmp_path / "backup"
    shutil.copytree(src, dst_root / "hive_snapshot")
    backend.close()

    restored = ZvecBackend(root=str(dst_root))
    try:
        for i in range(20):
            assert restored.get_text("hive_snapshot", f"m{i}") == f"本文-{i}" * 20
        hits = restored.search("hive_snapshot", vector=[0.05] * 16, top_k=5)
        assert len(hits) == 5 and all(h.text for h in hits)
    finally:
        restored.close()


# ---- fastembed（実モデルのダウンロードを伴うため既定は skip）--------------------------------------
@pytest.mark.skipif(
    os.environ.get("SUBACO_HIVE_LIVE_EMBEDDING") != "1",
    reason="実モデルの取得（数百 MB・ネットワーク）を伴うため SUBACO_HIVE_LIVE_EMBEDDING=1 のときだけ実行する",
)
def test_fastembed_default_model_matches_known_dim():
    """既定モデルが fastembed で実際にロードでき、KNOWN_DIMS の次元と一致すること。"""
    fastembed = pytest.importorskip("fastembed")
    from subaco_hive.embedding import DEFAULT_FASTEMBED_MODEL, KNOWN_DIMS, FastEmbedProvider

    supported = {m["model"] for m in fastembed.TextEmbedding.list_supported_models()}
    assert DEFAULT_FASTEMBED_MODEL in supported, (
        f"既定モデルが fastembed 非対応: {DEFAULT_FASTEMBED_MODEL}"
    )

    provider = FastEmbedProvider()
    vec = provider.embed_query("エージェントの記憶はどこに保存されるか")
    assert len(vec) == KNOWN_DIMS[DEFAULT_FASTEMBED_MODEL] == provider.dim


@pytest.mark.skipif(
    os.environ.get("SUBACO_HIVE_LIVE_EMBEDDING") != "1",
    reason="実モデルの取得（数百 MB・ネットワーク）を伴うため SUBACO_HIVE_LIVE_EMBEDDING=1 のときだけ実行する",
)
def test_japanese_recall_end_to_end(tmp_path):
    """実 fastembed + 実 Zvec で日本語の意味検索が成立すること（M1 DoD シナリオの記憶系部分）。"""
    from subaco_hive.embedding import FastEmbedProvider

    conn = dbmod.init_db(tmp_path / "messages.db", "alpha", embedding_model="live", embedding_dim=0)
    try:
        _join(conn, "alice")
        provider = FastEmbedProvider()
        be = ZvecBackend(root=str(tmp_path / "zvec"))
        st = MemoryStore(conn, provider, backend=be)
        s = Session("alpha", "alice")
        st.hive_remember(
            s, kind="decision", text="本番のデプロイは毎週火曜に行うと決めた", request_id="d1"
        )
        st.hive_remember(s, kind="note", text="昼食は近所のラーメン屋が良い", request_id="n1")
        try:
            got = st.hive_recall(s, query="リリースはいつ行う?", top_k=1)
            assert got.entries and "火曜" in got.entries[0].body
        finally:
            be.close()
    finally:
        conn.close()


class _AngleProvider:
    """クエリ= x 軸単位ベクトル・本文=登録した角度、の決定的埋め込み（再現率の検証用）。"""

    dim = 8
    model_name = "angle-fake"

    def __init__(self) -> None:
        self.by_text: dict[str, list[float]] = {}

    @staticmethod
    def _vec(deg: float) -> list[float]:
        r = math.radians(deg)
        v = [0.0] * 8
        v[0] = math.cos(r)
        v[1] = math.sin(r)
        return v

    def embed_query(self, text: str) -> list[float]:
        return self.by_text.get(text, self._vec(0.0))


def _recall_bodies_with_dominant_other_kind(backend, db_path) -> list[str]:
    """多数派 kind='note' 40 件がクエリ近傍を占める状態で、日本語 kind 3 件を recall する。"""
    provider = _AngleProvider()
    conn = dbmod.init_db(db_path, "alpha", embedding_model=provider.model_name, embedding_dim=8)
    try:
        _join(conn, "alice")
        store = MemoryStore(conn, provider, backend=backend)
        s = Session("alpha", "alice")
        for i in range(40):
            text = f"note-{i}"
            provider.by_text[text] = provider._vec(0.1 * (i + 1))
            store.hive_remember(s, kind="note", text=text, request_id=f"n{i}")
        for i in range(3):
            text = f"design-{i}"
            provider.by_text[text] = provider._vec(30.0 + i)
            store.hive_remember(s, kind="設計判断", text=text, request_id=f"d{i}")
        out = store.hive_recall(s, query="q", kind="設計判断", top_k=5)
        # 本文は乱数タグ付きデリミタ（ヘッダ偽装対策）で囲まれ、タグは呼び出しごとに変わるため
        # 中身の行だけを取り出して比較する。
        return [e.body.split("\n")[1] if "\n" in e.body else e.body for e in out.entries]
    finally:
        conn.close()


def test_recall_non_pushdown_kind_finds_matches_beyond_global_topk(tmp_path):
    """押し下げ不能な kind（日本語等）でも「kind 一致の上位 top_k」が返ること（再現率のパリティ）。

    単発の topk 取得だと「全体上位 topk の中の一致分」となり、多数派 kind がクエリ近傍を
    占めると実在する記憶が 0 件で返る（レビューで実機再現した取りこぼし）。ZvecBackend は
    topk を段階拡大して InMemoryVectorBackend と同じ結果を返さなければならない。
    """
    zb = ZvecBackend(root=str(tmp_path / "vec"))
    try:
        got = _recall_bodies_with_dominant_other_kind(zb, str(tmp_path / "z.db"))
    finally:
        zb.close()
    expected = _recall_bodies_with_dominant_other_kind(
        InMemoryVectorBackend(), str(tmp_path / "m.db")
    )
    assert got == expected
    assert sorted(got) == ["design-0", "design-1", "design-2"]
