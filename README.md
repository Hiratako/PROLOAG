# PROLOAG

**Progress & Reflection OS for Learning, Organization, And Growth**

今日の記録を、次の物語へ。

PROLOAGは、学習計画・計測・ノート・復習と、睡眠・体調・運動などの生活記録をまとめる個人開発アプリです。中期の計画を日々のタスクと学習実績につなぎ、記録を振り返ることを目的にしています。Python / Streamlitを用い、生成AIの支援も活用して開発しています。

**[Web体験版を開く](https://proloag-demo.streamlit.app/)** · [Web体験版のソースコード](https://github.com/Hiratako/PROLOAG/tree/codex/web-demo)

Web体験版はインストール不要です。架空180日分のデータを使い、計画・記録・ノート・復習・分析などの主要機能をブラウザで試せます。追加・編集・保存は閲覧者専用の一時データへ反映され、共通サンプルや他の閲覧者には影響しません。操作内容は永続保存されないため、実際の記録の保管用ではありません。

Web体験版にはHobby & Toolsを含めず、Google連携による接続・データ送信も行いません。Web公開向けの構成は `codex/web-demo` ブランチで管理しています。

> ソースコードをプレビュー版として公開しています。PROLOAG本体の公開ライセンスは未確定です。[LICENSE_STATUS.md](LICENSE_STATUS.md)を確認してください。

このREADMEと `main` ブランチは、アプリを実行するPC内のJSONへ記録を保存するローカル版の説明・ソースです。Streamlitを使い、ブラウザで操作します。Google連携は任意の手動操作で、基本機能にGoogleアカウントやAIのAPI接続は不要です。

## できること（ローカル版）

| 分野 | 主な機能 |
|---|---|
| 学習の計画と実行 | 今日のタスク、繰越、ガント形式のロードマップ、日別配分、AIが作成した計画JSONの確認・取り込み |
| 学習時間の記録 | 秒給カウンター、Chrome/Edge対応の小窓、一時停止・再開、手動ログ、編集・削除・復元 |
| 知識と復習 | ノート、カード、復習リマインダー、知識都市、学習蓄積ビジュアライザー |
| 生活記録と振り返り | 日記、服薬・カフェイン、睡眠・体調、運動、生活と学習の推移・条件比較 |
| データの利用 | 全記録CSV・ZIP、旧版勉強ログの移行、データ整合性チェック、マイデータ統計演習 |
| Hobby & Tools | MP4→MP3、日本語・英語の文字起こし、画像・PDFの文字読み取り（追加準備が必要） |

ポータルを含む29ページを、目的別のナビゲーションにまとめています。すべての機能を使わず、必要な記録から始められます。

## 画面例

![学習ロードマップの架空Demo](docs/images/roadmap-demo.png)

架空Demoの画面例です。2026年11月5日を基準にしたサンプル計画で、実際の試験日程・学習成果を示すものではありません。

## 試す方法を選ぶ

| 方法 | 用途 | 公開状況・必要な準備 |
|---|---|---|
| [Web体験版](https://proloag-demo.streamlit.app/) | 架空データで主要機能をブラウザから試す | 公開中。インストール不要。閲覧者専用の一時データを使用 |
| このソース（main） | コードを読む・自分のPCでローカル版を動かす | 公開中。実行にはPython 3.11と依存ライブラリの導入が必要 |
| Windows通常版ZIP | 自分の記録を空の状態から始める | 作成済み・一般配布は未開始。Python本体同梱、初回ライブラリ導入時はネット接続が必要 |
| Windows Demo版ZIP | 架空データでローカル版の画面・入力・分析を試す | 作成済み・一般配布は未開始。180日分の架空データと少量のロードマップを収録 |

Windows通常版・Demo版ZIPは作成済みですが、一般配布はまだ開始していません。GitHubのReleasesは下書き段階で、公開済みZIPのダウンロード先はありません。ブラウザで試す場合は公開中のWeb体験版をご利用ください。このmainブランチから得るソースには、Pythonランタイム・導入済みライブラリ・個人記録を含めません。

## ソースから起動する

このmainブランチのローカル版の検証基準は **Windows x64 / Python 3.11** です。ローカル版のほかのOSでの動作は今回の検証範囲に含めていません。

ソースを新しい書き込み可能なフォルダへ配置し、そのフォルダでPowerShellを開いて実行します。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-release.lock.txt
.\.venv\Scripts\python.exe scripts/init_local_data.py
.\.venv\Scripts\python.exe -m streamlit run main.py --server.address localhost --server.port 8502
```

`init_local_data.py`は空の初期JSONと必要なフォルダを用意し、既存ファイルを上書きしません。準備が終わったら、ブラウザで `http://localhost:8502/` を開きます。終了は起動したコンソールで Ctrl+C を押します。同じポートで別のアプリが起動している場合は、対象を確認してから、未使用のポートを手動指定して使い分けてください。

Windowsでは`run_app.bat`からも起動できます。READMEの初期セットアップ後、この公開用ランチャーはPython 3.11の専用`.venv`を使います。依存は初期セットアップで固定定義から導入します。

`requirements-release.lock.txt`はWindows/Python 3.11で確認した基本依存の固定定義です。ソース用の`requirements.txt`と、任意機能用の依存定義も分けています。

初回は「マスタ管理」で分類、「ユーザー設定」で入力初期値を確認し、「今日の記録・計画」や「秒給カウンター」から記録できます。詳しい操作はアプリ内の「使い方ガイド」にあります。

## 追加準備が必要な機能

- [メディア変換](docs/media-conversion.md)：FFmpeg/FFprobe。
- [文字起こし](docs/transcription.md)：FFmpeg/FFprobe、専用エンジン、初回モデル取得。
- [画像・PDFの文字読み取り](docs/ocr.md)：専用エンジン・モデル等。
- [Google Sheets共有](docs/google-sheets-setup.md)・[Google Docs相談レポート](docs/google-docs-reports.md)：任意依存と、自分のGoogle設定・認証。

これらを準備しなくても、基本の学習・生活記録を利用できます。認証情報・モデル・教材や音声の原本は公開ソースに含めません。

## 構成と設計の見どころ

- ページ、機能サービス、共通UI部品を分け、保存や集計を共用しています。
- JSONの版確認・排他・一時ファイルからの置換を使い、複数ファイルの操作にはジャーナルと復旧処理を用意しています。
- タイマーの秒表示と入力を部分更新で分け、入力のたびに周囲の集計を実行しない構成を使っています。
- ロードマップは期間・数量進捗・記録時間を分け、AIからの取り込みもプレビュー確認後に保存します。
- 時間のかかる変換・認識はバックグラウンド処理へ分け、進捗・中止・結果の確認を用意しています。

具体的なモジュールと境界は[アーキテクチャ](docs/architecture.md)、変更時の進め方は[開発ガイド](docs/development.md)にまとめています。

## テストと配布

自動テストは`unittest`を使っています。保存・競合・入力保持・集計・取り込み・配布物の除外などを、架空データと一時ディレクトリで確認します。[テスト手順と範囲](docs/testing.md)を参照してください。

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py --suite core
```

FFmpegやOCR専用依存が必要な検証は別のintegration群です。使用済みの実記録があるフォルダをテスト対象にせず、テストガイドに従って実行してください。

Windows通常版ZIPとWindows Demo版ZIPは同じアプリコードを使い、初期データとDemo設定を分けます。ビルド・Demo生成・検査の手順は[配布物の作成](docs/building.md)にあります。Web体験版は、公開向けに機能範囲と一時保存の仕組みを調整した別ブランチです。過去の配布検証結果を、今後の変更すべての動作保証とは扱いません。

[初期公開用ソースの検証記録](https://github.com/Hiratako/PROLOAG/blob/66511380cf03a0c9b35b7fd7e5ca9baafebf565f/docs/verification.md)と[SOURCE_MANIFEST.json](https://github.com/Hiratako/PROLOAG/blob/66511380cf03a0c9b35b7fd7e5ca9baafebf565f/SOURCE_MANIFEST.json)は、2026年11月6日の準備時点の情報です。READMEと利用条件の状況説明は公開後の案内に合わせて更新しています。これらの文書更新を、アプリの再検証や新しい利用許諾の付与として扱うものではありません。

## 記録の保管と既知の制限

記録はプロジェクト内の`study_logs/`、`daily_plans/`、`notes_data/`、`settings/`等へ保存します。使用後のフォルダには個人情報が残るため、Gitへ登録・再配布するのは公開用ソースと未使用の配布物だけにしてください。CSV・ZIP出力は一覧・分析用で、アプリ全体の完全復元機能ではありません。

この版には入力・表示・編集導線の既知の制限があります。[既知の制限](docs/known-limitations.md)を確認してください。分析は記録上の傾向を示し、因果関係・理解度・健康状態を確定するものではありません。

## 利用ガイド

- [学習ロードマップ](docs/study-roadmap.md)
- [AI計画の取り込み](docs/roadmap-ai-import.md)・[GPT/Geminiへ渡す指示書](docs/ai-roadmap-import-prompt.md)
- [旧版勉強ログの移行](docs/legacy-study-import.md)
- [相談提案の取り込み](docs/consultation-proposals.md)

## ライセンス

PROLOAG本体の公開ライセンスは未確定です。[LICENSE_STATUS.md](LICENSE_STATUS.md)を参照してください。第三者ソフトの利用条件・表示は、そのソフトに付属する文書を基準にしてください。
