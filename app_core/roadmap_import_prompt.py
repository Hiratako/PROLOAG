"""Public, static instructions and a synthetic example for AI roadmap import."""

from functools import lru_cache
import json
from pathlib import Path


PROMPT_PATH = Path(__file__).resolve().parents[1] / "docs" / "ai-roadmap-import-prompt.md"


@lru_cache(maxsize=1)
def get_import_prompt() -> str:
    """Return instructions only; never read or export the user's records."""
    return PROMPT_PATH.read_text(encoding="utf-8-sig")


def get_example_json() -> bytes:
    """A complete importable example, explicitly unrelated to actual plans."""
    example = {
        "format": "proloag.study-roadmap",
        "schema_version": 1,
        "plan_title": "架空サンプル・40問の演習と見直し（実際の計画ではありません）",
        "items": [
            {
                "ref": "E01",
                "title": "架空問題集｜問題1〜40・初回演習",
                "start_date": "2026-11-09",
                "end_date": "2026-11-13",
                "deadline": None,
                "planned_minutes": 300,
                "status": "not_started",
                "progress_kind": "quantity",
                "completed": 0,
                "total": 40,
                "unit": "問",
                "classification": {
                    "project": None,
                    "subject": None,
                    "material": None,
                    "unit": None,
                },
                "predecessor_refs": [],
                "memo": "架空例。解答・解説確認・誤答理由の記録までで1問完了。平均7.5分×40問＝300分という仮見積もり。日付・範囲は実際の条件に合わせて作成してください。",
            },
            {
                "ref": "E02",
                "title": "架空問題集｜問題1〜40・見直し",
                "start_date": "2026-11-14",
                "end_date": "2026-11-15",
                "deadline": "2026-11-15",
                "planned_minutes": 120,
                "status": "not_started",
                "progress_kind": "quantity",
                "completed": 0,
                "total": 40,
                "unit": "問",
                "classification": {
                    "project": None,
                    "subject": None,
                    "material": None,
                    "unit": None,
                },
                "predecessor_refs": ["E01"],
                "memo": "架空例。全40問の解答根拠を確認し、誤答は再解答して1問完了。平均3分×40問＝120分という仮見積もり。期限も例示です。",
            },
        ],
        "milestones": [
            {
                "ref": "M01",
                "title": "架空の提出期限（サンプル）",
                "date": "2026-11-15",
                "project": None,
                "memo": "取込機能を試すための架空例です。ユーザーの実際の提出期限ではありません。",
            }
        ],
    }
    return (json.dumps(example, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
