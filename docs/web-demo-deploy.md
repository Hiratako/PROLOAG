# Web展示用DemoをCommunity Cloudへ配置する

この手順は、用意したWeb展示用Demoを本人の操作で配置するための説明です。パッケージの作成・ローカル検証と、GitHubへの登録・一般公開は別の作業です。

## 現在の状態

- GitHubの `Hiratako/PROLOAG` には、ローカル版の公開用ソースを登録済みです。
- 通常版・Demo版のWindows ZIPとチェックサムは、PrivateリポジトリのRelease下書きに登録済みです。
- このWeb展示版の変更は、それらへ自動反映されません。
- PROLOAG本体の利用条件は未確定です。第三者ソフトの条件とは分けて、[利用条件の状態](../LICENSE_STATUS.md)を確認してください。

## 1. ローカルで確認する

[Web展示版README](../WEB_DEMO_README.md)に従って `main.py` を起動します。

ホームから各展示画面へ移動し、ガントチャートの検索や期間変更、分析の絞り込みを試します。架空データの基準日が2026年11月5日と表示され、保存・削除・取り込み・外部送信の操作が公開されていないことを確認します。

## 2. GitHubでどのソースを使うか決める

Web展示版は、元のWindows用配布ソースと識別できる専用ブランチ等へ登録します。新しいブランチを使う場合は、例えば `codex/web-demo` とします。**現在のmainやRelease下書きを、確認せずにWeb展示版で上書きしないでください。**

ブランチを分けても、リポジトリの公開範囲は共通です。公開リポジトリの中に個人記録や秘密情報を置くための方法にはなりません。

登録するのは展示用ソースと明示された合成サンプルです。`.venv`、導入済みライブラリ、Pythonランタイム、実際の個人記録、認証情報、過去の配布ZIPは登録しません。GitHub Desktop等を使う場合は、サンプルデータが`.gitignore`で除外されていないことと、公開対象のファイル一覧を確認します。

## 3. 公開範囲とStreamlitへの権限を確認する

Community Cloudのアプリ公開範囲と、GitHubリポジトリの公開範囲は別の設定です。Privateソースをもとに作ったアプリを、Cloud側で公開することもできます。

ただし、Streamlit公式資料では、**Privateリポジトリとの接続に追加の広いGitHub `repo` OAuth権限が必要**と説明されています。現在のCodex用GitHubプラグインでPROLOAGだけを選択していても、その制限がStreamlitの新しい接続へ引き継がれるわけではありません。

「PROLOAG以外の非公開リポジトリへアクセスを与えない」という方針なら、Private接続の追加認可をそのまま許可せずに止めてください。公開用に整えたソースをPublicリポジトリから配置する方法も含め、接続画面で実際に要求される権限を確認します。トークンやパスワードをチャットへ貼る必要はありません。

作品をブラウザで見てもらう場合は、アプリ側は閲覧者が開ける公開設定にします。ソースをPublicにするかは、別途本人が決めます。今回は公開操作を実施していません。

## 4. Community Cloudにアプリを作る

1. [Community Cloud](https://share.streamlit.io/)へ本人のアカウントでログインします。
2. GitHubとの接続・権限の表示を確認します。意図しないアクセス拡大があれば、承認せずに止めます。
3. アプリの新規作成から、GitHubリポジトリを指定する方法を選びます。
4. リポジトリ・Web展示用ブランチ・入口ファイルを指定します。
5. Advanced settingsでPythonを **3.11** に設定します。
6. 設定を確認してデプロイします。合成サンプルを見る展示には、Google認証やAIサービスのAPIキーを登録しません。

| 項目 | 指定するもの |
|---|---|
| Repository | Web展示版を登録したリポジトリ |
| Branch | Web展示用に登録したブランチ |
| Main file path | `main.py` |
| Python version | `3.11` |
| Secrets | この展示用途では追加不要 |

Community CloudはLinux上でソースを起動します。Pythonやライブラリのバージョンを自己判断で変更せず、まず同梱の `requirements.txt` を使います。ビルドに失敗した場合はログを確認します。Windowsのローカル検証だけでLinuxの成功は保証できません。

## 5. Cloud上で確認する

- アプリが起動し、各展示ページ・グラフ・ガントチャートが表示される。
- 期間や分類の変更ができ、別のブラウザで開いた訪問者の表示条件が勝手に変わらない。
- 保存・削除・取り込み・Google接続等の操作が利用できない。
- ブラウザを更新しても固定の架空サンプルを確認できる。
- ログにエラーがなく、画面に個人パスや秘密情報が表示されない。

公開する場合は、CloudのSharing設定で閲覧範囲を確認し、本人が公開操作を行います。最後にログアウト状態やシークレットウィンドウで開き、閲覧者として表示できることを確認します。

## 6. 作品紹介へリンクを付ける

Cloud上の確認後、GitHubのREADMEへ「ブラウザで体験」のリンクを追加します。URLは実際に発行されたものを使用し、仮のURLを公開しません。求人等へはGitHubのトップページを案内し、そのページからWeb Demoへ進める構成にします。

無料のCommunity Cloudにはリソース上限があり、公式資料では12時間アクセスがないアプリの休止が説明されています。初回の起動待ちに備えて、READMEには画面画像と短い機能説明も載せます。重い文字起こし・OCR・動画変換を展示用に有効化する必要はありません。

## この版の範囲

この展示は、固定の架空データを使った閲覧・表示条件の操作を対象にしています。訪問者ごとの保存・編集・リセットを提供する体験版は別の開発段階です。本番用の個人記録保存サービスや、複数ユーザー向けの認証付きサービスとして公開しないでください。

## 公式資料

- [依存関係の指定](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies)
- [GitHubアカウントの接続](https://docs.streamlit.io/deploy/streamlit-community-cloud/get-started/connect-your-github-account)
- [GitHub OAuth権限と制限](https://docs.streamlit.io/deploy/streamlit-community-cloud/status)
- [アプリの共有・公開範囲](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app)
- [アプリ管理・休止](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app)
