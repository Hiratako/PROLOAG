# PROLOAG Web展示用Demo

架空の学習・生活記録を使い、PROLOAGの計画・分析・可視化をブラウザで見てもらうための展示版です。Streamlit Community Cloudへの配置を想定しています。

**現在はローカルで作成・検証するためのパッケージです。このファイルがあることは、GitHubへの反映やWeb公開が完了したことを意味しません。** PROLOAG本体の利用条件は未確定です。[LICENSE_STATUS.md](LICENSE_STATUS.md)を確認してください。

## 最初に見る順番

1. ホームで、何をまとめて管理するアプリかを確認します。
2. 学習ロードマップで、工程・期間・進捗と記録済み時間のつながりを見ます。
3. 学習ダッシュボードで、期間や分類を変えて学習の傾向を見ます。
4. 学習蓄積・知識都市で、学習内容の積み重ねを見ます。
5. 生活・健康ダッシュボードで、睡眠・体調・運動と学習の記録を見ます。

表示基準日は **2026年11月5日** に固定しています。180日分の記録と学習計画はすべて合成したサンプルで、実在する人の記録・試験結果・健康情報ではありません。

## この展示版でできること

- ページの切り替え、期間や分類の絞り込み、グラフの操作。
- ガントチャートの表示切り替え、工程の検索・詳細確認。
- 画面に用意されている、架空データのCSVダウンロード。

保存・削除・分類マスタの変更・ファイル取り込みは提供しません。Googleへの接続・送信、動画変換、文字起こし、OCR、管理・復旧用の操作も展示版では提供しません。これらを説明する場合は、Web展示の機能とローカル版の機能を区別してください。

訪問者が変えるのは表示条件です。共通サンプルの保存先は変更できません。この版を自分の記録の保存先として利用しないでください。

## 手元のPCで起動する

新しいフォルダにこのパッケージを配置し、そのフォルダでPowerShellを開きます。Python 3.11を使用します。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run main.py --server.address 127.0.0.1 --server.port 61970 --server.headless true
```

準備が終わったら、ブラウザで `http://127.0.0.1:61970/` を開きます。終了は起動したPowerShellで Ctrl+C です。61970番が使用中なら、未使用の別ポートを指定してください。既に使っている通常版やDemo版を終了・上書きする必要はありません。

**通常版の `init_local_data.py` やDemo再生成・配布作成のスクリプトを、展示版の起動準備として実行しないでください。** 展示用のサンプルはこのパッケージに用意します。

## Community Cloudへ配置する

入口ファイルは **`main.py`**、Pythonは **3.11** を指定します。Windows用の `.exe`、ランタイムや配布ZIPをCommunity Cloudへアップロードする方式ではありません。

GitHubの公開範囲、接続時の権限、画面上の設定は、[初心者向けの配置手順](docs/web-demo-deploy.md)を参照してください。GitHubの元のPROLOAGソースと、Windows配布用のRelease下書きは、このパッケージの作成だけでは変更されません。

## 検証の範囲

このパッケージでは、ローカルの画面表示・表示条件の操作・書き込み拒否・データ不変性を確認します。結果は同梱する検証記録を基準にしてください。

展示版向けの検証は、初期セットアップ後に専用コマンドで実行できます。

```powershell
.\.venv\Scripts\python.exe scripts/verify_web_demo.py
```

展示版の単体テストだけを実行する場合は、次のコマンドを使います。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_web_demo_*.py" -v
```

元のローカル版から引き継いだ保存・編集用テストは、書き込みを拒否する展示版の検証対象ではありません。元の公開用ソースで確認したテスト件数を、この展示版での実行結果として表示しないでください。

Linux環境および実際のCommunity Cloudでの起動・同時アクセスは、ローカルのWindows検証とは別です。Community Cloud上のビルドと画面確認を終えるまでは、「Cloudで動作確認済み」と表示しないでください。

Community Cloudのローカルファイルは永続保存が保証されず、アクセスがないとアプリが休止する場合があります。展示は同梱した固定サンプルから再開できる構成とし、READMEの画面画像も作品説明として残します。

## 参考

- [Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud)
- [Cloudの依存関係](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)
- [CloudのGitHub権限と制限](https://docs.streamlit.io/deploy/streamlit-community-cloud/status)
- [Cloudのアプリ管理・休止](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)
