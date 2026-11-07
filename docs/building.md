# Windows配布ZIPを作る

このソースには、通常版と中小企業診断士Demo版を作る補助を収録しています。GitHubへのアップロード・公開は行いません。

通常版は記録が空で、医学・英語のサンプル分類のみです。Demo版は同じアプリコードに、専用生成器の架空データ・固定日・少量のロードマップを追加します。使用中の記録・設定を配布用データとしてコピーしません。

## 前提

- Windows x64と、標準インストール版のPython 3.11 x64。
- Windows PowerShellと.NETのC#コンパイル機能。軽量な`PROLOAG.exe`を生成します。
- リポジトリの公開用ファイル一式。Pythonランタイムやインストール済み依存はソースに含まれません。
- 本体の利用条件はルートの`LICENSE`（PROLOAG 利用条件（販売・有料提供禁止）第1版／`LicenseRef-PROLOAG-No-Sale-1.0`）です。配布ZIPにも同じLICENSEを同梱します。利用・複製・改変・無料再配布は可能ですが、ソフトウェアまたは利用権への対価を伴う提供は禁止します。

ビルドは新しい`release/`配下だけに出力します。既存の同名フォルダ・ZIP・チェックサム・ビルド結果がある場合は、上書きせず停止します。失敗時も既存配布物を削除せず、新しい版名でやり直すか、作業者が生成途中のフォルダを確認してください。未公開下書きの添付更新でも、この上書き防止は変えません。同名出力がない別のクリーンな作業場所から作り、元のZIPや検証資料は保管してください。

## 読み取り専用で入力を確認する

リポジトリのルートで実行します。

```powershell
py -3.11 scripts/build_public_release.py --check --version 0.1.0-preview.2
```

必要な公開ファイル、ライセンス状態、版名、出力先の重複を確認します。ランタイム生成、データ保存、ZIP作成、ネット通信は行いません。`--help`で全引数を確認できます。

## 通常版とDemo版を作る

以下は、2026年11月7日に、公開前の`0.1.0-preview.2`下書きを利用条件入りの内容へ更新した際の作成例です。当時は未公開で添付の取得がないことを確認し、版番号を増やさずZIPとチェックサム等を差し替えました。現在は[同版のRelease](https://github.com/Hiratako/PROLOAG/releases/tag/v0.1.0-preview.2)を公開済みです。公開済み版を変更する場合は、新しい識別版を使用してください。

```powershell
py -3.11 scripts/build_public_release.py --version 0.1.0-preview.2 --reference-date 2026-11-05 --days 180 --seed 20261105
```

既定では両方を生成します。通常版だけなら`--variant normal`、Demoだけなら`--variant demo`を指定します。Pythonが複数ある場合は、`--python "<Python 3.11 x64のpython.exe>"`で明示できます。

Demoの基準日は、その版で再現できるよう固定します。基準日・乱数seed・日数は生成manifestへ記録します。ロードマップには3科目9工程、節目2件、今日/翌日のタスクと少数の既存記録の関連を追加します。実際の試験日程や学習成果ではありません。

出力例：

```text
release/
├─ proloag-v0.1.0-preview.2-windows-x64/
├─ proloag-demo-consultant-v0.1.0-preview.2-windows-x64/
├─ proloag-v0.1.0-preview.2-windows-x64.zip
├─ proloag-demo-consultant-v0.1.0-preview.2-windows-x64.zip
├─ 各ZIPの.sha256
├─ SHA256SUMS-proloag-v0.1.0-preview.2.txt
└─ BUILD_RESULT-proloag-v0.1.0-preview.2.json
```

配布用Python本体を同梱します。通常の初回起動では、`app/requirements-release.lock.txt`から基本ライブラリをネット経由で導入します。開発環境の`.deps311`・venv・モデル等をZIPへコピーする方式ではありません。

## 行う検査

標準のビルドでは、許可したソースの一致、JSONの形式、通常版の厳密な空データ、Demoの生成manifestと全JSONハッシュ、リンク/再解析ポイント、個人データ・認証・キャッシュ等の禁止ファイル名、同梱Markdownのリンクを確認します。両版を作った場合はPythonコードとCSSの一致を確認します。

ZIPは一時的な`.zip.partial`として生成し、全ファイル一覧・CRC・各ファイルのSHA-256を照合できてから正式名へ移します。失敗した`.partial`を公開しないでください。

検証済み試作ZIPの版表示・配布manifest・チェックサム等だけを調整して組み直す場合は、アプリ・Demoデータ・Pythonランタイム・LICENSEのバイト列が変わらないことを確認します。継承する画面検証と、組み直したZIPでやり直した静的検査の範囲を分けて記録します。

静的検査だけでは、実画面の動作や別PCでの初回導入まで検証したことにはなりません。アプリ画面の読み取り専用検査も行う場合は、ルートの`.deps311`へ固定依存を準備し、次のように実行します。

```powershell
py -3.11 -m pip install --target .deps311 -r requirements-release.lock.txt
py -3.11 scripts/build_public_release.py --version 0.1.0-preview.2 --reference-date 2026-11-05 --verify-pages
```

`--verify-pages`は既存の配布検査を呼び、通常版・Demo版ともポータルと現行全ページを自動列挙します。検査中の記録保存・外部通信・外部プロセス起動は防止し、配布内データを変更しない検査です。`.deps311`は検査専用に使い、ソースやZIPには含めません。

別途、ZIPを別フォルダへ展開し、初回セットアップ・保存/再起動・ファイル取得を確認します。Googleの実認証・送信、実際の教材の認識、別Windows端末での導入は、実施した範囲を明記してください。

## 公開する資料と任意機能

配布用README等は`docs/distribution/`から組み込みます。`{{VERSION}}`、`{{REFERENCE_DATE}}`、`{{REFERENCE_DATE_JA}}`をテンプレートに使えます。初期の文書に残る既知の版・基準日の表記も置換しますが、機能説明や既知の制限は作業者が現行実装に合わせて確認してください。

本体の`LICENSE`と、`THIRD_PARTY_NOTICES.md`・`licenses/`・同梱Pythonのライセンスを配布へ含めます。本体の独自利用条件は、第三者ソフトの利用条件を置き換えません。

この共通ビルドはFFmpeg/FFprobe、ASR/OCR専用依存・モデル、Google認証を同梱しません。FFmpegを追加配布する場合は、選んだビルドの再配布条件に応じた資料と検証を別途準備します。

## ソースと過去の配布物の対応

`0.1.0-preview.2`は、成分初期例を統一し、独自の利用条件を同梱して2026年11月7日に公開したプレビュー版です。公開前に同版のRelease下書きの添付を整理しました。`0.1.0-preview.3`のReleaseやタグは作成していません。元の`0.1.0-preview.2` ZIP、利用条件入りの`0.1.0-preview.3`試作ZIP、初期の`0.1.0-preview.1` ZIP、準備時点の`SOURCE_MANIFEST.json`はローカルの履歴資料として保管し、上書きしません。同じアプリソースでも、Python版、EXEコンパイル、ZIPの時刻等でバイト列は変わるため、過去ZIPと完全同一のSHA-256になるとは限りません。

Git管理されたルートから作ると、ソースcommitと未コミット差分の有無をmanifestに記録します。Git外なら未設定です。`source_dirty=true`の配布物を、そのcommitだけから作ったものと説明しないでください。公開時はソース・資料・利用条件の版を確定し、対応タグとZIPをそろえます。
