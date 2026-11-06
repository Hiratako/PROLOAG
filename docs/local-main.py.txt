from __future__ import annotations

from datetime import date
from app_core.app_clock import app_today, demo_mode_label, is_demo_mode
from pathlib import Path
from typing import Any

import streamlit as st

import prolog_ui.components as ab
from app_core.branding import BRAND_EXPANSION, BRAND_NAME, BRAND_TAGLINE_JA
from app_core.diary_service import investment_total, load_diary_entry
from config import DIARY_ENTRIES_DIR, STUDY_LOGS_DIR
from data_utils import load_study_sessions_file


from app_core.settings_service import load_user_settings as _load_page_settings
PAGE_SETTINGS = _load_page_settings()

ROOT = Path(__file__).resolve().parent


def ensure_dirs() -> None:
    STUDY_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DIARY_ENTRIES_DIR.mkdir(parents=True, exist_ok=True)


def today_json_path(base_dir: Path, target: date) -> Path:
    return base_dir / f"{target:%Y}" / f"{target:%m}" / f"{target:%Y%m%d}.json"


def sec_to_hms(seconds: int | float) -> str:
    total = max(int(seconds or 0), 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def yen(value: int | float) -> str:
    return f"{int(value or 0):,}円"


def summarize_today(target: date) -> dict[str, Any]:
    study_data = load_study_sessions_file(today_json_path(STUDY_LOGS_DIR, target))
    diary_data = load_diary_entry(target)

    duration = sum(float(item.get("duration_sec", 0) or 0) for item in study_data if isinstance(item, dict))
    money = sum(float(item.get("money", 0) or 0) for item in study_data if isinstance(item, dict))
    mood = diary_data.get("mood") or "未記録"
    weather = diary_data.get("weather") or "未記録"
    return {
        "duration": duration,
        "money": money,
        "mood_weather": f"{mood} / {weather}",
        "investment": investment_total(diary_data),
    }


def render_home_feature_card(page: dict[str, str]) -> None:
    with st.container(border=True):
        path = page.get("path", "")
        if path and (ROOT / path).exists():
            st.page_link(path, label=page["title"])
        else:
            st.markdown(f"**{page['title']}**")
            st.caption("準備中")
        st.caption(page["description"])


def render_feature_group(title: str, items: list[dict[str, str]]) -> None:
    st.markdown(f"#### {title}")
    for index in range(0, len(items), 2):
        left, right = st.columns(2)
        with left:
            render_home_feature_card(items[index])
        if index + 1 < len(items):
            with right:
                render_home_feature_card(items[index + 1])


FEATURE_GROUPS = [
    {
        "title": "Study Management",
        "items": [
            {
                "path": "pages/09_今日の学習プラン.py",
                "title": "今日の記録・計画",
                "description": "今日の学習計画、主要な生活記録、タスク、復習対象をまとめて確認・入力します。",
            },
            {
                "path": "pages/19_学習ロードマップ.py",
                "title": "学習ロードマップ",
                "description": "学習工程をガントで見渡し、日程・進捗・日別タスク・実績をつなぎます。",
            },
            {
                "path": "pages/01_時は金なり｜秒給カウンター.py",
                "title": "秒給カウンター",
                "description": "勉強セッション中の時間、換算金額、分類、コンディションを記録します。",
            },
            {
                "path": "pages/02_勉強ログ.py",
                "title": "勉強ログ",
                "description": "過去の学習セッション、日別集計、関連ノート、分類フィルターを確認します。",
            },
            {
                "path": "pages/03_可視化_ダッシュボード.py",
                "title": "可視化ダッシュボード",
                "description": "学習時間、金額、曜日・時間帯、勉強内容の傾向をグラフで確認します。",
            },
            {
                "path": "pages/17_学習パフォーマンス・条件分析.py",
                "title": "学習パフォーマンス・条件分析",
                "description": "成果が高く翌日も崩れにくかった条件を、睡眠・計画・カフェイン・運動から横断的に検証します。",
            },
            {
                "path": "pages/05_高度AI分析.py",
                "title": "マイデータ統計演習",
                "description": "自分の学習・生活ログを固定した問題データに変換し、統計学の計算と結果の読み方を練習します。",
            },
            {
                "path": "pages/11_学習蓄積ビジュアライザー.py",
                "title": "学習蓄積ビジュアライザー",
                "description": "日々の学習時間、ノート、カード、復習予定の蓄積を視覚的に確認します。",
            },
            {
                "path": "pages/10_マスタ管理.py",
                "title": "マスタ管理",
                "description": "プロジェクト、科目、教材、単元、タグを管理し、分類精度を高めます。",
            },
        ],
    },
    {
        "title": "Knowledge & Review",
        "items": [
            {
                "path": "pages/04_ノート・知識ベース.py",
                "title": "ノート・知識ベース",
                "description": "学習メモ、関連セッション、カード、復習予定を知識ベースとして整理します。",
            },
            {
                "path": "pages/15_知識都市ビュー.py",
                "title": "知識都市ビュー",
                "description": "科目を街区、単元を建物として、学習時間・ノート・カード・復習予定の蓄積をカラフルに俯瞰します。",
            },
            {
                "path": "pages/07_記憶フラッシュ.py",
                "title": "記憶フラッシュ",
                "description": "重要事項をカード化し、要復習項目を中心に確認します。",
            },
            {
                "path": "pages/08_忘却曲線リマインダー.py",
                "title": "復習リマインダー",
                "description": "ノートやキーワードを復習予定として管理します。",
            },
        ],
    },
    {
        "title": "Life & Healthcare",
        "items": [
            {
                "path": "pages/06_日記.py",
                "title": "日記・ライフログ",
                "description": "日々の出来事、気分、天気、成分摂取、自己投資費用を記録します。",
            },
            {
                "path": "pages/12_睡眠・体調ログ.py",
                "title": "睡眠・体調ログ",
                "description": "睡眠、疲労、ストレス、集中しやすさを記録し、学習との関係を振り返ります。",
            },
            {
                "path": "pages/13_運動ログ.py",
                "title": "運動ログ",
                "description": "ランニングや運動の記録を残し、体調や学習状況との関係を振り返ります。",
            },
            {
                "path": "pages/14_生活・健康ダッシュボード.py",
                "title": "生活・健康ダッシュボード",
                "description": "睡眠、体調、運動、勉強ログ、学習プランを日付軸で統合して確認します。",
            },
            {
                "path": "pages/16_カフェイン・運動分析.py",
                "title": "成分リズム・コンディション分析",
                "description": "成分別の推定残存量を比較し、カフェインと勉強・睡眠・集中・運動の傾向を確認します。",
            },
        ],
    },
    {
        "title": "Hobby & Tools",
        "items": [
            {
                "path": "pages/20_メディア変換.py",
                "title": "メディア変換",
                "description": "MP4の音声をMP3へ変換し、試聴・保存します。大容量動画の直接指定にも対応します。",
            },
            {"path": "pages/21_音声・動画の文字起こし.py", "title": "音声・動画の文字起こし", "description": "日本語・英語のMP4／MP3を文字起こしし、聞き直し・修正・字幕出力します。"},
            {"path": "pages/22_画像・PDFの文字読み取り.py", "title": "画像・PDFの文字読み取り", "description": "日英の教材画像やPDFを読み取り、原本を見ながら修正・保存・出力します。"},
        ],
    },
    {
        "title": "Administration",
        "items": [
            {
                "path": "pages/95_使い方ガイド.py",
                "title": "使い方ガイド",
                "description": "初期設定から毎日の記録、修正、振り返りまで、PROLOAGの基本的な使い方を確認します。",
            },
            {
                "path": "pages/96_Google連携ガイド.py",
                "title": "Google連携ガイド",
                "description": "Google連携の初期設定から、学習実績の共有・相談レポート・提案の取り込みまでの使い方を確認します。",
            },
            {
                "path": "pages/97_データエクスポート.py",
                "title": "データエクスポート",
                "description": "全記録を属性を保った種類別CSVとして、個別または一括ZIPで取り出します。",
            },
            {
                "path": "pages/94_旧版ログ移行.py",
                "title": "旧版ログ移行",
                "description": "旧式カウンターのCSVを診断し、重複・分類を確認してから勉強ログへ取り込みます。",
            },
            {
                "path": "pages/98_ユーザー設定.py",
                "title": "ユーザー設定",
                "description": "タイマー、日記、睡眠・体調、運動ログの初期値と、成分ごとの残存推定・表示設定を管理します。",
            },
            {
                "path": "pages/99_データ整合性チェック.py",
                "title": "データ整合性チェック",
                "description": "JSONデータのID重複、欠損、参照切れ、不正値を確認します。",
            },
        ],
    },
]


st.set_page_config(page_title=f"{BRAND_NAME} | ポータル", layout="wide")
ensure_dirs()
ab.inject_css(settings=PAGE_SETTINGS)
ab.sidemenu("メニューから各機能へ移動してください。", settings=PAGE_SETTINGS)

ab.topbar(BRAND_NAME, BRAND_TAGLINE_JA, eyebrow=BRAND_EXPANSION)

today = app_today()
if is_demo_mode():
    st.info(f"{demo_mode_label()} / 表示基準日: {today:%Y年%m月%d日}")

summary = summarize_today(today)

st.subheader("本日のクイックサマリー")
cols = st.columns(4)
with cols[0]:
    ab.metric_block("今日の勉強時間", sec_to_hms(summary["duration"]), "本日記録済みの累計")
with cols[1]:
    ab.metric_block("今日の換算金額", yen(summary["money"]), "勉強時間の価値換算")
with cols[2]:
    ab.metric_block("今日の気分・天気", summary["mood_weather"], "日記から自動反映")
with cols[3]:
    ab.metric_block("今日の自己投資額", yen(summary["investment"]), "本日の費用ログ合計")

st.markdown("### 今日の入口")
ab.card(
    "まずは今日の記録・計画から",
    "学習計画、気分、カフェイン、自己投資、睡眠・体調、運動の簡易記録をまとめて扱えます。勉強セッション中の詳細は秒給カウンターで記録します。",
)

st.markdown("### 機能ガイド")
for group in FEATURE_GROUPS:
    render_feature_group(group["title"], group["items"])
