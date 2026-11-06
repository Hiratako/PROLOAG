"""Local OCR sources and durable jobs, independent of study/health records."""

from __future__ import annotations

import atexit
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from app_core.transcription import (
    atomic_json,
    read_json as _shared_read_json,
    file_lock,
)
from app_core.media_conversion import _identity as _media_identity, _process_options

ACTIVE = {"starting", "loading", "reading", "cancelling"}
UPLOAD_LIMIT = 200 * 1024**2
LOCAL_LIMIT = 1024**3
MAX_PAGES = 100
_lock = threading.RLock()


class OCRError(ValueError):
    pass


def read_json(path, default=None):
    # A Windows atomic replace can briefly deny a simultaneous reader.
    for attempt in range(20):
        try:
            return _shared_read_json(path, default)
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.025)


def _identity(path):
    try:
        return _media_identity(path)
    except ValueError as exc:
        raise OCRError(
            "元ファイルが見つからないか読み取りできません。場所を確認してください。"
        ) from exc


def dependency_dir(root):
    return Path(root) / f".deps-ocr{sys.version_info.major}{sys.version_info.minor}"


def engine_available(root):
    folder = dependency_dir(root)
    return all(
        (folder / package / "__init__.py").is_file()
        or importlib.util.find_spec(package) is not None
        for package in ("rapidocr", "pypdfium2", "PIL")
    )


def worker_env(root):
    env = os.environ.copy()
    paths = [str(dependency_dir(root)), str(root)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env.update(
        PYTHONPATH=os.pathsep.join(paths),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        PYTHONIOENCODING="utf-8",
        HF_HUB_DISABLE_TELEMETRY="1",
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
    )
    return env


def command(root, action, request):
    return [
        sys.executable,
        "-B",
        str(Path(root) / "app_core/ocr_worker.py"),
        action,
        str(request),
    ]


def parse_pages(value, count):
    if not isinstance(value, str) or len(value) > 4000:
        raise OCRError("ページを「1-3,5」の形式で指定してください。")
    value = (
        value.strip()
        .replace("，", ",")
        .replace("、", ",")
        .replace("〜", "-")
        .replace("～", "-")
    )
    if not value:
        value = f"1-{count}"
    result = set()
    for token in value.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", token)
        if not match:
            raise OCRError("ページを「1-3,5」の形式で指定してください。")
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= count or end - start >= MAX_PAGES:
            raise OCRError(
                f"1〜{count}ページの範囲で、1回{MAX_PAGES}ページまで指定してください。"
            )
        result.update(range(start, end + 1))
        if len(result) > MAX_PAGES:
            raise OCRError(f"1回に読み取れるページは{MAX_PAGES}ページまでです。")
    return sorted(result)


def transform(value=None):
    value = value or {}
    rotation = value.get("rotation", 0)
    crop = value.get("crop", [0, 0, 1, 1])
    if type(rotation) is not int or rotation not in (0, 90, 180, 270):
        raise OCRError("回転角度を確認してください。")
    if not isinstance(crop, (list, tuple)) or len(crop) != 4:
        raise OCRError("読み取り範囲を確認してください。")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in crop):
        raise OCRError("読み取り範囲を確認してください。")
    x1, y1, x2, y2 = crop
    if (
        not 0 <= x1 < x2 <= 1
        or not 0 <= y1 < y2 <= 1
        or x2 - x1 < 0.01
        or y2 - y1 < 0.01
    ):
        raise OCRError("読み取り範囲を画像内で指定してください（縦横1%以上）。")
    return {"rotation": rotation, "crop": [round(float(v), 6) for v in crop]}


@dataclass(frozen=True)
class OCRSource:
    id: str
    path: Path
    name: str
    mode: str
    identity: tuple
    pages: int
    kind: str
    folder: Path


class OCRSources:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._base = None
        self.sources = {}
        self._lock = threading.RLock()
        self._preparing = set()
        atexit.register(self.close)

    def _folder(self):
        if self._base is None or not self._base.exists():
            self._base = Path(tempfile.mkdtemp(prefix="proloag-ocr-"))
        path = self._base / uuid4().hex
        path.mkdir()
        return path

    def _tool(self, action, request, folder):
        if not engine_available(self.root):
            raise OCRError(
                "OCRエンジンが未導入です。セットアップ案内を確認してください。"
            )
        request_path = folder / (uuid4().hex + ".json")
        output_path = request_path.with_suffix(".result.json")
        atomic_json(request_path, {**request, "output": str(output_path)})
        try:
            result = subprocess.run(
                command(self.root, action, request_path),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=45,
                env=worker_env(self.root),
                cwd=self.root,
                **_process_options(),
            )
            data = read_json(output_path)
            if result.returncode or not isinstance(data, dict) or data.get("error"):
                raise OCRError(
                    (data or {}).get("error")
                    or "ファイルを処理できませんでした。正常な画像・PDFを指定してください。"
                )
            return data
        except subprocess.TimeoutExpired as exc:
            raise OCRError(
                "確認が時間内に終わりませんでした。ファイルの大きさ・状態を確認してください。"
            ) from exc
        finally:
            request_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    def snapshot(self, owner):
        with self._lock:
            return self.sources.get(owner)

    def prepare(self, owner, *, path=None, name=None, buffer=None):
        if not re.fullmatch("[a-f0-9]{32}", owner):
            raise OCRError("入力セッションを確認してください。")
        if buffer is None:
            path = Path(str(path).strip().strip('"')).expanduser()
            if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
                raise OCRError("PC内のファイルのフルパスを指定してください。")
            try:
                path = path.resolve(strict=True)
            except (OSError, ValueError, RuntimeError) as exc:
                raise OCRError(
                    "ファイルが見つかりません。場所を確認してください。"
                ) from exc
            name = path.name
        else:
            if (
                not isinstance(buffer, (bytes, bytearray, memoryview))
                or not 0 < len(buffer) <= UPLOAD_LIMIT
            ):
                raise OCRError(
                    "アップロードは空でない200MB以内の画像・PDFに対応します。"
                )
            name = str(name).replace("\\", "/").split("/")[-1]
        suffix = Path(name).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".pdf"}:
            raise OCRError("JPEG・PNG・PDFを選択してください。")
        with self._lock:
            if owner in self._preparing:
                raise OCRError("この入力を確認中です。しばらくお待ちください。")
            folder = self._folder()
            self._preparing.add(owner)
        try:
            if buffer is not None:
                path = folder / ("input" + suffix)
                with path.open("xb") as output:
                    output.write(buffer)
            identity = _identity(path)
            if not 0 < identity[0] <= LOCAL_LIMIT:
                raise OCRError("空でない1GB以内のファイルを指定してください。")
            meta = self._tool("inspect", {"path": str(path), "suffix": suffix}, folder)
            if identity != _identity(path):
                raise OCRError(
                    "確認中に元ファイルが変更されました。もう一度確認してください。"
                )
            source = OCRSource(
                uuid4().hex,
                path,
                name,
                "upload" if buffer is not None else "local",
                identity,
                meta["pages"],
                meta["kind"],
                folder,
            )
            with self._lock:
                old = self.sources.get(owner)
                self.sources[owner] = source
            if old:
                shutil.rmtree(old.folder, ignore_errors=True)
            return source
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            with self._lock:
                self._preparing.discard(owner)

    def preview(self, source, page, rotation):
        if source.identity != _identity(source.path):
            raise OCRError("元ファイルが変更されました。もう一度確認してください。")
        if type(page) is not int or not 1 <= page <= source.pages:
            raise OCRError("ページ番号を確認してください。")
        transform({"rotation": rotation})
        path = source.folder / f"preview-{page}-{rotation}.png"
        if not path.exists():
            self._tool(
                "preview",
                {
                    "path": str(source.path),
                    "suffix": Path(source.name).suffix.lower(),
                    "page": page,
                    "rotation": rotation,
                    "image": str(path),
                },
                source.folder,
            )
        return path.read_bytes()

    def clear(self, owner):
        with self._lock:
            if owner in self._preparing:
                raise OCRError("確認中です。完了してからクリアしてください。")
            source = self.sources.pop(owner, None)
            if source:
                shutil.rmtree(source.folder, ignore_errors=True)

    def close(self):
        with self._lock:
            if self._base and self._base.exists():
                shutil.rmtree(self._base, ignore_errors=True)
            self.sources.clear()


class OCRStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / "ocr_data"

    def folder(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch("[a-f0-9]{32}", job_id):
            raise OCRError("読み取りIDが不正です。")
        path = self.base / "jobs" / job_id
        if (
            self.base.is_symlink()
            or (self.base / "jobs").is_symlink()
            or path.is_symlink()
            or not self.base.resolve().is_relative_to(self.root)
            or not path.resolve().is_relative_to(self.base.resolve())
        ):
            raise OCRError("読み取りの保存先を確認してください。")
        return path

    def state(self, job_id):
        folder = self.folder(job_id)
        value = read_json(folder / "state.json")
        if not isinstance(value, dict) or value.get("id") != job_id:
            raise OCRError("読み取り履歴が見つかりません。履歴を更新してください。")
        if value["status"] in ACTIVE:
            try:
                with file_lock(folder / ".worker.lock", blocking=False):
                    stale = time.time() - value["created"] > 45
            except OSError:
                stale = False
            if stale:
                value = dict(
                    value,
                    status="interrupted",
                    error="処理プロセスが終了しました。完了したページは残っています。元ファイルを指定して再実行できます。",
                )
        return value

    def history(self):
        parent = self.base / "jobs"
        if not parent.exists():
            return []
        values = []
        for path in parent.iterdir():
            if path.is_dir() and re.fullmatch("[a-f0-9]{32}", path.name):
                try:
                    values.append(self.state(path.name))
                except PermissionError:
                    raise
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(values, key=lambda v: v["created"], reverse=True)

    def start(self, source, pages, transforms=None, strategy="auto", operation_id=None):
        if (
            not isinstance(pages, list)
            or not pages
            or len(pages) > MAX_PAGES
            or any(type(p) is not int or not 1 <= p <= source.pages for p in pages)
            or len(set(pages)) != len(pages)
            or strategy not in {"auto", "ocr"}
        ):
            raise OCRError("読み取りページ・方法を確認してください。")
        pages = sorted(pages)
        settings = {str(p): transform((transforms or {}).get(str(p))) for p in pages}
        if source.identity != _identity(source.path):
            raise OCRError("元ファイルが変更されました。もう一度確認してください。")
        if not engine_available(self.root):
            raise OCRError(
                "OCRエンジンが未導入です。セットアップ案内を確認してください。"
            )
        operation_id = operation_id or uuid4().hex
        if not re.fullmatch("[a-f0-9]{32}", operation_id):
            raise OCRError("処理IDが不正です。")
        fingerprint = [source.id, pages, settings, strategy]
        try:
            with _lock, file_lock(self.base / ".catalog.lock"):
                history = self.history()
                previous = next(
                    (row for row in history if row.get("operation_id") == operation_id),
                    None,
                )
                if previous:
                    if previous.get("fingerprint") != fingerprint:
                        raise OCRError("処理IDと読み取り条件が一致しません。")
                    return previous["id"]
                if any(row["status"] in ACTIVE for row in history):
                    raise OCRError(
                        "別の読み取りを処理中です。履歴から進捗を確認してください。"
                    )
                if (
                    shutil.disk_usage(self.base).free
                    < source.identity[0] + len(pages) * 12 * 1024**2 + 128 * 1024**2
                ):
                    raise OCRError("作業用ディスクの空き容量が不足しています。")
                job_id = uuid4().hex
                folder = self.folder(job_id)
                folder.mkdir(parents=True)
                try:
                    path = folder / ("source" + Path(source.name).suffix.lower())
                    digest = hashlib.sha256()
                    with (
                        source.path.open("rb") as incoming,
                        path.open("xb") as outgoing,
                    ):
                        while chunk := incoming.read(1024**2):
                            digest.update(chunk)
                            outgoing.write(chunk)
                    if source.identity != _identity(source.path):
                        raise OCRError(
                            "保存中に元ファイルが変更されました。もう一度確認してください。"
                        )
                    request = {
                        "schema_version": 1,
                        "id": job_id,
                        "root": str(self.root),
                        "path": str(path),
                        "source_name": source.name,
                        "source_sha256": digest.hexdigest(),
                        "pages": pages,
                        "transforms": settings,
                        "strategy": strategy,
                        "languages": ["ja", "en"],
                    }
                    state = {
                        "schema_version": 1,
                        "id": job_id,
                        "name": source.name,
                        "created": time.time(),
                        "status": "starting",
                        "progress": 0.0,
                        "elapsed": 0.0,
                        "completed_pages": [],
                        "message": "起動しています",
                        "error": "",
                        "operation_id": operation_id,
                        "fingerprint": fingerprint,
                    }
                    atomic_json(folder / "request.json", request)
                    atomic_json(folder / "state.json", state)
                    with (folder / "worker.log").open("wb") as log:
                        process = subprocess.Popen(
                            command(self.root, "run", folder / "request.json"),
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            env=worker_env(self.root),
                            cwd=self.root,
                            **_process_options(),
                        )
                    threading.Thread(target=process.wait, daemon=True).start()
                except Exception:
                    shutil.rmtree(folder)
                    raise
                return job_id
        except OSError as exc:
            raise OCRError(
                "読み取りを開始できません。保存先の空き容量・権限を確認してください。"
            ) from exc

    def cancel(self, job_id):
        if self.state(job_id)["status"] in ACTIVE:
            (self.folder(job_id) / "cancel").touch()

    def result(self, job_id):
        folder = self.folder(job_id)
        state = self.state(job_id)
        request = read_json(folder / "request.json")
        edits = read_json(folder / "edits.json", {"revision": 0, "texts": {}})
        pages = []
        # Discover committed page files, including one saved just before interruption.
        for number in request["pages"]:
            page = read_json(folder / f"page-{number}.json")
            if page:
                for block in page["blocks"]:
                    block["text"] = edits["texts"].get(
                        block["id"], block["original_text"]
                    )
                pages.append(page)
        return {
            "schema_version": 1,
            "id": job_id,
            "source_name": request["source_name"],
            "source_sha256": request["source_sha256"],
            "state": state,
            "requested_pages": request["pages"],
            "strategy": request["strategy"],
            "languages": request["languages"],
            "transforms": request["transforms"],
            "revision": edits["revision"],
            "pages": pages,
        }

    def image(self, job_id, page):
        if (
            type(page) is not int
            or page not in read_json(self.folder(job_id) / "request.json")["pages"]
        ):
            raise OCRError("ページ番号を確認してください。")
        path = self.folder(job_id) / f"page-{page}.png"
        if not path.is_file() or path.stat().st_size > 16 * 1024**2:
            raise OCRError("原本のプレビューを取得できませんでした。")
        return path.read_bytes()

    def save_edits(self, job_id, revision, texts):
        if self.state(job_id)["status"] in ACTIVE:
            raise OCRError("処理が終わってから文章を修正できます。")
        folder = self.folder(job_id)
        try:
            with _lock, file_lock(folder / ".edit.lock"):
                result = self.result(job_id)
                if result["revision"] != revision:
                    raise OCRError(
                        "別の画面で修正されました。下書きを控えてから履歴を開き直してください。"
                    )
                known = {b["id"] for p in result["pages"] for b in p["blocks"]}
                edits = read_json(folder / "edits.json", {"revision": 0, "texts": {}})
                for key, value in texts.items():
                    if (
                        key not in known
                        or not isinstance(value, str)
                        or len(value) > 20000
                        or "\x00" in value
                    ):
                        raise OCRError(
                            "修正内容を確認してください（1区間20000文字以内）。"
                        )
                    edits["texts"][key] = value
                edits["revision"] += 1
                atomic_json(folder / "edits.json", edits)
        except OSError as exc:
            raise OCRError(
                "修正を保存できませんでした。入力を保持しています。"
            ) from exc

    def delete(self, job_id):
        try:
            with _lock, file_lock(self.base / ".catalog.lock"):
                if self.state(job_id)["status"] in ACTIVE:
                    raise OCRError(
                        "処理中の履歴は削除できません。先に中止してください。"
                    )
                shutil.rmtree(self.folder(job_id))
        except OSError as exc:
            raise OCRError(
                "履歴を削除できませんでした。もう一度お試しください。"
            ) from exc


def export_result(result, kind):
    if kind == "json":
        return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode(
            "utf-8"
        )
    if kind not in {"txt", "md"}:
        raise OCRError("未対応の出力形式です。")
    texts = []
    for page in result["pages"]:
        prefix = (
            f"## {page['number']}ページ\n\n"
            if kind == "md"
            else f"[{page['number']}ページ]\n"
        )
        texts.append(prefix + "\n".join(b["text"] for b in page["blocks"]))
    return ("\n\n".join(texts) + "\n").encode("utf-8-sig" if kind == "txt" else "utf-8")
