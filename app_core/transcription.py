"""Durable local transcription jobs; independent of the personal JSON journal."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from uuid import uuid4

from app_core.media_conversion import (
    MediaError,
    MediaManager,
    Source,
    Track,
    UPLOAD_LIMIT,
    _finite_duration,
    _identity,
    _probe,
    _process_options,
    discover_tools,
)

ACTIVE = {"starting", "preparing", "loading", "transcribing", "cancelling"}
MODELS = {"standard": "small", "accurate": "medium"}
_mutex = threading.RLock()


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        # Windows readers can briefly prevent replacement of an open file.
        for attempt in range(20):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.025)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


@contextmanager
def file_lock(path, blocking=True):
    """Separate byte locks, never app_core.json_store / global record locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class TranscriptionSources(MediaManager):
    """Reuse owned uploads and track descriptors, with MP3 validation added."""

    def _source(self, path, name, mode):
        before = _identity(path)
        try:
            data = _probe(self._tools(), path)
        except MediaError as exc:
            raise MediaError(
                "音声情報を取得できません。正常なMP4／MP3を選択してください。"
            ) from exc
        formats = set(str(data.get("format", {}).get("format_name", "")).split(","))
        expected = {"mp3"} if Path(name).suffix.lower() == ".mp3" else {"mov", "mp4"}
        if not formats.intersection(expected):
            raise MediaError("ファイルの内容とMP4／MP3の拡張子が一致しません。")
        tracks = []
        for row in data.get("streams", []):
            if row.get("codec_type") != "audio":
                continue
            index = row.get("index")
            if type(index) is not int or index < 0:
                raise MediaError("音声トラック情報が不正です。")
            tags = row.get("tags") or {}
            tracks.append(
                Track(
                    index,
                    str(row.get("codec_name", ""))[:40],
                    str(tags.get("language", ""))[:80],
                    str(tags.get("title", ""))[:200],
                    int(row.get("channels", 0)),
                    _finite_duration(row.get("duration")),
                )
            )
        duration = _finite_duration(data.get("format", {}).get("duration"))
        if (
            not tracks
            or len(tracks) > 256
            or len({t.index for t in tracks}) != len(tracks)
        ):
            raise MediaError("利用できる音声トラックがありません。")
        if not duration or duration > 24 * 3600:
            raise MediaError("長さを確認できる、24時間以内の音声を選択してください。")
        if before != _identity(path):
            raise MediaError(
                "確認中に元ファイルが変更されました。もう一度確認してください。"
            )
        return Source(uuid4().hex, path, name, mode, before, duration, tuple(tracks))

    def prepare_local(self, owner, value):
        path = Path(str(value).strip().strip('"')).expanduser()
        if (
            not path.is_absolute()
            or path.suffix.lower() not in {".mp3", ".mp4"}
            or str(path).startswith(("\\\\", "//"))
        ):
            raise MediaError("PC内のMP4／MP3ファイルのフルパスを指定してください。")
        try:
            path = path.resolve(strict=True)
        except (OSError, ValueError, RuntimeError) as exc:
            raise MediaError(
                "元ファイルが見つかりません。場所を確認してください。"
            ) from exc
        return self._prepare(owner, path=path, name=path.name)

    def prepare_upload(self, owner, name, buffer):
        if Path(str(name)).suffix.lower() not in {".mp3", ".mp4"}:
            raise MediaError("MP4／MP3を選択してください。")
        if (
            not isinstance(buffer, (bytes, bytearray, memoryview))
            or not 0 < len(buffer) <= UPLOAD_LIMIT
        ):
            raise MediaError("アップロードは空でない200MB以内のファイルに対応します。")
        return self._prepare(
            owner, buffer=buffer, name=name.replace("\\", "/").split("/")[-1]
        )


def dependency_dir(root):
    return Path(root) / f".deps-asr{sys.version_info.major}{sys.version_info.minor}"


def engine_available(root):
    return (
        dependency_dir(root) / "faster_whisper/__init__.py"
    ).is_file() or importlib.util.find_spec("faster_whisper") is not None


def timestamp(seconds, separator="."):
    value = max(0, round(float(seconds) * 1000))
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    seconds, millis = divmod(value, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{millis:03}"


def export_result(result, kind):
    rows = result["segments"]
    if kind == "json":
        return json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode(
            "utf-8"
        )
    if kind == "txt":
        return ("\n".join(row["text"] for row in rows) + "\n").encode("utf-8-sig")
    if kind not in {"srt", "vtt"}:
        raise MediaError("未対応の出力形式です。")
    cues = []
    for number, row in enumerate(rows, 1):
        # Cue-like input must not turn into subtitle structure.
        text = re.sub(r"\n\s*\n", "\n", row["text"].replace("\r", "")).replace(
            "-->", "→"
        )
        sep = "," if kind == "srt" else "."
        cues.append(
            f"{number}\n{timestamp(row['start'], sep)} --> {timestamp(row['end'], sep)}\n{text}\n"
        )
    return (("WEBVTT\n\n" if kind == "vtt" else "") + "\n".join(cues)).encode("utf-8")


class TranscriptionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / "transcription_data"

    def folder(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch("[a-f0-9]{32}", job_id):
            raise MediaError("文字起こしIDが不正です。")
        path = self.base / "jobs" / job_id
        if (
            self.base.is_symlink()
            or (self.base / "jobs").is_symlink()
            or path.is_symlink()
            or not self.base.resolve().is_relative_to(self.root)
            or not path.resolve().is_relative_to(self.base.resolve())
        ):
            raise MediaError("文字起こしの保存先を確認してください。")
        return path

    def _state(self, job_id):
        folder = self.folder(job_id)
        state = read_json(folder / "state.json")
        if not isinstance(state, dict):
            raise MediaError("文字起こしが見つかりません。履歴を更新してください。")
        if state.get("status") in ACTIVE:
            try:
                with file_lock(folder / ".worker.lock", blocking=False):
                    stale = time.time() - state.get("created", 0) > 45
            except OSError:
                stale = False
            if stale:
                # Do not mutate an orphan from a read. It remains recoverable.
                state = dict(
                    state,
                    status="interrupted",
                    error="処理プロセスが終了しました。途中結果を保持しています。元ファイルを指定して再実行できます。",
                )
        return state

    def history(self):
        parent = self.base / "jobs"
        if not parent.exists():
            return []
        values = []
        for path in parent.iterdir():
            if path.is_dir() and re.fullmatch("[a-f0-9]{32}", path.name):
                try:
                    values.append(self._state(path.name))
                except (OSError, ValueError):
                    continue
        return sorted(values, key=lambda row: row.get("created", 0), reverse=True)

    def start(
        self,
        source,
        track_index,
        language,
        mode,
        hotwords="",
        device="cpu",
        operation_id=None,
    ):
        if (
            language not in {"ja", "en", "auto"}
            or mode not in MODELS
            or device not in {"cpu", "auto"}
        ):
            raise MediaError("文字起こし設定を確認してください。")
        hotwords = str(hotwords).strip()
        if len(hotwords) > 1000:
            raise MediaError("用語ヒントは1000文字以内にしてください。")
        track = next((t for t in source.tracks if t.index == track_index), None)
        if not track or _identity(source.path) != source.identity:
            raise MediaError(
                "元ファイルが変更されたか音声を選択できません。もう一度確認してください。"
            )
        if not engine_available(self.root):
            raise MediaError(
                "文字起こしエンジンが未導入です。下のセットアップ案内を確認してください。"
            )
        tools = discover_tools(self.root)
        operation_id = operation_id or uuid4().hex
        if not re.fullmatch("[a-f0-9]{32}", operation_id):
            raise MediaError("処理IDが不正です。")
        try:
            with _mutex, file_lock(self.base / ".catalog.lock"):
                rows = self.history()
                old = next(
                    (r for r in rows if r.get("operation_id") == operation_id), None
                )
                fingerprint = [source.id, track_index, language, mode, hotwords, device]
                if old:
                    if old.get("fingerprint") != fingerprint:
                        raise MediaError("処理IDと設定が一致しません。")
                    return old["id"]
                if any(r["status"] in ACTIVE for r in rows):
                    raise MediaError(
                        "別の文字起こしを処理中です。履歴から進捗を確認してください。"
                    )
                required = (
                    int(source.duration * 32000)
                    + (source.identity[0] if source.mode == "upload" else 0)
                    + 256 * 1024**2
                )
                if shutil.disk_usage(self.base).free < required:
                    raise MediaError("作業用ディスクの空き容量が不足しています。")
                job_id = uuid4().hex
                folder = self.folder(job_id)
                folder.mkdir(parents=True)
                try:
                    input_path = source.path
                    if source.mode == "upload":
                        input_path = folder / (
                            "input" + Path(source.name).suffix.lower()
                        )
                        with (
                            source.path.open("rb") as incoming,
                            input_path.open("xb") as outgoing,
                        ):
                            shutil.copyfileobj(incoming, outgoing, 1024**2)
                    if _identity(source.path) != source.identity:
                        raise MediaError(
                            "保存中に元ファイルが変更されました。もう一度確認してください。"
                        )
                    request = {
                        "schema_version": 1,
                        "id": job_id,
                        "source_name": source.name,
                        "source_path": str(input_path),
                        "identity": list(_identity(input_path)),
                        "duration": source.duration,
                        "track": asdict(track),
                        "language": language,
                        "model": MODELS[mode],
                        "mode": mode,
                        "hotwords": hotwords,
                        "device": device,
                        "ffmpeg": str(tools.ffmpeg),
                        "root": str(self.root),
                    }
                    state = {
                        "schema_version": 1,
                        "id": job_id,
                        "name": source.name,
                        "created": time.time(),
                        "status": "starting",
                        "progress": 0.0,
                        "elapsed": 0.0,
                        "message": "起動しています",
                        "error": "",
                        "operation_id": operation_id,
                        "fingerprint": fingerprint,
                    }
                    atomic_json(folder / "request.json", request)
                    atomic_json(folder / "state.json", state)
                    env = os.environ.copy()
                    paths = [str(self.root)]
                    if dependency_dir(self.root).is_dir():
                        paths.insert(0, str(dependency_dir(self.root)))
                    if env.get("PYTHONPATH"):
                        paths.append(env["PYTHONPATH"])
                    env.update(
                        PYTHONPATH=os.pathsep.join(paths),
                        PYTHONNOUSERSITE="1",
                        PYTHONDONTWRITEBYTECODE="1",
                        PYTHONIOENCODING="utf-8",
                        HF_HUB_DISABLE_TELEMETRY="1",
                        HF_HUB_DISABLE_XET="1",
                        HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
                    )
                    with (folder / "worker.log").open("wb") as log:
                        process = subprocess.Popen(
                            [
                                sys.executable,
                                "-B",
                                str(self.root / "app_core/transcription_worker.py"),
                                str(folder),
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            env=env,
                            cwd=self.root,
                            **_process_options(),
                        )
                    # Retain handles only until exit; do not wait or terminate at app shutdown.
                    threading.Thread(target=process.wait, daemon=True).start()
                except Exception:
                    shutil.rmtree(folder)
                    raise
                return job_id
        except OSError as exc:
            raise MediaError(
                "文字起こしを開始できません。保存先の空き容量・権限を確認してください。"
            ) from exc

    def cancel(self, job_id):
        if self._state(job_id)["status"] in ACTIVE:
            (self.folder(job_id) / "cancel").touch()

    def result(self, job_id):
        state = self._state(job_id)
        folder = self.folder(job_id)
        request = read_json(folder / "request.json", {})
        segments = []
        path = folder / "segments.jsonl"
        if path.exists():
            with path.open(encoding="utf-8") as incoming:
                for line in incoming:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        break  # An interrupted last append never hides earlier segments.
                    segments.append(row)
        edits = read_json(folder / "edits.json", {"revision": 0, "texts": {}})
        for row in segments:
            row["text"] = edits["texts"].get(str(row["id"]), row["original_text"])
        return {
            "schema_version": 1,
            "id": job_id,
            "source_name": state["name"],
            "state": state,
            "language": request.get("language"),
            "model": request.get("model"),
            "duration": request.get("duration"),
            "track": request.get("track"),
            "revision": edits["revision"],
            "segments": segments,
        }

    def save_edits(self, job_id, revision, texts):
        if self._state(job_id)["status"] in ACTIVE:
            raise MediaError("処理が終わってから修正できます。")
        folder = self.folder(job_id)
        try:
            with _mutex, file_lock(folder / ".edit.lock"):
                result = self.result(job_id)
                if revision != result["revision"]:
                    raise MediaError(
                        "別の画面で修正されました。下書きを控えてから履歴を開き直してください。"
                    )
                known = {row["id"]: row["original_text"] for row in result["segments"]}
                edits = read_json(folder / "edits.json", {"revision": 0, "texts": {}})
                for key, value in texts.items():
                    if (
                        key not in known
                        or not isinstance(value, str)
                        or len(value) > 20000
                        or "\x00" in value
                    ):
                        raise MediaError(
                            "修正内容を確認してください（1区間20000文字以内）。"
                        )
                    edits["texts"][str(key)] = value
                edits["revision"] += 1
                atomic_json(folder / "edits.json", edits)
                return edits["revision"]
        except OSError as exc:
            raise MediaError(
                "修正を保存できませんでした。入力を保持しています。"
            ) from exc

    def playback(self, job_id, start, end):
        """Only decode the requested section, never load a long original into UI RAM."""
        folder = self.folder(job_id)
        path = folder / "audio.wav"
        if not path.is_file():
            raise MediaError("聞き直し用の音声がまだ準備できていません。")
        if (
            not all(math.isfinite(float(v)) for v in (start, end))
            or not 0 <= start < end
        ):
            raise MediaError("再生区間を確認してください。")
        tools = discover_tools(self.root)
        command = [
            str(tools.ffmpeg),
            "-v",
            "error",
            "-nostdin",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            str(start),
            "-i",
            str(path),
            "-t",
            str(min(60, end - start + 0.5)),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            "pipe:1",
        ]
        try:
            output = subprocess.run(
                command, capture_output=True, timeout=30, **_process_options()
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError(
                "音声区間を読み出せませんでした。もう一度お試しください。"
            ) from exc
        if output.returncode or not output.stdout:
            raise MediaError("音声区間を読み出せませんでした。")
        return output.stdout

    def delete(self, job_id):
        try:
            with _mutex, file_lock(self.base / ".catalog.lock"):
                if self._state(job_id)["status"] in ACTIVE:
                    raise MediaError(
                        "処理中の履歴は削除できません。先に中止してください。"
                    )
                shutil.rmtree(self.folder(job_id))
        except OSError as exc:
            raise MediaError(
                "履歴を削除できませんでした。もう一度お試しください。"
            ) from exc
