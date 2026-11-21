"""Durable local transcription jobs; independent of the personal JSON journal."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import importlib.util
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import stat
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
BLOCKING = ACTIVE | {"unavailable"}
MODELS = {
    "standard": "small", "accurate": "medium",
    "precise": "large-v3", "fast": "turbo",
}
_mutex = threading.RLock()


@dataclass(frozen=True)
class MatroskaTranscriptionSource(Source):
    """MKV-specific presentation/clock metadata without changing legacy sources."""

    default_track_index: int | None = None
    media_kind: str = "mkv"


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024**2):
            digest.update(block)
    return digest.hexdigest()


def checkpoint_signature(request):
    """Audio/configuration identity shared by the launcher and isolated worker."""
    values = {key: request.get(key) for key in (
        "source_sha256", "duration", "track", "language", "model", "hotwords", "device", "audio_memo_id"
    )}
    values.update(pipeline=1, chunk_seconds=300, overlap_seconds=2, beam_size=5,
                  vad_filter=True, condition_on_previous_text=False)
    if request.get("media_kind") == "mkv":
        values.update(media_kind="mkv", timeline=request.get("timeline"))
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _checkpoint_file(path):
    value = Path(path).lstat()
    if not stat.S_ISREG(value.st_mode) or getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
        raise MediaError("再開用の履歴にリンクは使用できません。")


def _checkpoint_json(path):
    _checkpoint_file(path)
    with Path(path).open("rb") as stream:
        raw = stream.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise MediaError("再開用の履歴が大きすぎます。")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (ValueError, TypeError, RecursionError) as exc:
        raise MediaError("再開用の履歴を確認できません。") from exc


def load_checkpoint(folder, request):
    folder = Path(folder)
    checkpoint = _checkpoint_json(folder / "checkpoint.json")
    try:
        completed, duration = checkpoint["completed_seconds"], request["duration"]
        if (checkpoint.get("schema_version") != 1 or checkpoint.get("job_id") != folder.name
                or checkpoint.get("signature") != checkpoint_signature(request)
                or checkpoint.get("source_sha256") != request.get("source_sha256")
                or not isinstance(checkpoint.get("source_sha256"), str) or not re.fullmatch("[0-9a-f]{64}", checkpoint["source_sha256"])
                or type(completed) not in {int, float} or not math.isfinite(completed)
                or type(duration) not in {int, float} or not math.isfinite(duration)
                or not 0 < completed <= duration <= 24 * 3600
                or (completed % 300 != 0 and abs(completed - duration) > 0.001)
                or type(checkpoint.get("segments_count")) is not int or not 0 <= checkpoint["segments_count"] <= 1_000_000
                or type(checkpoint.get("segments_bytes")) is not int or not 0 <= checkpoint["segments_bytes"] <= 1024**3
                or not isinstance(checkpoint.get("segments_sha256"), str) or not re.fullmatch("[0-9a-f]{64}", checkpoint["segments_sha256"])
                or not isinstance(checkpoint.get("detected_languages"), list) or len(checkpoint["detected_languages"]) > 128
                or any(not isinstance(value, str) or not re.fullmatch("[a-z0-9-]{1,16}", value) for value in checkpoint["detected_languages"])):
            raise ValueError("incompatible checkpoint")
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise MediaError("同じ音声・認識条件の完了区間を確認できません。以前の結果は保持しています。") from exc
    return checkpoint


def checkpoint_prefix(path, checkpoint, destination=None, *, exact=False):
    """Validate/copy only committed lines; a failed window's tail is excluded."""
    _checkpoint_file(path)
    digest = hashlib.sha256()
    remaining = checkpoint["segments_bytes"]
    count = 0
    with Path(path).open("rb") as incoming:
        while remaining:
            line = incoming.readline(min(1024**2 + 1, remaining + 1))
            if not line or len(line) > min(1024**2, remaining) or not line.endswith(b"\n"):
                raise MediaError("再開区間の原文が途中で切れています。")
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("invalid segment object")
                start, end = row["start"], row["end"]
                if (type(row.get("id")) is not int or row["id"] != count
                        or type(start) not in {int, float} or type(end) not in {int, float}
                        or not math.isfinite(start) or not math.isfinite(end)
                        or not 0 <= start < end <= checkpoint["completed_seconds"] + 0.001
                        or not isinstance(row.get("original_text"), str) or "\0" in row["original_text"]):
                    raise ValueError("invalid segment")
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                raise MediaError("再開区間の原文・時刻を確認できません。") from exc
            digest.update(line)
            if destination is not None:
                destination.write(line)
            remaining -= len(line)
            count += 1
        if exact and incoming.read(1):
            raise MediaError("再開用の原文に未完了の末尾があります。")
    if count != checkpoint["segments_count"] or digest.hexdigest() != checkpoint["segments_sha256"]:
        raise MediaError("再開区間の件数・ハッシュが一致しません。")
    return digest


def copy_checkpoint(previous, current, request):
    """Copy a stopped job's committed prefix into a new, independently owned job."""
    previous, current = Path(previous), Path(current)
    for path in (previous / "state.json", previous / "request.json", previous / "checkpoint.json", previous / "segments.jsonl"):
        _checkpoint_file(path)
    if (previous / ".worker.lock").exists() or (previous / ".worker.lock").is_symlink():
        _checkpoint_file(previous / ".worker.lock")
    with file_lock(previous / ".worker.lock", blocking=False):
        state = _checkpoint_json(previous / "state.json")
        if state.get("status") in ACTIVE:
            created = state.get("created")
            if type(created) not in {int, float} or not math.isfinite(created) or time.time() - created <= 45:
                raise MediaError("以前の認識処理が終了してから再開してください。")
        elif state.get("status") not in {"failed", "cancelled", "interrupted"}:
            raise MediaError("完了済みの認識は、最初から再認識してください。")
        previous_request = _checkpoint_json(previous / "request.json")
        checkpoint_raw = _checkpoint_json(previous / "checkpoint.json")
        previous_request.setdefault("source_sha256", checkpoint_raw.get("source_sha256"))
        checkpoint = load_checkpoint(previous, previous_request)
        if checkpoint["signature"] != checkpoint_signature(request):
            raise MediaError("音声または認識条件が異なるため、途中から再開できません。")
        with (current / "segments.jsonl").open("xb") as output:
            checkpoint_prefix(previous / "segments.jsonl", checkpoint, output)
            output.flush()
            os.fsync(output.fileno())
        checkpoint = {**checkpoint, "job_id": current.name, "copied_from": previous.name}
        atomic_json(current / "checkpoint.json", checkpoint)
        return checkpoint


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
    for attempt in range(3):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.025)


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
    """Owned MP3/MP4/MKV inputs; managed recording WAVs use a separate entry."""

    def _upload_filename(self, name):
        suffix = Path(name).suffix.lower()
        if suffix not in {".mp3", ".mp4", ".mkv"}:
            raise MediaError("MP4／MP3／MKVを選択してください。")
        return "input" + suffix

    def _source(self, path, name, mode):
        before = _identity(path)
        try:
            data = _probe(self._tools(), path)
        except MediaError as exc:
            raise MediaError(
                "音声情報を取得できません。正常なMP4／MP3／MKVを選択してください。"
            ) from exc
        formats = set(str(data.get("format", {}).get("format_name", "")).split(","))
        suffix = Path(name).suffix.lower()
        expected = {".mp3": {"mp3"}, ".wav": {"wav"}, ".mp4": {"mov", "mp4"}}
        if suffix == ".mkv":
            from app_core.matroska import inspect_matroska

            inspect_matroska(path, data)
        elif not formats.intersection(expected.get(suffix, set())):
            raise MediaError("ファイルの内容と音声の拡張子が一致しません。")
        tracks = []
        preferred = []
        for row in data.get("streams", []):
            if row.get("codec_type") != "audio":
                continue
            index = row.get("index")
            if type(index) is not int or index < 0:
                raise MediaError("音声トラック情報が不正です。")
            tags = row.get("tags") or {}
            disposition = row.get("disposition")
            if suffix == ".mkv" and isinstance(disposition, dict) and disposition.get("default") == 1:
                preferred.append(index)
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
        if suffix == ".mkv" and duration:
            try:
                origin = float(data["format"].get("start_time", 0))
                if not math.isfinite(origin):
                    raise ValueError("nonfinite container origin")
                # Matroska duration is its ending timestamp. -start_at_zero
                # shifts the source origin, so measure the same span here.
                duration -= origin
            except (TypeError, ValueError, OverflowError) as exc:
                raise MediaError("MKVの開始時刻・長さを確認できません。") from exc
        if (
            not tracks
            or len(tracks) > 256
            or len({t.index for t in tracks}) != len(tracks)
        ):
            raise MediaError("利用できる音声トラックがありません。")
        if not duration or not 0 < duration <= 24 * 3600:
            raise MediaError("長さを確認できる、24時間以内の音声を選択してください。")
        if before != _identity(path):
            raise MediaError(
                "確認中に元ファイルが変更されました。もう一度確認してください。"
            )
        values = (uuid4().hex, path, name, mode, before, duration, tuple(tracks))
        if suffix == ".mkv":
            return MatroskaTranscriptionSource(*values, default_track_index=preferred[0] if preferred else tracks[0].index)
        return Source(*values)

    def prepare_local(self, owner, value):
        path = Path(str(value).strip().strip('"')).expanduser()
        if (
            not path.is_absolute()
            or path.suffix.lower() not in {".mp3", ".mp4", ".mkv"}
            or str(path).startswith(("\\\\", "//"))
        ):
            raise MediaError("PC内のMP4／MP3／MKVファイルのフルパスを指定してください。")
        try:
            path = path.resolve(strict=True)
        except (OSError, ValueError, RuntimeError) as exc:
            raise MediaError(
                "元ファイルが見つかりません。場所を確認してください。"
            ) from exc
        return self._prepare(owner, path=path, name=path.name)

    def prepare_upload(self, owner, name, buffer):
        if Path(str(name)).suffix.lower() not in {".mp3", ".mp4", ".mkv"}:
            raise MediaError("MP4／MP3／MKVを選択してください。")
        if (
            not isinstance(buffer, (bytes, bytearray, memoryview))
            or not 0 < len(buffer) <= UPLOAD_LIMIT
        ):
            raise MediaError("アップロードは空でない200MB以内のファイルに対応します。")
        return self._prepare(
            owner, buffer=buffer, name=name.replace("\\", "/").split("/")[-1]
        )

    def prepare_recording(self, owner, path):
        """Prepare an immutable WAV already saved by the audio-memo service."""
        from app_core.audio_memo_service import (
            validate_recording_file,
        )

        with self._lock:
            self._owner(owner)
            if self._busy(owner):
                raise MediaError("処理中です。完了してから録音を選択してください。")
        path = Path(path)
        memo_id = path.parent.name
        expected = self.root / "transcription_data/memos" / memo_id / "audio.wav"
        if (
            not path.is_absolute()
            or not re.fullmatch("[a-f0-9]{32}", memo_id)
            or path.name != "audio.wav"
            or path != expected
        ):
            raise MediaError("保存済みの録音メモを選択してください。")
        try:
            for candidate in (
                self.root / "transcription_data",
                self.root / "transcription_data/memos",
                expected.parent,
                expected,
            ):
                if candidate.is_symlink():
                    raise MediaError("録音メモの保存先を確認してください。")
            path = path.resolve(strict=True)
            if not path.is_relative_to(self.root):
                raise MediaError("録音メモの保存先を確認してください。")
            before = _identity(path)
            meta = read_json(path.parent / "memo.json", {})
            is_long = (isinstance(meta, dict) and meta.get("source_kind") == "long_recording"
                       and meta.get("recording_id") == memo_id and meta.get("id") == memo_id)
            validate_recording_file(path, long_recording=is_long)
            if _identity(path) != before:
                raise MediaError("録音が変更されました。もう一度選択してください。")
        except MediaError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise MediaError("保存済みの録音を読み取れません。") from exc
        prepared = self._prepare(owner, path=path, name=path.name)
        if prepared.identity != before:
            raise MediaError("録音が変更されました。もう一度選択してください。")
        source = replace(prepared, id=memo_id, mode="recording")
        with self._lock:
            self._owner(owner)
            work = self._works.get(owner)
            if work is None or work.source is not prepared:
                raise MediaError("録音の選択が変わりました。もう一度選択してください。")
            work.source = source
        return source


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
                    values.append(
                        {
                            "id": path.name,
                            "status": "unavailable",
                            "name": "状態を確認できない履歴",
                            "created": 0.0,
                            "progress": 0.0,
                            "elapsed": 0.0,
                            "message": "履歴を読み取れません。少し待って再読込してください。",
                            "error": "処理中か確認できないため、新しい文字起こしを停止しています。",
                        }
                    )
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
        *,
        audio_memo_id=None,
        resume_from=None,
    ):
        if resume_from is not None and (not isinstance(resume_from, str) or not re.fullmatch("[a-f0-9]{32}", resume_from)):
            raise MediaError("再開元の認識履歴IDを確認してください。")
        if audio_memo_id is not None and (
            not isinstance(audio_memo_id, str)
            or not re.fullmatch("[a-f0-9]{32}", audio_memo_id)
        ):
            raise MediaError("録音メモの識別情報を確認してください。")
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
                if audio_memo_id is not None:
                    fingerprint.append(audio_memo_id)
                if resume_from is not None:
                    fingerprint.extend(["resume_from", resume_from])
                if old:
                    if old.get("fingerprint") != fingerprint:
                        raise MediaError("処理IDと設定が一致しません。")
                    return old["id"]
                if any(r["status"] == "unavailable" for r in rows):
                    raise MediaError(
                        "履歴の状態を確認できません。履歴を再読込してから開始してください。"
                    )
                if any(r["status"] in BLOCKING for r in rows):
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
                        "source_sha256": file_sha256(input_path),
                    }
                    if getattr(source, "media_kind", None) == "mkv":
                        request.update(media_kind="mkv", timeline="container_start")
                    if _identity(source.path) != source.identity:
                        raise MediaError("確認中に元音声が変更されました。")
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
                    if audio_memo_id is not None:
                        request["audio_memo_id"] = audio_memo_id
                        state["audio_memo_id"] = audio_memo_id
                    if resume_from is not None:
                        request["resume_from"] = resume_from
                        checkpoint = copy_checkpoint(self.folder(resume_from), folder, request)
                        state.update(resume_from=resume_from, resumed_seconds=checkpoint["completed_seconds"],
                                     segments=checkpoint["segments_count"])
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
                state = self._state(job_id)
                if state["status"] in ACTIVE:
                    raise MediaError(
                        "処理中の履歴は削除できません。先に中止してください。"
                    )
                memo_id = state.get("audio_memo_id")
                if memo_id is not None:
                    if not isinstance(memo_id, str) or not re.fullmatch("[a-f0-9]{32}", memo_id):
                        raise MediaError("録音メモとの対応を確認できないため、履歴を削除できません。")
                    memo_path = self.base / "memos" / memo_id / "memo.json"
                    if memo_path.exists() or memo_path.is_symlink():
                        raise MediaError(
                            "録音メモに関連する認識履歴は削除できません。『録音・文字起こしメモ』で確認してください。"
                        )
                shutil.rmtree(self.folder(job_id))
        except OSError as exc:
            raise MediaError(
                "履歴を削除できませんでした。もう一度お試しください。"
            ) from exc
