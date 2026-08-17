# subaco-hive

Subaco のメモリプレーンを担う **MCP サーバー＋ops CLI**。
SQLite（メッセージング）と Zvec（長期記憶）に対する**唯一のライター**として、
マルチエージェント協業のエピソード記憶とチーム内メッセージングを提供する。
agmsg の「SQLite ファイルが床、エージェントがプレイヤー」という薄さを踏襲しつつ、
**床の管理人を一人だけ置く**（first-writer-wins）構図。

- 論理名: subaco-hive（本文中では hive-mcp とも呼称）
- パッケージ: `subaco_hive` ／ ライセンス: Apache-2.0 ／ 対象 Python: 3.11+

## エントリポイント

| コマンド | 実体 | 役割 |
|---|---|---|
| `subaco-hive` | `subaco_hive.server:main` | **stdio MCP サーバー**を起動（`.mcp.json` / `hive-mcp` ランチャが呼ぶ） |
| `hive` | `subaco_hive.cli:main` | **ops/admin CLI**（set-trust / reset-token / backup / restore / reembed / stats / migrate） |

> stdout は JSON-RPC（MCP stdio トランスポート）専有。診断ログは **stderr**（`HIVE_LOG_LEVEL`）。

## v0 の実装範囲

**動くもの（本リポジトリ v0）:**

- メッセージング層（純 stdlib sqlite3）: `hive_join` / `hive_post` / `hive_inbox` / `hive_members` / `hive_history`。
  メンバートークン・冪等キー（request_id）・trusted_agents 許可リスト・mcp_posts 台帳による trust 解決・
  未信頼本文の非配送＋メタデータ一度きり通知・recipient 実在検証・秘密パターン検査（書込拒否/読出赤塗り）。
- first-writer-wins（`.hive/writer.lock` の flock ＋ `.hive/hive.sock` の NDJSON プロキシ）とフェイルオーバー。
- 記憶層（`hive_remember` / `hive_recall`）: 二段書き（pending→Zvec→committed）・起動時孤児掃除・
  固定フィルタ（source_trust≥1 かつ現在 trust≥1）。ベクタバックエンドは抽象化され、既定は **Zvec（遅延 import）**。
  Zvec 未導入でも純 Python の `InMemoryVectorBackend` で SQLite 状態機械を動作・テスト可能。
  `ZvecBackend` は **zvec 0.6 の実 API で実装済み**（コレクション＝`<.hive>/memory/<name>` ディレクトリ、
  COSINE・HNSW、本文は STRING スカラー）。
- 埋め込み抽象（`EmbeddingProvider`）: 既定 fastembed（ローカル・遅延 import）／OpenAI 互換 API。`hive reembed` で原子的スワップ。
  既定モデルは `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（384 次元）。
- ops CLI・監査ログ（本文非記録）・`hive_stats`。
  なお agmsg は**着想元であり、agmsg 連携対応はスコープ外**（同一 DB での無改変 agmsg 相互運用は
  不成立と spike で確定したうえ、agmsg 自体が発展途上で互換性を破るスキーマ変更が短い間隔で
  発生しており、ブリッジでも追随保守が見合わないため——docs 06_spike結果）。

**繰延（TODO をコード内 docstring に明示）:**

- 埋め込み既定モデルの最終選定（日本語簡易ベンチ — M1-5）。fastembed が対応する多言語モデルは
  MiniLM-L12-v2（384 次元 / 0.22GB・既定）・mpnet-base-v2（768 次元 / 1.0GB）・
  `intfloat/multilingual-e5-large`（1024 次元 / 2.24GB）の 3 つ。
- ハイブリッド検索（Zvec の FTS/BM25 面）の活用。v0 は密ベクタ検索のみ。
- 前方マイグレーション runner（v0 は `hive_meta.schema_version` の照合のみ）。
- プロキシ↔ライターのスケール限界・管理チャネルの admin token ゲート・メンバー個別除去 等。

**記憶系を有効にする:**

```sh
pip install 'subaco-hive[memory]'   # zvec + fastembed
```

記憶系の統合テストは zvec が入っているときだけ走る（`tests/test_memory_zvec.py`）。
実モデルのダウンロード（数百 MB）を伴う日本語 E2E は `SUBACO_HIVE_LIVE_EMBEDDING=1` のときのみ実行する。

> **Zvec の排他に関する運用上の制約（spike で実測）:** 常駐ライターがコレクションを開いている間は、
> 別プロセスからの **read-only オープンも失敗する**（`Can't lock read-only collection: .../LOCK`）。
> したがってバックアップは必ず常駐ライター経由（管理チャネル）か、ライター停止状態で行う。
> 一方、ライターが SIGKILL されても LOCK は OS が解放するため、**残留ロックの手動解放は不要**で
> 昇格したプロキシは待ちなしで開き直せる。

## アーキテクチャ: 単一ライターと first-writer-wins

- **Zvec と memories/members への書き込みプロセスは常に 1 つ**。各エージェントは stdio で MCP を起動するが、
  実起動時に `.hive/hive.sock` を確認し、既存ライターがあれば**薄いプロキシ**として接続する。
- ライターの正当性は**ソケットの存在ではなく `.hive/writer.lock` の flock 保持**で担保する。
  flock 対象は hive.sock とは別の恒久ロックファイルで、稼働中は unlink・再作成しない
  （unlink+flock レースによる二重ライターを排除）。
- **フェイルオーバー**: プロキシがソケット EOF を検知したら flock を争奪し、勝者が新ライターへ昇格して
  SQLite/Zvec を開き直し socket を再作成、敗者は再接続する。書込系（join/post/remember）は
  request_id で冪等化し（`processed_requests` 台帳）、昇格後の新ライターも突合して重複実行を抑止する。
- **アクセス制御**: `.hive/` は 0700・`hive.sock` は 0600。接続受付時に SO_PEERCRED（macOS は LOCAL_PEERCRED）で
  同一 UID のみ許可。同一 UID の悪意プロセスは脅威モデル外（`.hive/` へ直接書けるため防御不能）。
- **CLI 直書きの境界**: `messages` / `message_reads` への直接 INSERT のみ CLI 経路に許容。
  WAL＋busy_timeout で競合を吸収。CLI 直書きは mcp_posts 台帳に載らないため **trust=0 扱い**（本文は既定 inbox で非配送）。

## MCP ツール一覧

| ツール | 引数 | 概要 |
|---|---|---|
| `hive_join` | `name, vendor?, team?, token?` | 参加。members 登録・メンバートークン発行/照合・セッション束縛 |
| `hive_post` | `body, recipient?` | 送信（recipient 省略でブロードキャスト・指定時は実在検証）。秘密検査あり |
| `hive_inbox` | `since?, include_untrusted?` | 未読取得。既定は trust≥1 本文＋未信頼はメタデータ一度きり通知 |
| `hive_members` | — | 登録メンバー一覧（CLI 参加者は現れない） |
| `hive_history` | `limit=50` | ルーム履歴の再生（ブロードキャストのみ・文脈シード用） |
| `hive_remember` | `kind, text` | 長期記憶へ書き込み（二段書き）。秘密検査あり |
| `hive_recall` | `query, kind?, top_k=5` | セマンティック検索（固定フィルタ・出所ラベル付与） |
| `hive_stats` | — | 利用統計（recall 回数・メッセージ往復数等） |

`trust` は 0:未信頼 / 1:通常 / 2:高信頼。**昇格・降格は人間の管理操作のみ**（`hive admin set-trust`）。
v0 の機械フィルタ（recall・inbox 配送）は全て `>=1` で判定する（2 と 1 を機械的に区別しない）。

## ops/admin CLI（`hive`）

```sh
hive admin set-trust <team> <name> <0|1|2> [--restamp]  # 昇格/降格（--restamp で過去記憶の source_trust も更新）
hive admin reset-token <team> <name>                     # メンバートークン紛失時の再発行
hive admin backup <dir>                                  # SQLite(.backup) + Zvec スナップショット + manifest
hive admin restore <dir>                                 # 整合照合の上で差し替え（不一致は fail-closed）
hive reembed [--provider fastembed|openai] [--model ..]  # 埋め込みモデル切替の原子的再埋め込み
hive stats                                               # 利用統計
hive migrate                                             # schema_version 照合（v0。runner は将来対応）
```

管理操作の経路: 常駐ライターがいれば `hive.sock` の管理チャネル経由、いなければ
**一時ライター**として flock を取得して実行（一回限り・hive.sock は作成せず flock 解放で終了）。いずれも audit に記録。

## 環境変数

| 変数 | 既定 | 用途 |
|---|---|---|
| `HIVE_DB_PATH` | `$PWD/.hive/messages.db` | SQLite のパス。未設定時は CWD から上位へ `.hive/` を探索（git ルート/$HOME で打切） |
| `HIVE_TEAM` | `.hive/team` の内容 | チーム名。**導出はせず読むだけ**（正規化は devShell の `hive-team` が担う） |
| `HIVE_LOG_LEVEL` | `info` | 診断ログのレベル。**ログは stderr、stdout は JSON-RPC 専有** |

`subaco_hive` は `HIVE_DB_PATH` 未設定時に上位方向へ既存 `.hive/` を探索するのみで、**暗黙に新規作成しない**
（作成は `.envrc` / bootstrap の責務。チームのサイレント分裂を防ぐ）。

## MCP クライアントへの登録

> **重要**: direnv 有効シェルからの起動でも、MCP 子プロセスへの環境変数伝播は**クライアント実装依存**であり
> 保証されない。そのため下記の登録雛形は `HIVE_DB_PATH` / `HIVE_TEAM` を**絶対パス・明示値**で指定する。

### Claude Code（プロジェクトスコープ `.mcp.json`）

devShell の `hive-mcp` ラッパー（固定 requirements 経由で `subaco-hive` を起動）を指す:

```json
{
  "mcpServers": {
    "subaco-hive": { "command": "hive-mcp", "args": [] }
  }
}
```

devShell 外・素の環境では `uvx subaco-hive`（下記 Codex/Gemini と同型）に置き換える。

### Codex（`~/.codex/config.toml` の `mcp_servers` 節）

```toml
[mcp_servers.subaco-hive]
command = "uvx"
args = ["subaco-hive"]        # 再現性重視なら: ["--with-requirements", "/abs/requirements-hive.txt", "subaco-hive==<版>"]

[mcp_servers.subaco-hive.env]
HIVE_DB_PATH = "/abs/path/to/my-product/.hive/messages.db"  # 絶対パス明示（伝播はクライアント依存）
HIVE_TEAM    = "my-product"                                  # 絶対値明示（.hive/team と一致させる）
```

### Gemini CLI（`.gemini/settings.json` の `mcpServers`）

```json
{
  "mcpServers": {
    "subaco-hive": {
      "command": "uvx",
      "args": ["subaco-hive"],
      "env": {
        "HIVE_DB_PATH": "/abs/path/to/my-product/.hive/messages.db",
        "HIVE_TEAM": "my-product"
      }
    }
  }
}
```

> 公開前（PyPI 未登録）の dev では `uvx --from /abs/path/to/subaco-hive subaco-hive`（または `SUBACO_HIVE_DEV`）で
> ローカルの作業ツリーから起動できる。

## エージェントハーネスの推奨権限（deny/ask 雛形）

**「昇格は人間のみ」は技術的 enforce ではなく慣行＋摩擦**（Bash を持つエージェントは `hive admin` を実行し得る）。
補完として、ハーネス側の権限設定で管理コマンドを拒否する雛形を同梱する。Claude Code の `settings.json` 例:

```json
{
  "permissions": {
    "deny": [
      "Bash(hive admin:*)",
      "Bash(hive reembed:*)"
    ]
  }
}
```

これは defense-in-depth であり、同一 UID の直接 DB 書換には無効（残余リスク）。

## trusted_agents 許可リスト（リポジトリ外）

ホスト管理者が管理する `~/.config/subaco/<team>/trusted_agents`（エージェント書換不能）に列挙した名義は、
**members 行の新規作成時に限り** trust=1 を自動付与する（リスト外は trust=0）。各行:

```
# 1 行 = name、または「name <token_hash>」（事前共有トークンのハッシュ併記）。# 以降はコメント。
alice
codex-1  3b1f...e9   # トークン併記名義は初回 join でも照合必須。未提示/不一致は行を作らず拒否（squatting 封じ）
```

- 既存メンバーの trust は join では変更しない（変更手段は `hive admin set-trust` のみ）。
- bootstrap はこのファイルを生成しない（作成手順の案内のみ）。`.hive/` リセット後の最初の join で再適用される。

## 運用 runbook

### リセット（チーム作り直し）

hive-mcp を**停止**してから `.hive/` を削除し、`.envrc` / bootstrap で再初期化する。
`trusted_agents`（リポジトリ外）は消えないため、再 join 時に trust が再適用される。

```sh
# MCP サーバー（全エージェントのセッション）を止めてから:
rm -rf .hive/            # messages.db・team・hive.sock・writer.lock・memory/ を破棄
direnv reload            # .envrc が .hive/ を 0700 で再初期化し team を再生成
```

### バックアップ

```sh
hive admin backup /path/to/backup-2026-07-08
# 生成物: messages.db（.backup 産物）/ memory/（Zvec スナップショット）/ manifest.json
#         manifest = { schema_version, embedding_model, embedding_dim, active_collection }
```

SQLite は WAL 下でもオンライン安全な `.backup` API を使う。Zvec スナップショットの静止は一時ライターの
flock 取得で担保（常駐ライター稼働中は静止できない可能性を警告。管理チャネル quiesce は TODO）。

### 復元（fail-closed）

hive-mcp を**停止した状態**で行う（常駐ライターが flock 保持中なら拒否）。
`schema_version` / `embedding_model` / `embedding_dim` / `active_collection` の整合が取れる組み合わせだけを
受理し、**不一致は fail-closed で拒否**する（SQLite と Zvec を同一バックアップ時点の組で差し替え）。

```sh
hive admin restore /path/to/backup-2026-07-08
```

### 埋め込みモデル切替

環境変数を変えただけでは切り替わらない（共有記憶の静かな破壊を防ぐ）。**必ず `hive reembed`** で
一時コレクションを構築→`hive_meta.active_collection` を原子的に切替→旧コレクション削除、の順で行う。

```sh
# 単一ライターで行うため、先に MCP サーバー（subaco-hive）を停止する。
hive reembed --provider fastembed --model intfloat/multilingual-e5-base
```

## 開発

```sh
just test     # pytest（stdlib 層は外部依存なしで緑。並行性テストはプロセスレベルで自動化）
just lint     # ruff check
just fmt      # ruff format
just compile  # 構文チェックのみ（依存取得なし）
```

外部依存（mcp SDK / zvec / fastembed）は `optional-dependencies`（`[mcp]` / `[memory]` / `[all]`）に置き、
**依存が未インストールでも各モジュールは import 可能**（遅延 import）を守る。
CI（`.github/workflows/ci.yml`）は ubuntu / macos の両ランナーで ruff＋pytest を実行し、
Zvec 統合は macOS(arm64) の別ジョブで走らせる（wheel が無ければ skip）。

### テストの層構成

- `test_messaging` / `test_idempotency` / `test_normalize` / `test_secrets` /
  `test_memory` / `test_server_dispatch` … stdlib のみで緑（外部依存不要）。
- `test_concurrency` … **プロセスレベル**（subprocess）で first-writer-wins・フェイルオーバー・
  WAL 並行（integrity_check・欠落/重複なし・busy_timeout 超過のエラー返却・書込昇格の再試行）を検証。
- `test_memory_zvec` … `pytest.importorskip("zvec")` でガード。wheel 無ければ skip。

## 固定 requirements の生成

テンプレート同梱の `requirements-hive.txt` は**リリース時に `uv export` で生成**する産物（v0 はプレースホルダ）。
起動スクリプトは `uvx --with-requirements requirements-hive.txt subaco-hive==<版>` で推移依存（zvec / fastembed / mcp）を固定する。
生成手順は `requirements-hive.txt` 冒頭のコメントを参照（`uv export --extra all --no-dev --frozen …`）。

## スキーマ

`subaco_hive.db` が保持する SQLite テーブル:
`members` / `messages` / `mcp_posts` / `message_reads` / `message_notified` /
`processed_requests` / `memories` / `hive_meta` / `audit`。
`hive_meta` に `schema_version` / `active_collection`（初期値 `hive_{team}`）/ `embedding_model` / `embedding_dim` を保存し、
起動時に `schema_version` を照合する（不一致・未初期化は fail-closed）。

## セキュリティ境界（要点）

- author / trust ラベルは「同一 UID 内の協業エージェント間の**出所追跡**」であり、認証済み ID ではない。
- 中核防御はサーバー側機構（trust フィルタ・未信頼本文の非配送・秘密パターン検査・本文の乱数タグ付きデリミタ）。
  出所ラベルや `.agents/core.md` の階層化ポリシーは**勧告的な defense-in-depth**。
- 監査ログ（audit）には**本文を記録しない**（`build_summary` が本文系キーを機械的に弾く）。
- `.hive/` は gitignore（記憶・メッセージはマシン間を移動しない。単一マシン前提）。

## ライセンス

Apache-2.0（`LICENSE` を参照。3 リポジトリ共通）。
