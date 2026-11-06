"""Read-only, offline-first instructions; no account or record access."""
from __future__ import annotations

import streamlit as st


TOPICS = ("はじめに", "初回設定", "Sheets共有", "Docsレポート", "相談結果を戻す", "困ったとき")
SOURCE_URLS = {
    "Google Cloud Console": "https://console.cloud.google.com/",
    "OAuth同意画面の設定": "https://developers.google.com/workspace/guides/configure-oauth-consent",
    "Sheets・Desktopクライアントの設定": "https://developers.google.com/workspace/sheets/api/quickstart/python",
    "Google Docs APIの設定": "https://developers.google.com/workspace/docs/api/quickstart/python",
    "ファイル単位の権限について": "https://developers.google.com/workspace/sheets/api/scopes",
    "認証トークンの失効条件": "https://developers.google.com/identity/protocols/oauth2#expiration",
}

# Vector diagrams are deliberately schematic, not screenshots of a Google account.
FLOW_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="400" viewBox="0 0 1120 400" role="img">
<title>Google共有と相談の流れ</title><desc>PROLOAGの記録からSheetsまたはDocsへ手動共有し、GPTへは別に依頼文と資料を渡す。返答をPROLOAGへ貼り付け、差分を承認する。</desc>
<rect width="1120" height="400" rx="20" fill="#f2f5fa"/>
<g font-family="Segoe UI,Yu Gothic,Meiryo,sans-serif" fill="#162942">
<text x="32" y="42" font-size="15" font-weight="700" letter-spacing="2">SHARE → DISCUSS → REFLECT</text>
<rect x="32" y="104" width="224" height="130" rx="14" fill="#162942"/>
<text x="54" y="142" fill="white" font-size="24" font-weight="700">PROLOAG</text>
<text x="54" y="175" fill="#dfebf7" font-size="17">学習実績・再開メモ</text>
<text x="54" y="206" fill="#dfebf7" font-size="16">送る内容をプレビュー</text>
<path d="M267 168 H337" stroke="#607691" stroke-width="3"/><path d="M327 161 L338 168 L327 175" fill="none" stroke="#607691" stroke-width="3"/>
<text x="267" y="146" font-size="15">手動送信</text>
<rect x="350" y="80" width="265" height="78" rx="12" fill="white" stroke="#cbd7e6"/>
<rect x="350" y="178" width="265" height="78" rx="12" fill="white" stroke="#cbd7e6"/>
<text x="370" y="111" font-size="21" font-weight="700">Google Sheets</text><text x="370" y="140" font-size="16">日別・科目別の表</text>
<text x="370" y="209" font-size="21" font-weight="700">Google Docs</text><text x="370" y="238" font-size="16">日次・週次の相談文書</text>
<path d="M626 168 H763" stroke="#607691" stroke-width="3" stroke-dasharray="6 5"/><path d="M753 161 L764 168 L753 175" fill="none" stroke="#607691" stroke-width="3"/>
<text x="634" y="126" font-size="15">依頼文をコピー</text><text x="634" y="149" font-size="15">資料を別途渡す</text><text x="634" y="197" font-size="14" fill="#526783">自動送信ではない</text>
<rect x="780" y="104" width="306" height="130" rx="14" fill="white" stroke="#cbd7e6"/>
<text x="802" y="143" font-size="23" font-weight="700">普段のGPTとの相談</text><text x="802" y="176" font-size="16">資格・語学・仕事の学びなど</text><text x="802" y="206" font-size="16">アクセス権も別途確認</text>
<path d="M934 271 V320 H149 V264" fill="none" stroke="#317784" stroke-width="3"/><path d="M142 275 L149 264 L156 275" fill="none" stroke="#317784" stroke-width="3"/>
<rect x="355" y="295" width="451" height="51" rx="25" fill="#e0eff0"/>
<text x="378" y="327" font-size="17" fill="#225b66">返答を貼り付け → 差分を確認 → 自分で採用</text>
<text x="32" y="377" font-size="14" fill="#526783">図解：元の学習記録はGoogleへの共有では変更されません。</text>
</g></svg>"""

SETUP_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="240" viewBox="0 0 1120 240" role="img">
<title>初回設定の3つの場所</title><desc>PCで追加ライブラリを導入、Google CloudでAPIとDesktopクライアントを用意、PROLOAGで接続。接続だけでは学習データを送信しない。</desc>
<rect width="1120" height="240" rx="20" fill="#f2f5fa"/>
<g font-family="Segoe UI,Yu Gothic,Meiryo,sans-serif" fill="#162942">
<rect x="28" y="28" width="323" height="160" rx="13" fill="white" stroke="#ccd8e6"/>
<rect x="399" y="28" width="323" height="160" rx="13" fill="white" stroke="#ccd8e6"/>
<rect x="770" y="28" width="323" height="160" rx="13" fill="white" stroke="#ccd8e6"/>
<text x="49" y="66" font-size="15" fill="#56718d">01 / このPC</text><text x="49" y="103" font-size="23" font-weight="700">ライブラリを用意</text><text x="49" y="135" font-size="17">アプリと同じPythonを選ぶ</text><text x="49" y="163" font-size="16">Google用フォルダへ分離</text>
<text x="420" y="66" font-size="15" fill="#56718d">02 / Google Cloud</text><text x="420" y="103" font-size="23" font-weight="700">API・認証を用意</text><text x="420" y="135" font-size="17">Sheets / Docs APIを有効化</text><text x="420" y="163" font-size="16">DesktopのJSONを取得</text>
<text x="791" y="66" font-size="15" fill="#56718d">03 / PROLOAG</text><text x="791" y="103" font-size="23" font-weight="700">接続して共有先を作成</text><text x="791" y="135" font-size="17">JSONを選び、ブラウザで許可</text><text x="791" y="163" font-size="16">この時点では学習データ未送信</text>
<path d="M362 107 H385 M377 100 L385 107 L377 114 M733 107 H756 M748 100 L756 107 L748 114" fill="none" stroke="#607691" stroke-width="3"/>
<text x="28" y="219" font-size="14" fill="#526783">図解：この3段階は初回に必要です。通常の共有では、期間選択 → プレビュー → 送信だけを行います。</text>
</g></svg>"""

SHARE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="248" viewBox="0 0 1120 248" role="img">
<title>プレビューを確認して送信する3段階</title><desc>期間と含める項目を選ぶ。プレビューで内容を確認する。確認チェック後に送信する。条件を変えたらプレビューを作り直す。</desc>
<rect width="1120" height="248" rx="20" fill="#f2f5fa"/>
<g font-family="Segoe UI,Yu Gothic,Meiryo,sans-serif" fill="#162942">
<rect x="28" y="25" width="329" height="145" rx="14" fill="white" stroke="#ccd8e6"/>
<text x="48" y="60" font-size="16" fill="#56718d">1. 送る範囲を選ぶ</text><text x="48" y="98" font-size="22" font-weight="700">期間・任意項目</text><text x="48" y="135" font-size="16">メモや資料リンクは必要なときだけ</text>
<rect x="395" y="25" width="329" height="145" rx="14" fill="white" stroke="#ccd8e6"/>
<text x="415" y="60" font-size="16" fill="#56718d">2. 送る内容を確認する</text><text x="415" y="98" font-size="22" font-weight="700">プレビューを作成</text><text x="415" y="135" font-size="16">内容と送信先を読み、確認チェック</text>
<rect x="762" y="25" width="329" height="145" rx="14" fill="#162942"/>
<text x="782" y="60" font-size="16" fill="#d7e5f4">3. 自分で実行する</text><text x="782" y="98" font-size="22" font-weight="700" fill="white">Googleへ送信</text><text x="782" y="135" font-size="16" fill="#d7e5f4">成功日時と共有先を確認</text>
<path d="M365 99 H385 M377 92 L385 99 L377 106 M732 99 H752 M744 92 L752 99 L744 106" fill="none" stroke="#607691" stroke-width="3"/>
<text x="28" y="207" font-size="17" fill="#225b66">条件や元の記録を変えたら、プレビューを作り直してください。</text>
<text x="28" y="232" font-size="14" fill="#526783">図解：送信するのは最後に作った固定プレビューです。画面を開いただけでは送信しません。</text>
</g></svg>"""


def _step(number: int, title: str, body: str) -> None:
    with st.container(border=True):
        st.caption(f"STEP {number:02d}")
        st.markdown(f"#### {title}")
        st.markdown(body)


def _overview() -> None:
    st.subheader("まずは、何に使うかを選びましょう")
    st.image(FLOW_SVG, width="stretch")
    st.caption("説明用の図解です。実アカウントのスクリーンショットではありません。")
    st.markdown("""
| やりたいこと | 使う機能 | 接続 |
|---|---|---|
| 日別・科目別の実績を表で見たい | Google Sheets共有 | 送信時に必要 |
| 1日・1週間の振り返りを文章で相談したい | Google Docs相談レポート | 送信時に必要 |
| 教材の資料URL・次の作業を残したい | 資料リンク・引き継ぎ | 不要 |
| 相談の返答を再開メモ・計画へ反映したい | 相談提案の取り込み | 不要 |
""")
    st.info("この連携は、自分で確認して送る『片方向の共有』です。自動同期・Google側の編集取り込み・GPTへの自動送信は行いません。")
    cols = st.columns(2)
    with cols[0]:
        _step(1, "初めてなら", "上の **初回設定** を順番に進めます。Google側の設定が終わったら、**Sheets共有** または **Docsレポート** の手順へ進んでください。")
    with cols[1]:
        _step(2, "まず試したいなら", "Google未接続でも、データエクスポートで **プレビュー** を作成できます。Docsは **レポートをテキストで保存** も可能です。")
    with st.expander("送られるもの・送られないもの"):
        st.markdown("""
- 送信対象は、学習時間・件数などの集計と、本人が選んだ任意項目です。
- Sheetsの学習明細・学習メモ、Docsの資料リンク・再開メモ・学習メモは選択式です。
- 日記・健康・服薬・金額・ノート本文・タスクはこの共有の対象外です。
- 科目名・教材名・相談文に個人情報を書いた場合は送信内容に含まれます。プレビューで確認してください。
- **CSV・ZIPダウンロードは別機能**です。全記録を含むため、Google共有と同じ除外条件にはなりません。
""")


def _setup() -> None:
    st.subheader("初回設定：このPC → Google Cloud → PROLOAG")
    st.image(SETUP_SVG, width="stretch")
    st.caption("Googleへのログイン・許可は、ご自身で操作します。このガイドを開くだけでは接続・インストールは行いません。")
    st.markdown("### 1. Google連携用のライブラリを用意する")
    st.write("起動コンソールの Python / Executable を確認してください。アプリを終了し、プロジェクトフォルダでPowerShellを開いて、利用中の環境に合うコマンドを実行します。")
    variant = st.selectbox("利用しているアプリ環境", ["ソース版 / Python 3.11", "ソース版 / Python 3.12", "ポータブル版 / PROLOAG.exe", "仮想環境版"], key="google_guide_environment")
    commands = {
        "ソース版 / Python 3.11": 'py -3.11 -m pip install -r .\\requirements-google.txt --target .\\.deps-google311 --upgrade',
        "ソース版 / Python 3.12": 'py -3.12 -m pip install -r .\\requirements-google.txt --target .\\.deps-google312 --upgrade',
        "ポータブル版 / PROLOAG.exe": '$env:PYTHONPATH = (Resolve-Path .\\app\\.deps311).Path\n& .\\runtime\\python311\\python.exe -m pip install -r .\\app\\requirements-google.txt --target .\\app\\.deps-google311 --upgrade',
        "仮想環境版": 'python -m pip install -r requirements-google.txt',
    }
    st.code(commands[variant], language="powershell", wrap_lines=True)
    if variant.startswith("ポータブル"):
        st.caption("一度通常起動して基本ライブラリを導入してから終了し、PROLOAG.exeがあるフォルダで実行します。")
    elif variant == "仮想環境版":
        st.caption("アプリで使用する仮想環境を有効にした状態で実行します。別のPython環境には入れないでください。")
    else:
        with st.expander("『pyが見つかりません』と表示されたら"):
            st.write("起動コンソールの Executable に表示された python.exe を使います。次のパス部分を、ご自身の表示に置き換えてください。")
            suffix = "311" if "3.11" in variant else "312"
            st.code(f'& "<Executableに表示されたpython.exeの絶対パス>" -m pip install -r .\\requirements-google.txt --target .\\.deps-google{suffix} --upgrade', language="powershell", wrap_lines=True)
    st.caption("ソース版・ポータブル版ではGoogle専用フォルダへ分離します。既存の .deps311 / .deps312 に混ぜず、Pythonの版も混用しません。導入後にアプリを再起動します。")
    st.markdown("### 2. Google Cloudで認証を用意する")
    st.link_button("Google Cloud Consoleを開く", SOURCE_URLS["Google Cloud Console"])
    _step(1, "プロジェクトとAPIを用意", "自分で管理するGoogleアカウントでプロジェクトを作成または選択します。**Google Sheets API** を有効にし、Docsも使う場合は **Google Docs API** も有効にします。\n\nDocsだけ使う予定でも、PROLOAGの共通接続は空の専用Sheetsを作るため、Sheets APIも必要です。")
    _step(2, "Google Auth platformを設定", "**Branding** でアプリ名・連絡先を設定し、**Audience** を選びます。個人アカウントでは通常 **External** を選択。Testingで使う場合は **Test users / Add users** に、ご自身のGoogleアカウントを登録します。組織専用のInternalは、その組織で使う場合だけです。")
    _step(3, "Data Accessでファイル単位の権限を設定", "必要なスコープは下記です。全Driveや全スプレッドシートにアクセスする広い権限を、追加で指定する必要はありません。")
    st.code("https://www.googleapis.com/auth/drive.file", language=None)
    st.caption("権限の単位はファイルです。特定のタブだけに限定する権限ではありません。")
    _step(4, "Desktop appのJSONを取得", "**Clients → Create client** で **Desktop app（デスクトップアプリ）** を選び、クライアントJSONをダウンロードします。Webアプリ用JSON・APIキー・サービスアカウント鍵は使いません。\n\nJSONは本人だけが扱う場所に保管します。Googleのサンプルにある credentials.json / token.json をPROLOAG内へ置く手順は不要です。")
    st.markdown("### 3. PROLOAGから接続する")
    st.markdown("""
1. **データエクスポート → Google Sheets共有 → Google接続・初回設定** を開きます。
2. **OAuthクライアントJSON（デスクトップアプリ）** に、取得したJSONを選びます。
3. **Googleに接続して共有先を作成** を押します。
4. 開いたブラウザでアカウント・アプリ名・要求権限を確認し、本人がログインと許可を行います。待機は最大120秒です。
5. PROLOAGへ戻り、接続成功と専用表のリンクを確認します。
""")
    st.success("接続できたら初回設定は完了です。この操作だけでは、学習データはまだ送信していません。")
    st.info("ChatGPT側のGoogle接続とは別設定です。PROLOAGが接続できても、GPTが同じ資料を自動で読めるようになるわけではありません。")
    with st.expander("Google公式の設定手順"):
        for name in ("OAuth同意画面の設定", "Sheets・Desktopクライアントの設定", "Google Docs APIの設定", "ファイル単位の権限について"):
            st.markdown(f"- [{name}]({SOURCE_URLS[name]})")
        st.caption("確認日：2026-09-16。Google側の画面名は変更されることがあります。上記公式手順で確認してください。")


def _sheets() -> None:
    st.subheader("Sheets：表で実績を共有する")
    st.image(SHARE_SVG, width="stretch")
    st.markdown("**開く場所：データエクスポート → Google Sheets共有**")
    _step(1, "期間と共有する項目を選ぶ", "**共有する期間** は直近7・30・90日から選び、**期間の最終日** を指定します。学習明細・学習メモを含めるかを選び、必要なら相談メモを入力します。")
    _step(2, "プレビューを作成・更新", "**プレビューを作成・更新** を押します。学習記録数・日別・科目別・教材別などを開き、送る内容を確認してください。元の記録や共有条件を変えたら、このボタンをもう一度押します。")
    _step(3, "確認チェックを入れて送信", "**このプレビューの内容と共有先を確認しました** にチェックし、**Googleへ送信** を押します。送信先はPROLOAGが作成した専用表です。任意の既存スプレッドシートを上書きする機能ではありません。")
    _step(4, "成功日時と表を確認", "成功日時・共有先のリンクを確認します。相談する場合は **GPTへ渡す依頼文** をコピーし、普段の会話へ貼り付けます。資料の受け渡しとアクセス権は、ご自身のGPT側の環境で別途確認します。")
    st.warning("送信は履歴への追記ではなく、専用表の管理タブの置き換えです。期間外の行や、今回外したメモ・明細は管理タブから除かれます。残したい過去の結果は、別ファイルへコピーしてください。")
    st.caption("専用表に自分で追加した別タブは変更しません。ただし、アプリ管理タブへの手作業の追記は次回共有で置き換わります。")


def _docs() -> None:
    st.subheader("Docs：文章で日次・週次の相談をする")
    st.image(SHARE_SVG, width="stretch")
    st.markdown("**開く場所：データエクスポート → Google Docs相談レポート**")
    st.caption("Google Docs APIを追加で有効にしてください。Sheetsと同じ接続・drive.fileの権限を使用します。")
    _step(1, "日次・週次と最終日を選ぶ", "日次は選んだ日1日分、週次は選んだ日までの7日間です。月曜～日曜に固定されません。日次用・週次用に専用文書を1つずつ使用します。")
    _step(2, "必要な情報だけ含める", "**登録した資料リンクを含める**・**『次はここから』メモを含める** を選ぶ場合は、**資料・引き継ぎメモを共有する教材** も指定します。学習メモと今回の相談事項も確認してください。\n\n教材の資料URLや再開メモは、データエクスポート／マスタ管理の **資料リンク・引き継ぎ** で事前に登録できます。リンク先の本文をアプリが自動で読むことはありません。")
    _step(3, "本文と送信先を確認", "**相談レポートのプレビューを作成** で本文を確認します。未接続でも作成でき、**レポートをテキストで保存** も利用できます。\n\nGoogleへ送る場合は **共有先ドキュメントを作成・確認** を押して専用Docを用意します。この操作だけでは学習データを送信しません。")
    _step(4, "承認して送信・相談", "**このレポートの内容と送信先を確認しました** にチェックし、**Google Docsへ送信** を押します。成功後、**このレポートを使ってGPTに相談する** の依頼文をコピーして活用します。")
    st.warning("次回も同じ日次／週次Docの本文を置き換えます。過去レポートや相談の回答を残す場合は、別の文書へコピーしてください。専用Docへの追記・装飾は避けてください。")
    st.caption("本文の手編集や更新競合は検出して止めますが、装飾だけの変更は保護対象外です。文書の日付別アーカイブを自動作成する機能ではありません。")


def _consultation() -> None:
    st.subheader("相談の返答を、次の行動へ戻す")
    st.caption("Google未接続でも使えます。資格試験・語学・仕事の学習など、テーマを限定しない仕組みです。")
    _step(1, "GPTなどからの返答を貼り付ける", "**データエクスポート → 相談提案の取り込み** を開きます。**今日の記録・計画 → 相談提案箱を開く** からも利用できます。\n\n通常は **本文を貼り付け** で始めます。必要なら出典メモを添え、**取り込みプレビューを作成** → **この候補を提案箱へ保存** の順に進みます。ここではまだ日次計画や教材メモへ反映しません。")
    _step(2, "1件ずつ開いて反映先を選ぶ", "**確認する提案** を選び、**この提案を開く** を押します。タイトル・本文を確認し、教材の **次はここから**、または指定日の **日次計画の相談メモ** を反映先に選びます。参考として残す・保留・見送りも可能です。")
    _step(3, "差分を確認して採用する", "**採用前の差分を作成** を押し、変更前・変更後と教材・日付を確認します。**この採用差分と反映先を確認しました** にチェックし、**承認して採用・反映** を押した場合だけ反映します。")
    st.info("提案は学習実績や完了タスクではありません。採用しても、勉強時間・成績・健康記録を自動で変更しません。")
    with st.expander("反映先による違い・採用取消"):
        st.markdown("""
- 教材の **次はここから** は本文を置き換えます。残したい文章は変更後の本文に含めてください。
- **日次計画の相談メモ** は対象日に1件追加します。既存のタスクや計画は置き換えません。
- 採用後は **適用取消のプレビューを作成** → 差分の確認チェック → **適用を取り消す** で戻せます。
- 反映先がその後に編集された場合は、他の変更を失わないよう取消を拒否します。
""")
    with st.expander("複数の提案をまとめて取り込みたいとき"):
        st.write("Sheets / Docsの相談依頼文にあるJSON形式指定をGPTへ渡すと、返答を複数の提案として取り込めます。『JSONを貼り付け』または『JSONファイル』を使い、プレビューで確認してください。本文だけの返答でも通常の貼り付けを利用できます。")


def _troubleshooting() -> None:
    st.subheader("困ったとき")
    issues = [
        ("追加ライブラリが必要／接続ボタンが押せない", "**初回設定** でアプリと同じPython用に導入したか確認してください。導入後は再起動が必要です。接続にはWindows環境とクライアントJSONの選択も必要です。プレビューは未接続で作成できます。"),
        ("Googleの認証で拒否される", "Desktop app用のJSONか、同じCloudプロジェクトで必要なAPIを有効にしたか、External / Testingの場合は使用するアカウントをTest usersに登録したかを確認します。組織管理の制限は管理者へ確認してください。Googleの警告を無理に回避せず、アプリ名・アカウント・権限を確認してください。"),
        ("数日後に再認証が必要になった", "External / Testingでこの権限を使用する場合、リフレッシュトークンは7日で失効します。認証の取り消し等でも失効するため、画面に従って再認証してください。PROLOAGを再起動すれば永久に延長される仕組みではありません。"),
        ("送信途中で切れた／結果が分からない", "画面を再表示し、Sheetsでは **送信済みか確認** → 必要なら **同じ内容を再送**、Docsでは **レポート送信済みか確認** → **同じレポートを再送** を使います。\n\n別の内容へ差し替えたり、保留中の状態ファイルを削除したりしないでください。古い画面の操作が拒否された場合は最新の状態を表示し直し、対象を確認します。"),
        ("条件を変更したのに、送信内容が前のまま", "送るのは最後に作成した固定プレビューです。期間・任意項目・元の記録を変えたら、プレビューを作り直してから再確認・送信してください。"),
        ("Docsの手編集を検出して止まる", "専用Docは出力専用です。残したい手編集は別文書へ退避し、表示されたエラーを確認してください。強制上書きの操作はありません。状態ファイルを削除して保護を回避しないでください。"),
        ("教材メモや相談提案の保存で競合した", "入力は保持されます。**最新の保存内容との差を確認** → 比較の確認チェック → **入力を保持して最新を再読込** で再開できます。再読込だけでは保存・採用しません。提案は新しい差分を作成し、改めて承認します。"),
        ("接続を解除したい", "**Google接続・初回設定** で、このPCの接続情報を削除する確認チェックを入れ、**接続を解除** を押します。未完了の送信がある場合は先に結果確認が必要です。\n\nGoogle上の表・Docは残ります。Google側のアクセス許可の取消やファイル削除は別操作です。"),
        ("GPTが共有先の資料を読めない", "PROLOAGの接続とGPT側のGoogle接続・アクセス権は別です。普段使うGPT側で資料を渡せるか確認します。公開リンクにする必要はありません。Docsの **レポートをテキストで保存** で内容を取り出し、必要な範囲を本人が渡す方法もあります。"),
    ]
    for title, body in issues:
        with st.expander(title):
            st.markdown(body)
    st.caption("問い合わせ時は、止まった操作とエラー文を伝えてください。認証JSON・トークン・connection.dpapi・私的なレポート全文は添付しないでください。")
    st.markdown(f"[Google公式：認証トークンの失効条件]({SOURCE_URLS['認証トークンの失効条件']})")


def render_google_guide() -> None:
    st.caption("読むだけで使えるガイドです。Googleへの接続・送信や、保存データの変更は行いません。")
    from web_demo.navigation import product_page_link
    product_page_link("pages/97_データエクスポート.py", label="実際の操作画面：データエクスポートを開く")
    topic = st.radio("知りたいこと", TOPICS, horizontal=True, key="google_guide_topic")
    st.divider()
    renderers = dict(zip(TOPICS, (_overview, _setup, _sheets, _docs, _consultation, _troubleshooting)))
    renderers[topic]()
