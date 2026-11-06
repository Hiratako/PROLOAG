# Community Cloudの配置

このWeb体験版の指定値：

| 項目 | 指定値 |
|---|---|
| Repository | Hiratako/PROLOAG |
| Branch | codex/web-demo |
| Main file path | main.py |
| Python | 3.11 |
| Secrets | 空 |
| App URL | proloag-demo（既存の公開URL） |

公開URLは https://proloag-demo.streamlit.app/ です。既存アプリは展示用ブランチを監視しているため、そのブランチの更新が反映対象です。`main` のローカル用起動設定へ切り替えないでください。

`main` にはローカル用のlocalhost/8502設定があります。Web用のこのブランチはアドレス・ポートを固定せずCloud側に従います。

初回の配置にはStreamlit管理画面のCreate appを使います。ZIPのアップロード、mainへのマージ、ReleaseのPublishは不要です。アプリの起動元ブランチを間違えた場合は、Cloud側の対象アプリを削除・再配置します。GitHubリポジトリやPCの記録を削除する操作ではありません。

Publicリポジトリからの配置に、Privateリポジトリ用の追加権限は不要です。GitHubをPrivateに保った配置も技術上は可能ですが、Streamlit側の追加repo OAuth権限とアプリの共有設定を別に検討します。

## 表示の確認

はじめに・今日の計画・タイマー・勉強ログ・ノート・カード・ロードマップ・生活記録・分析・設定等を開き、サンプルを保存して戻っても保持することを確認します。別ブラウザでは同じ変更が見えないこと、リセットすると初期状態に戻ることを確認します。

再配置後にJavaScriptの動的読み込みエラーが出る場合は、開いたままのタブを閉じ、再読み込みや別ブラウザで切り分けます。キャッシュ・拡張機能・配信の問題など、原因を確認せず一律にコードのバージョンを変更しません。

公式: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy
