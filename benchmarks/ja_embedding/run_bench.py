#!/usr/bin/env python3
"""M1-5 日本語検索品質の簡易ベンチ（fastembed 3 モデルの recall@5 比較）。

社内文書コーパス 20 件（corpus.json）とクエリ 20 件（queries.json——本文と語彙が重ならない
言い換えで作成し、金銭系・休暇系・セキュリティ系・障害系など**近傍トピックの束**を意図的に
含めて弁別性を持たせた）に対し、fastembed の多言語 3 モデル（04_spike結果 §3 で確定した
全対応モデル）の recall@1 / recall@5 / MRR を計測する。

計測は **hive の実コードパス**（subaco_hive.embedding.FastEmbedProvider——hive_remember /
hive_recall が使うのと同じ経路。E5 の "query:"/"passage:" プレフィクスは付かない）で行い、
参考として E5 のプレフィクス付き変種（モデルカード推奨形。hive 側に実装する価値の判断材料）
も併記する。

実行（モデルは初回に HuggingFace から DL される。合計約 3.5GB・要ネットワーク）:
    uv run --extra memory python benchmarks/ja_embedding/run_bench.py
    uv run --extra memory python benchmarks/ja_embedding/run_bench.py --show-misses
    uv run --extra memory python benchmarks/ja_embedding/run_bench.py --field body

CI では実行しない（モデル DL が重いため。実行はローカル・結果は docs の実測結果に記録）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# fastembed 0.8 が対応する多言語（日本語可）モデルの全 3 択（04_spike結果 §3）。
BENCH_MODELS = [
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
    "intfloat/multilingual-e5-large",
]
# E5 モデルカード推奨のプレフィクス（参考計測。hive 実パスでは付かない）。
E5_MODEL = "intfloat/multilingual-e5-large"


def _load(name: str) -> list[dict]:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def _rank_all(doc_vecs, query_vecs) -> list[list[int]]:
    """クエリごとにコサイン類似度降順の文書 index 列を返す。"""
    import numpy as np

    d = np.asarray(doc_vecs, dtype=np.float64)
    q = np.asarray(query_vecs, dtype=np.float64)
    d = d / np.linalg.norm(d, axis=1, keepdims=True)
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    sims = q @ d.T  # (n_query, n_doc)
    return [list(map(int, row.argsort()[::-1])) for row in sims]


def bench_variant(
    label: str,
    model_name: str,
    docs: list[str],
    queries: list[str],
    *,
    doc_prefix: str = "",
    query_prefix: str = "",
) -> dict:
    """1 変種（モデル × プレフィクス有無）を計測して結果 dict を返す。"""
    from subaco_hive.embedding import FastEmbedProvider

    provider = FastEmbedProvider(model_name)
    doc_vecs = provider.embed([doc_prefix + t for t in docs])
    query_vecs = provider.embed([query_prefix + t for t in queries])
    rankings = _rank_all(doc_vecs, query_vecs)
    return {"label": label, "model": model_name, "rankings": rankings, "dim": provider.dim}


def summarize(result: dict, relevant_idx: list[int]) -> dict:
    """rankings と正解 index から recall@1 / recall@5 / MRR を算出する。"""
    ranks = []  # 正解文書の順位（1 始まり）
    for ranking, rel in zip(result["rankings"], relevant_idx, strict=True):
        ranks.append(ranking.index(rel) + 1)
    n = len(ranks)
    return {
        "label": result["label"],
        "dim": result["dim"],
        "recall@1": sum(1 for r in ranks if r <= 1) / n,
        "recall@5": sum(1 for r in ranks if r <= 5) / n,
        "mrr": sum(1.0 / r for r in ranks) / n,
        "ranks": ranks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--field",
        choices=["title_body", "body"],
        default="title_body",
        help="文書として埋め込む範囲（既定はタイトル+本文。body は本文のみの頑健性確認用）",
    )
    parser.add_argument(
        "--show-misses", action="store_true", help="top-5 を外したクエリの詳細（目視評価用）を表示"
    )
    parser.add_argument("--json-out", default=None, help="結果 JSON の出力先パス")
    args = parser.parse_args(argv)

    corpus = _load("corpus.json")
    queries = _load("queries.json")
    if args.field == "title_body":
        docs = [f"{d['title']}\n{d['body']}" for d in corpus]
    else:
        docs = [d["body"] for d in corpus]
    qtexts = [q["query"] for q in queries]
    id_to_idx = {d["id"]: i for i, d in enumerate(corpus)}
    relevant_idx = [id_to_idx[q["relevant"]] for q in queries]

    results = []
    for model in BENCH_MODELS:
        short = model.split("/")[-1]
        print(f"▶ {short} を計測中（初回はモデル DL あり）…", file=sys.stderr)
        results.append(bench_variant(short, model, docs, qtexts))
    print(f"▶ {E5_MODEL.split('/')[-1]} (+prefix) を計測中…", file=sys.stderr)
    results.append(
        bench_variant(
            f"{E5_MODEL.split('/')[-1]} (+prefix)",
            E5_MODEL,
            docs,
            qtexts,
            doc_prefix="passage: ",
            query_prefix="query: ",
        )
    )

    summaries = [summarize(r, relevant_idx) for r in results]

    print(f"\n## 結果（field={args.field}・クエリ {len(qtexts)} 件 × 文書 {len(docs)} 件）\n")
    print("| モデル | 次元 | recall@1 | recall@5 | MRR |")
    print("|---|---|---|---|---|")
    for s in summaries:
        print(
            f"| {s['label']} | {s['dim']} | {s['recall@1']:.2f} | {s['recall@5']:.2f} | {s['mrr']:.3f} |"
        )

    if args.show_misses:
        print("\n## 目視評価用: top-5 を外した / 1 位でないクエリ\n")
        for s, r in zip(summaries, results, strict=True):
            misses = [
                (i, rank)
                for i, rank in enumerate(s["ranks"])
                if rank > 1  # 1 位でないものすべて（目視対象）
            ]
            print(f"### {s['label']}")
            if not misses:
                print("（全クエリ 1 位）")
            for i, rank in misses:
                top5 = [corpus[j]["title"] for j in r["rankings"][i][:5]]
                print(
                    f"- {queries[i]['id']}「{queries[i]['query']}」→ 正解 {corpus[relevant_idx[i]]['title']} は {rank} 位。top5: {top5}"
                )
            print()

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {"field": args.field, "summaries": summaries},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
