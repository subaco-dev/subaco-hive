# subaco-hive リリース手順（M1-11）

semver タグ push で `.github/workflows/release.yml` がビルド・検証・PyPI 公開（Trusted
Publishing）・固定 requirements 生成までを行う。人手の手順は以下のみ。

## 初回のみ（リポジトリ・PyPI の下準備）

1. GitHub リポジトリを作成して push する（`gh repo create ponponusa/subaco-hive --public`）。
2. PyPI で **パッケージ名 `subaco-hive` の可用性を確認・確保**する（実装計画書 M0-1 の残作業。
   初回公開が名前確保を兼ねる）。
3. PyPI → Account settings → Publishing → **pending publisher** を登録:
   - PyPI Project Name: `subaco-hive`
   - Owner / Repository: `ponponusa` / `subaco-hive`
   - Workflow name: `release.yml`
   - Environment: `pypi`
4. GitHub リポジトリ → Settings → Environments → `pypi` を作成
   （必要なら protection rules で承認者を設定）。

## 毎リリース

1. バージョンを 2 箇所同時に上げる（release.yml が一致をゲートする）:
   - `pyproject.toml` の `[project] version`
   - `subaco_hive/_version.py` の `__version__`
2. `pyproject.toml` の optional-dependencies の下限を見直す（TODO コメント参照。
   厳密ピンは requirements 側で行うため、ここは下限のままでよい）。
3. コミットして semver タグを push:

   ```sh
   git commit -am "release: v0.1.0"
   git tag v0.1.0
   git push origin main v0.1.0
   ```

4. Release ワークフローの green を確認（バージョンゲート → 全テスト → build → publish）。

## 公開後（subaco テンプレートへの反映）

1. Release 添付（artifact `dist`）の `requirements-hive.txt` を取得し、subaco リポジトリの
   `templates/multi-agent/requirements-hive.txt` を実ピンで置き換える
   （ローカル生成する場合: `just export-reqs`）。
2. `templates/multi-agent/wrappers/hive-mcp.sh` の固定版（`subaco-hive==<版>`）を更新する。
3. subaco 側で smoke CI が green になることを確認してコミットする。

## 備考

- requirements にはプロジェクト自身を含めない（`--no-emit-project`）。起動スクリプトが
  `uvx --with-requirements requirements-hive.txt subaco-hive==<版>` の形で版を固定する（設計書 §2.3）。
- fastembed のモデル出力は版によって変わり得る（mean pooling 変更の前例——04_spike結果 §3）。
  requirements の再生成でモデル系依存が上がった場合は `hive reembed` の案内を CHANGELOG に含める。
