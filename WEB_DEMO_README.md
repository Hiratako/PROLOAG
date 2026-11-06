# PROLOAG Web体験版

学習計画・時間計測・ノート・復習と、生活記録まで試せる個人開発アプリのWeb Demoです。

公開URL: https://proloag-demo.streamlit.app/

このブランチは `codex/web-demo` です。ローカル版の公開用ソースは `main` にあります。PROLOAG本体の公開ライセンスは未確定です。`LICENSE_STATUS.md` を参照してください。

## 体験できること

- 今日の記録・計画、学習ロードマップの作成・編集、タイマー、手動勉強ログ。
- ノート、カード、復習リマインダー、知識都市、学習蓄積の可視化。
- 日記、服薬、睡眠・体調、運動の記録と分析。
- マスタ・設定、統計演習、サンプルの取り込み・CSV/ZIP出力、整合性チェック。

元の25個の非Hobbyページに、はじめに・紹介ページを加えた27画面です。Hobby & Toolsのメディア変換・文字起こし・OCRは搭載しません。Google連携は手順説明のみで、認証・外部送信は実行しません。

## 試す順番

1. 「今日の記録・計画」でタスクを追加・完了します。
2. 「秒給カウンター」で短い時間を計測して保存し、「勉強ログ」で確認します。
3. 「ノート・知識ベース」「記憶フラッシュ」で知識を整理します。
4. 「学習の分析」「生活・健康の分析」で記録を振り返ります。

## 一時データについて

初期サンプルは、2026年11月5日を基準にした架空180日分です。実ユーザーの記録・認証情報は含みません。

Web体験版の初期成分は「カフェイン」「アセトアミノフェン」の2種類です。元のDemo ZIPの架空データを基に、Web展示用に成分だけを調整しています。元の3種類の薬の例は初期記録から除き、ほかの学習・生活記録とカフェイン記録は保持しています。体験中に独自の成分を追加する機能は利用できます。

訪問者ごとに一時データを作成します。追加・編集・削除はこのセッションだけに反映し、共通サンプルや他の訪問者へ反映しません。メニュー移動中は操作内容を引き継ぎます。サイドバーの「体験をリセット」で初期状態へ戻せます。

永続保存は行いません。再読み込み、接続切れ、Cloudの再起動等で操作内容が失われる場合があります。体験用サンプル入力をご利用ください。個人情報や実際の健康記録を保存する場所ではありません。

## 技術と検証

Python 3.11 / Streamlit 1.57.0。既存ページと保存サービスを活用し、訪問者ごとの保存先解決・境界検査・容量制限を加えています。共通の715 JSONはマニフェストで検証し、不一致時は開始しません。

- [構成と境界](docs/web-demo-experience.md)
- [配置方法](docs/web-demo-deploy.md)
- [検証記録](docs/web-demo-verification.md)
- [Web版の成分サンプルと今回の検証](docs/web-demo-substances-verification.md)
- [元のローカル版の説明](docs/local-source-readme.md)

Windowsでの起動例：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run main.py --global.developmentMode false --server.address 127.0.0.1 --server.port 61971
```

テスト：

```powershell
.\.venv\Scripts\python.exe scripts/verify_web_demo.py
```

第三者ソフトの利用条件は `THIRD_PARTY_NOTICES.md` と `licenses/` にあります。
