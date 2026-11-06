# テスト

このプロジェクトはPython標準の`unittest`とStreamlitの`AppTest`を使います。保存・復元、競合、分類と関連ID、計画の取り込み、入力の部分更新などを、架空データと一時フォルダで確認します。

## 基本の実行

Python 3.11の仮想環境へ、READMEに従って固定した基本依存を導入した後、ソースのルートで実行します。

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py --suite core
```

個々のテスト名も表示する場合：

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py --suite core --verbosity 2
```

`core`では外部ツールが必須の4モジュールだけを除き、除外名と理由を最初に表示します。除外をテスト成功として数えません。実行したテストの失敗・エラー・`unittest`によるスキップは、末尾の`SUMMARY`で確認できます。終了コードは成功0、テスト失敗1、準備状態の不備2です。

テストは個人記録のないソースコピーで実行してください。`scripts/init_local_data.py`が作る次の4ファイルは、正確に空の初期値である場合だけ許可します。

- `notes_data/notebooks.json`：空のノートブック一覧。
- `master_data/master.json`：プロジェクト・科目・教材・単元・タグがすべて空。
- `flashcards.json`、`reminders.json`：空配列。

既存の記録・設定JSONやデータ保存先のリンクがあれば実行を止めます。未知のJSONは内容を開かず、既知4ファイルだけを空値確認のために読みます。個人の記録を削除して通すのではなく、新しいソースコピーを使ってください。

## 外部ツールを使うテスト

| モジュール | 必要な準備 | 確認すること |
|---|---|---|
| `test_media_conversion.py` | FFmpeg／FFprobe | 架空動画からの音声選択・変換、失敗・中止・出力保護 |
| `test_media_conversion_ui.py` | FFmpeg／FFprobe | 架空のメディア素材を使う画面操作 |
| `test_transcription_process.py` | FFmpeg／FFprobe | 実プロセスの開始・中止・途中失敗。認識器はテスト用の偽物 |
| `test_ocr_process.py` | OCR専用依存 | 実プロセス・PDF／画像処理・中止。認識器はテスト用の偽物 |

FFmpegの素材生成にはMPEG-4、AAC、MP3（libmp3lame）等の対応が必要です。検出方法は[メディア変換ガイド](media-conversion.md)を参照してください。

OCR専用依存は、同じPythonから明示的に導入します。ネット接続が必要です。

```powershell
.\.venv\Scripts\python.exe app_core/setup_ocr.py
```

準備した後、外部ツールを使う4モジュールだけを実行する場合：

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py --suite integration
```

全モジュールを実行する場合：

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py --suite all
```

`integration`／`all`では、必要な外部ツールがなければテストのエラーとして報告します。認識モデルの品質比較や、実際の教材・講義の認識精度を検証するスイートではありません。Google連携も偽の通信・認証ストアを使い、実アカウントの認証や送信とは区別します。

## 主な検証範囲

- JSONの読み書き、原子的な更新、競合、更新待ち、操作単位の保存・復旧。
- 勉強セッションの追加・変更・削除・復元と、ノート・日別タスク等の参照保持。
- 日付・欠測・0の扱い、分類マスタ、集計・出力の値と範囲。
- ロードマップの工程・期限・配分・前提関係、AI計画JSONの検証・重複防止。
- ノート・日記・タスク等の入力保持と部分更新。
- Google連携の送信プレビュー・対象検証・再試行・認証情報の表示抑制。
- 任意ツールの保存・ジョブ管理・中止・履歴・出力。
- 配布許可リスト、認証情報や個人ファイルの混入防止、ZIP照合の検査コード。

## 実行環境による違い

Windows向け配布を対象としているため、PowerShellやWindows固有のパス表記を使うテストがあります。PowerShell・Git・Node.jsがない環境では、一部のテストが明示的にスキップされます。終了コードだけで全件を実行したと判断せず、スキップ件数と理由を確認してください。

`scripts/smoke_timer_partial_updates.py`、`smoke_page_performance.py`、`smoke_phase1_analysis.py`は既存テストが使う補助です。個人JSONをコピーせず、ソースと架空データから一時環境を作ります。そこでの読み込み回数や時間は、利用者のPCでの応答速度を保証するベンチマークではありません。

## テストで保証しないこと

AppTestはサーバー側の状態と画面要素の検証が中心です。ブラウザの小窓、ドラッグ操作、自動の1秒更新、別Windows端末での初回セットアップ、実Google認証・送信、実際の音声・画像の認識結果は、それぞれ別の確認が必要です。

また、自動テストの成功は、すべての操作が分かりやすいことや、既知の制限が解消済みであることを意味しません。[既知の制限](known-limitations.md)も参照してください。
