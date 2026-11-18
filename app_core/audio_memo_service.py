"""Durable recording-first audio memos, separate from learning-record locks.

memo.json is authoritative; transcript.txt is a repairable UTF-8 derivative.
All record paths are fixed by an opaque ID, never by the editable title.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import threading
import time
from uuid import uuid4
import wave

from app_core.media_conversion import MediaError, _identity
from app_core.transcription import (ACTIVE, TranscriptionSources, TranscriptionStore, atomic_json, file_lock,
                                    read_json, file_sha256, timestamp, load_checkpoint, checkpoint_prefix)


MAX_RECORDING_BYTES = 64 * 1024**2
MAX_RECORDING_SECONDS = 600
MAX_LONG_RECORDING_SECONDS = 10800
MAX_LONG_RECORDING_BYTES = MAX_LONG_RECORDING_SECONDS * 32000 + 1024**2
MAX_TEXT_CHARS = 1_000_000
_ID = re.compile(r"[0-9a-f]{32}")
_mutex = threading.RLock()


class _StartBusy(MediaError):
    pass


def _now():
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def validate_recording(data: bytes) -> dict:
    """Validate a complete PCM WAV without FFmpeg or an ASR installation."""
    if not isinstance(data, (bytes, bytearray, memoryview)) or not 0 < len(data) <= MAX_RECORDING_BYTES:
        raise MediaError("録音は空でない64MiB以内のWAVにしてください。原音をダウンロードして保存できます。")
    data = bytes(data)
    try:
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE" or struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
            raise ValueError("incomplete RIFF")
        with wave.open(io.BytesIO(data), "rb") as source:
            channels, width, rate, frames = source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getnframes()
            duration = frames / rate if rate else 0
            if (source.getcomptype() != "NONE" or channels not in {1, 2} or width not in {1, 2, 3, 4}
                    or not 8000 <= rate <= 192000 or not 0 < duration <= MAX_RECORDING_SECONDS):
                raise ValueError("unsupported recording")
            if len(source.readframes(frames)) != frames * channels * width:
                raise ValueError("truncated frames")
        return {"duration": duration, "sample_rate": rate, "channels": channels, "sample_width": width, "frames": frames}
    except (ValueError, EOFError, wave.Error, struct.error, ZeroDivisionError) as exc:
        raise MediaError("録音WAVを確認できません。PCM音声（1〜2ch、10分以内）を録音し直してください。") from exc


def validate_recording_file(path, *, long_recording=False):
    """Validate WAV frames with bounded reads, including native long recordings."""
    path = Path(path)
    limit = MAX_LONG_RECORDING_BYTES if long_recording else MAX_RECORDING_BYTES
    seconds = MAX_LONG_RECORDING_SECONDS if long_recording else MAX_RECORDING_SECONDS
    try:
        _not_link(path)
        size = path.stat().st_size
        if not 44 <= size <= limit:
            raise ValueError("recording size")
        with path.open("rb") as stream:
            header = stream.read(12)
            if (header[:4] != b"RIFF" or header[8:12] != b"WAVE"
                    or struct.unpack_from("<I", header, 4)[0] + 8 != size):
                raise ValueError("incomplete RIFF")
            position, data_size = 12, None
            while position < size:
                stream.seek(position)
                chunk = stream.read(8)
                if len(chunk) != 8:
                    raise ValueError("incomplete WAV chunk")
                length = struct.unpack_from("<I", chunk, 4)[0]
                end = position + 8 + length
                if end > size:
                    raise ValueError("incomplete WAV payload")
                if chunk[:4] == b"data":
                    if data_size is not None:
                        raise ValueError("multiple WAV data chunks")
                    data_size = length
                position = end + (length % 2 if end < size else 0)
            if position != size:
                raise ValueError("invalid WAV padding")
            stream.seek(0)
            with wave.open(stream, "rb") as audio:
                channels, width, rate, frames = audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()
                duration = frames / rate if rate else 0
                if (audio.getcomptype() != "NONE" or channels not in {1, 2} or width not in {1, 2, 3, 4}
                        or not 8000 <= rate <= 192000 or not 0 < duration <= seconds
                        or data_size != frames * channels * width
                        or (long_recording and (channels, width, rate) != (1, 2, 16000))):
                    raise ValueError("unsupported PCM")
                remaining = frames
                while remaining:
                    count = min(remaining, max(1, 1024**2 // (channels * width)))
                    if len(audio.readframes(count)) != count * channels * width:
                        raise ValueError("truncated PCM")
                    remaining -= count
        return {"duration": duration, "sample_rate": rate, "channels": channels, "sample_width": width, "frames": frames}
    except (OSError, ValueError, EOFError, wave.Error, struct.error, ZeroDivisionError) as exc:
        raise MediaError("保存済み録音WAVの形式・長さ・容量を確認できません。原音は保持しています。") from exc


def _not_link(path):
    try:
        value = Path(path).lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
        raise MediaError("録音メモの保存先にリンクは使用できません。")


def _atomic_bytes(path, data):
    _not_link(path)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _title(value):
    if not isinstance(value, str) or len(value) > 200 or "\0" in value:
        raise MediaError("タイトルは200文字以内で入力してください。")
    return value.strip()


class AudioMemoStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / "transcription_data" / "memos"
        self.transcriptions = TranscriptionStore(self.root)

    def _safe_base(self):
        for path in (self.root, self.root / "transcription_data", self.base, self.base / ".locks", self.base / ".deleted"):
            _not_link(path)
        if not self.base.resolve().is_relative_to(self.root):
            raise MediaError("録音メモの保存先を確認してください。")

    def _folder(self, memo_id):
        if not isinstance(memo_id, str) or not _ID.fullmatch(memo_id):
            raise MediaError("録音メモIDが不正です。")
        self._safe_base()
        folder = self.base / memo_id
        _not_link(folder)
        if folder.resolve().parent != self.base.resolve():
            raise MediaError("録音メモの保存先を確認してください。")
        for name in ("audio.wav", "memo.json", "transcript.txt"):
            _not_link(folder / name)
        return folder

    @contextmanager
    def _locked(self, memo_id):
        self._folder(memo_id)
        self.base.mkdir(parents=True, exist_ok=True)
        self._safe_base()
        deadline = time.monotonic() + 10
        with _mutex:
            while True:
                lock_path = self.base / ".locks" / (memo_id + ".lock")
                _not_link(lock_path)
                guard = file_lock(lock_path, blocking=False)
                try:
                    guard.__enter__()
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise MediaError("別の録音メモ操作を待っています。少し待って再試行してください。") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                guard.__exit__(None, None, None)

    def _read(self, memo_id):
        folder = self._folder(memo_id)
        try:
            meta = read_json(folder / "memo.json")
            if (not isinstance(meta, dict) or meta.get("schema_version") != 1 or meta.get("id") != memo_id
                    or type(meta.get("revision")) is not int or meta["revision"] < 0
                    or not isinstance(meta.get("text"), str) or not isinstance(meta.get("title"), str)
                    or not isinstance(meta.get("original_text"), str) or not isinstance(meta.get("original_versions"), list)
                    or type(meta.get("created")) not in {int, float} or not math.isfinite(meta["created"])
                    or meta.get("status") not in ACTIVE | {"recorded", "start_failed", "done", "failed", "cancelled", "interrupted"}
                    or (meta.get("start_reserved_at") is not None and (type(meta["start_reserved_at"]) not in {int, float}
                        or not math.isfinite(meta["start_reserved_at"])))
                    or not isinstance(meta.get("audio_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", meta["audio_sha256"])
                    or any(not isinstance(row, dict) or not isinstance(row.get("job_id"), str) or not _ID.fullmatch(row["job_id"])
                           for row in meta["original_versions"])
                    or (meta.get("job_id") is not None and (not isinstance(meta["job_id"], str) or not _ID.fullmatch(meta["job_id"])))
                    or (meta.get("start_operation_id") is not None and (not isinstance(meta["start_operation_id"], str) or not _ID.fullmatch(meta["start_operation_id"])))
                    or meta.get("source_kind") not in {None, "browser_recording", "long_recording"}
                    or (meta.get("source_kind") == "long_recording" and meta.get("recording_id") != memo_id)
                    or not (folder / "audio.wav").is_file()):
                raise ValueError("invalid memo metadata")
            return meta
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise MediaError("録音メモを読み取れません。原音は削除せず、保存場所を確認してください。") from exc

    @contextmanager
    def _launch_guard(self, memo_id):
        """Lease only the spawning phase; the worker never needs this lock."""
        self._folder(memo_id)
        path = self.base / ".locks" / (memo_id + ".start.lock")
        _not_link(path)
        guard = file_lock(path, blocking=False)
        try:
            guard.__enter__()
        except OSError as exc:
            raise _StartBusy("この録音の開始処理を確認中です。少し待って履歴を更新してください。") from exc
        try:
            yield
        finally:
            guard.__exit__(None, None, None)

    def _write(self, meta):
        path = self._folder(meta["id"]) / "memo.json"
        atomic_json(path, meta)

    def _with_txt(self, meta):
        # The first commit makes the saved text authoritative even if TXT fails.
        meta.update(txt_synced=False, txt_error="TXTは未反映です。『TXTを再生成』で作り直せます。")
        self._write(meta)
        return self._regenerate(meta)

    def _regenerate(self, meta):
        try:
            _atomic_bytes(self._folder(meta["id"]) / "transcript.txt", meta["text"].encode("utf-8-sig"))
            final = dict(meta, txt_synced=True, txt_error="", txt_revision=meta["revision"])
            self._write(final)
            return final
        except OSError as exc:
            failed = dict(meta, txt_synced=False, txt_error=f"本文JSONは保存済みですが、TXTを反映できませんでした。再生成してください。（{type(exc).__name__}）")
            try:
                self._write(failed)
            except OSError:
                pass  # The authoritative first commit still says TXT is stale.
            return failed

    def _view(self, meta):
        result = deepcopy(meta)
        result.setdefault("progress", 0.0)
        result.setdefault("elapsed", 0.0)
        result.setdefault("message", "録音を保存しました。")
        if meta.get("job_id"):
            try:
                state, _ = self._job(meta, meta["job_id"])
                for key in ("status", "progress", "elapsed", "message", "error", "resumed_seconds", "resume_from"):
                    if key in state:
                        result[key] = state[key]
            except MediaError as exc:
                result.update(status="unavailable", error=str(exc))
        result["active"] = result.get("status") in ACTIVE | {"unavailable"}
        return result

    def get(self, memo_id):
        return self._view(self._read(memo_id))

    def history(self):
        self._safe_base()
        if not self.base.exists():
            return []
        rows = []
        for folder in self.base.iterdir():
            if not _ID.fullmatch(folder.name):
                continue
            try:
                rows.append(self.get(folder.name))
            except (MediaError, OSError) as exc:
                rows.append({"id": folder.name, "title": "読み取れない録音メモ", "created": 0.0,
                             "created_at": "", "revision": None, "status": "unavailable", "active": True,
                             "error": str(exc), "text": "", "original_text": "", "job_id": None, "txt_synced": False})
        return sorted(rows, key=lambda row: float(row.get("created", 0)), reverse=True)

    def capture(self, data: bytes, capture_id: str, title=""):
        if not isinstance(capture_id, str) or not 0 < len(capture_id) <= 200 or "\0" in capture_id:
            raise MediaError("録音の識別情報を確認してください。録音し直して再試行できます。")
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        details = validate_recording(data)
        title = _title(title)
        capture_key = hashlib.sha256(capture_id.encode("utf-8")).hexdigest()
        memo_id, digest = capture_key[:32], hashlib.sha256(data).hexdigest()
        try:
            with self._locked(memo_id):
                tombstone = self.base / ".deleted" / (memo_id + ".json")
                _not_link(tombstone)
                if tombstone.exists():
                    raise MediaError("この録音は削除済みです。新しい録音を作成してください。")
                folder = self._folder(memo_id)
                if (folder / "memo.json").exists():
                    old = self._read(memo_id)
                    if old.get("capture_key") != capture_key or old.get("audio_sha256") != digest:
                        raise MediaError("同じ録音IDで異なる音声が送られました。新しい録音として保存してください。")
                    if file_sha256(folder / "audio.wav") != digest:
                        raise MediaError("保存済み原音が変更されています。上書きせず保存先を確認してください。")
                    return self._view(old)
                folder.mkdir(exist_ok=True)
                audio = folder / "audio.wav"
                if audio.exists():
                    if file_sha256(audio) != digest:
                        raise MediaError("保存途中の原音と今回の録音が異なります。原音は上書きしません。")
                else:
                    _atomic_bytes(audio, bytes(data))
                created = audio.stat().st_mtime
                created_at = datetime.fromtimestamp(created).astimezone().isoformat(timespec="microseconds")
                meta = {"schema_version": 1, "id": memo_id, "capture_key": capture_key, "audio_sha256": digest,
                        "source_kind": "browser_recording",
                        "created": created, "created_at": created_at, "updated_at": _now(), "revision": 0,
                        "title": title or "録音メモ " + datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M"),
                        "text": "", "original_text": "", "original_versions": [], "edited": False, "text_edited": False,
                        "job_id": None, "start_operation_id": None, "start_settings": None,
                        "status": "recorded", "error": "", **details}
                return self._view(self._with_txt(meta))
        except OSError as exc:
            raise MediaError("録音を保存できません。元WAVをダウンロードし、空き容量・保存先を確認してください。保存済み原音は消去しません。") from exc

    def audio_path(self, memo_id):
        self._read(memo_id)
        return self._folder(memo_id) / "audio.wav"

    def _recording_manifest(self, recording_id):
        if not isinstance(recording_id, str) or not _ID.fullmatch(recording_id):
            raise MediaError("長時間録音IDが不正です。")
        folder = self.root / "transcription_data/recordings" / recording_id
        for path in (self.root, folder.parent.parent, folder.parent, folder, folder / "chunks", folder / "recording.json"):
            _not_link(path)
        if folder.resolve().parent != (self.root / "transcription_data/recordings").resolve():
            raise MediaError("長時間録音の保存先を確認してください。")
        with (folder / "recording.json").open("rb") as stream:
            raw = stream.read(1024**2 + 1)
        if len(raw) > 1024**2:
            raise MediaError("長時間録音の台帳が大きすぎます。")
        try:
            meta = json.loads(raw)
            if not isinstance(meta, dict):
                raise ValueError("invalid recording manifest")
            rows = meta["segments"]
            owner = meta["owner"]
            if (type(meta.get("schema_version")) is not int or meta["schema_version"] != 1 or meta.get("id") != recording_id
                    or meta.get("status") not in {"stopped", "done", "interrupted", "failed"}
                    or any(type(meta.get(key)) is not int for key in ("rate", "channels", "sample_width"))
                    or (meta.get("rate"), meta.get("channels"), meta.get("sample_width")) != (16000, 1, 2)
                    or not isinstance(rows, list) or not 0 < len(rows) <= MAX_LONG_RECORDING_SECONDS
                    or type(meta.get("total_frames")) is not int or not 0 < meta["total_frames"] <= MAX_LONG_RECORDING_SECONDS * 16000
                    or type(meta.get("created")) not in {int, float} or not math.isfinite(meta["created"])
                    or not isinstance(owner, dict) or type(owner.get("pid")) is not int or owner["pid"] <= 0
                    or not isinstance(owner.get("creation_time"), str) or not re.fullmatch(r"[0-9]{1,30}", owner["creation_time"])
                    or int(owner["creation_time"]) <= 0):
                raise ValueError("invalid terminal manifest")
            total = 0
            for index, row in enumerate(rows):
                if (not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] != index
                        or row.get("file") != f"chunks/{index:06d}.wav"
                        or type(row.get("frames")) is not int or row["frames"] <= 0
                        or not isinstance(row.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", row["sha256"])):
                    raise ValueError("invalid recording segment")
                total += row["frames"]
            if total != meta["total_frames"]:
                raise ValueError("recording frames differ")
            _title(meta.get("title", ""))
            datetime.fromtimestamp(meta["created"])
        except (ValueError, TypeError, KeyError, OverflowError, OSError, RecursionError) as exc:
            raise MediaError("確定した長時間録音の連番・長さ・台帳を確認できません。原音は保持しています。") from exc
        stable = {key: meta[key] for key in ("id", "rate", "channels", "sample_width", "total_frames")}
        stable["segments"] = [{key: row[key] for key in ("index", "file", "frames", "sha256", "bytes") if key in row} for row in rows]
        signature = hashlib.sha256(json.dumps(stable,
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        return folder, meta, signature

    def _verified_recording_chunks(self, folder, recording):
        verified = []
        for row in recording["segments"]:
            source = folder / row["file"]
            _not_link(source)
            before = _identity(source)
            details = validate_recording_file(source, long_recording=True)
            if (details["frames"] != row["frames"]
                    or ("bytes" in row and (type(row["bytes"]) is not int or row["bytes"] != before[0]))
                    or file_sha256(source) != row["sha256"] or _identity(source) != before):
                raise MediaError("確定チャンクのサイズ・フレーム・ハッシュが一致しません。")
            verified.append((source, details, before))
        return verified

    def import_long_recording(self, recording_id):
        """Join verified committed PCM chunks without loading the recording in RAM."""
        temporary = None
        try:
            recording_folder, recording, signature = self._recording_manifest(recording_id)
            verified = self._verified_recording_chunks(recording_folder, recording)
            with self._locked(recording_id):
                tombstone = self.base / ".deleted" / (recording_id + ".json")
                _not_link(tombstone)
                if tombstone.exists():
                    raise MediaError("削除済みの録音メモを再作成できません。原チャンクは保持しています。")
                folder = self._folder(recording_id)
                if (folder / "memo.json").exists():
                    old = self._read(recording_id)
                    if (old.get("source_kind") != "long_recording" or old.get("recording_id") != recording_id
                            or old.get("recording_signature") != signature
                            or file_sha256(folder / "audio.wav") != old["audio_sha256"]):
                        raise MediaError("取り込み済み録音と今回の台帳・原音が一致しません。上書きしません。")
                    return self._view(old)
                folder.mkdir(exist_ok=True)
                temporary = folder / ("audio-" + uuid4().hex + ".tmp")
                with temporary.open("xb") as stream:
                    with wave.open(stream, "wb") as output:
                        output.setnchannels(1)
                        output.setsampwidth(2)
                        output.setframerate(16000)
                        for source, details, before in verified:
                            _not_link(source)
                            if _identity(source) != before:
                                raise MediaError("取り込み中に原音チャンクが変更されました。")
                            with wave.open(str(source), "rb") as audio:
                                remaining = details["frames"]
                                while remaining:
                                    count = min(remaining, 512 * 1024)
                                    block = audio.readframes(count)
                                    if len(block) != count * 2:
                                        raise MediaError("確定チャンクが途中で切れています。")
                                    output.writeframesraw(block)
                                    remaining -= count
                            if _identity(source) != before:
                                raise MediaError("取り込み中に原音チャンクが変更されました。")
                    stream.flush()
                    os.fsync(stream.fileno())
                if self._recording_manifest(recording_id)[2] != signature:
                    raise MediaError("取り込み中に録音台帳が変更されました。")
                details = validate_recording_file(temporary, long_recording=True)
                if details["frames"] != recording["total_frames"]:
                    raise MediaError("結合した録音の長さが一致しません。")
                digest = file_sha256(temporary)
                target = folder / "audio.wav"
                _not_link(target)
                if target.exists():
                    if file_sha256(target) != digest:
                        raise MediaError("保存途中の原音が今回の録音と異なります。上書きしません。")
                else:
                    os.replace(temporary, target)
                created = recording["created"]
                meta = {"schema_version": 1, "id": recording_id, "source_kind": "long_recording",
                        "recording_id": recording_id, "recording_signature": signature, "audio_sha256": digest,
                        "created": created, "created_at": datetime.fromtimestamp(created).astimezone().isoformat(timespec="microseconds"),
                        "updated_at": _now(), "revision": 0,
                        "title": _title(recording.get("title", "")) or "長時間録音 " + datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M"),
                        "text": "", "original_text": "", "original_versions": [], "edited": False, "text_edited": False,
                        "job_id": None, "start_operation_id": None, "start_settings": None, "status": "recorded", "error": "", **details}
                return self._view(self._with_txt(meta))
        except OSError as exc:
            raise MediaError("長時間録音を取り込めません。空き容量・保存先を確認してください。原チャンクは保持しています。") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def playback(self, memo_id, start_sec=0, seconds=60):
        try:
            valid = (type(start_sec) in {int, float} and type(seconds) in {int, float}
                     and math.isfinite(start_sec) and math.isfinite(seconds) and start_sec >= 0 and seconds > 0)
        except OverflowError:
            valid = False
        if not valid:
            raise MediaError("再生する開始時刻・秒数を確認してください。")
        path = self.audio_path(memo_id)
        try:
            with wave.open(str(path), "rb") as audio:
                rate, frames = audio.getframerate(), audio.getnframes()
                if rate <= 0 or frames <= 0 or start_sec >= frames / rate:
                    raise MediaError("録音の長さ以内の開始時刻を選択してください。")
                start = int(start_sec * rate)
                count = min(frames - start, max(1, int(min(seconds, 60) * rate)))
                audio.setpos(start)
                block = audio.readframes(count)
                if len(block) != count * audio.getnchannels() * audio.getsampwidth():
                    raise MediaError("再生する録音が途中で切れています。")
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as output:
                    output.setparams(audio.getparams())
                    output.writeframes(block)
            return buffer.getvalue()
        except (OSError, ValueError, EOFError, wave.Error) as exc:
            raise MediaError("録音の区間を読み取れません。原音は保持しています。") from exc

    def timed_text(self, memo_id):
        meta = self._read(memo_id)
        job_id = meta.get("original_job_id") or meta.get("job_id")
        if not job_id:
            return b""
        if job_id not in {row["job_id"] for row in meta["original_versions"]} | {meta.get("job_id")}:
            raise MediaError("認識原文の出典を確認できません。")
        folder = self.transcriptions.folder(job_id)
        for name in ("request.json", "state.json", "segments.jsonl", "edits.json"):
            _not_link(folder / name)
        request = read_json(folder / "request.json", {})
        if (request.get("audio_memo_id") != memo_id
                or Path(request.get("source_path", "")).resolve() != self.audio_path(memo_id).resolve()):
            raise MediaError("認識原文と録音の対応を確認できません。")
        rows = self.transcriptions.result(job_id)["segments"]
        if not rows:
            return b""
        lines = ["認識原文（元の録音時刻）", "編集した全文との時刻対応を示すものではありません。", ""]
        for row in rows:
            start, end = row.get("start"), row.get("end")
            if (type(start) not in {int, float} or type(end) not in {int, float}
                    or not 0 <= start < end <= meta.get("duration", 0) + 0.001
                    or not math.isfinite(start) or not math.isfinite(end)
                    or not isinstance(row.get("original_text"), str)):
                raise MediaError("認識原文の時刻を確認できません。")
            lines.append(f"[{timestamp(start)} --> {timestamp(end)}] {row['original_text']}")
        return ("\n".join(lines) + "\n").encode("utf-8-sig")

    def txt_path(self, memo_id):
        self._read(memo_id)
        return self._folder(memo_id) / "transcript.txt"

    def save(self, memo_id, revision, title, text):
        title = _title(title)
        if type(revision) is not int or revision < 0:
            raise MediaError("編集の版を確認できません。下書きを控えて再読込してください。")
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS or "\0" in text:
            raise MediaError("メモ本文を確認してください（100万文字以内）。")
        try:
            with self._locked(memo_id):
                meta = self._read(memo_id)
                if type(revision) is not int or revision != meta["revision"]:
                    if type(revision) is int and revision < meta["revision"] and meta.get("edited") and title == meta["title"] and text == meta["text"]:
                        return self._view(meta)  # Identical ACK-lost resubmission.
                    raise MediaError("別の画面または文字起こしで内容が変わりました。下書きを控えて再読込してください。")
                if meta.get("edited") and title == meta["title"] and text == meta["text"]:
                    return self._view(meta)
                meta.update(text_edited=bool(meta.get("text_edited")) or text != meta["text"],
                            title=title, text=text, edited=True, revision=meta["revision"] + 1, updated_at=_now())
                return self._view(self._with_txt(meta))
        except OSError as exc:
            raise MediaError("メモJSONを保存できませんでした。下書きを残して再試行してください。") from exc

    def regenerate_txt(self, memo_id, revision):
        with self._locked(memo_id):
            meta = self._read(memo_id)
            if type(revision) is not int or revision != meta["revision"]:
                raise MediaError("メモが更新されています。最新の内容を再読込してTXTを生成してください。")
            return self._view(self._regenerate(meta))

    def busy(self):
        try:
            return any(row.get("status") in ACTIVE | {"unavailable"} for row in self.transcriptions.history())
        except (OSError, ValueError):
            return True

    def _job(self, meta, job_id):
        try:
            folder = self.transcriptions.folder(job_id)
            for name in ("request.json", "state.json", "segments.jsonl", "edits.json"):
                _not_link(folder / name)
            state = self.transcriptions._state(job_id)
            request = read_json(folder / "request.json", {})
            if (not isinstance(state, dict) or not isinstance(request, dict)
                    or state.get("audio_memo_id") != meta["id"] or request.get("audio_memo_id") != meta["id"]
                    or state.get("id") != job_id or request.get("id") != job_id
                    or state.get("status") not in ACTIVE | {"done", "failed", "cancelled", "interrupted"}
                    or state.get("operation_id") != meta.get("start_operation_id")
                    or Path(request.get("source_path", "")).resolve() != (self._folder(meta["id"]) / "audio.wav").resolve()):
                raise ValueError("different memo operation")
            return state, request
        except (OSError, ValueError, TypeError) as exc:
            raise MediaError("録音メモと文字起こしの対応を確認できません。原音・メモは保持しています。") from exc

    def _find_job(self, meta):
        if meta.get("job_id"):
            self._job(meta, meta["job_id"])
            return meta["job_id"]
        operation = meta.get("start_operation_id")
        if not operation:
            return None
        matches = [row for row in self.transcriptions.history() if row.get("operation_id") == operation and row.get("audio_memo_id") == meta["id"]]
        if len(matches) > 1:
            raise MediaError("同じ開始操作に複数の認識履歴があります。自動で再開せず保存先を確認してください。")
        if matches:
            self._job(meta, matches[0]["id"])
            return matches[0]["id"]
        return None

    def _resume_candidate(self, meta, job_id):
        """Corrupt/missing checkpoints never block a safe, fresh recognition."""
        if not job_id:
            return None, ""
        try:
            folder = self.transcriptions.folder(job_id)
            if not (folder / "checkpoint.json").exists():
                return None, ""
            for name in ("request.json", "checkpoint.json", "segments.jsonl"):
                _not_link(folder / name)
            request = read_json(folder / "request.json", {})
            if (not isinstance(request, dict) or request.get("audio_memo_id") != meta["id"] or request.get("source_sha256") != meta["audio_sha256"]
                    or Path(request.get("source_path", "")).resolve() != self.audio_path(meta["id"]).resolve()):
                raise MediaError("different recording")
            settings = meta.get("start_settings") or {}
            if not isinstance(settings, dict):
                raise MediaError("invalid recognition settings")
            if (request.get("language") != settings.get("language")
                    or request.get("model") != {"standard": "small", "accurate": "medium"}.get(settings.get("mode"))
                    or request.get("hotwords") != settings.get("hotwords") or request.get("device") != "cpu"):
                raise MediaError("different recognition settings")
            checkpoint = load_checkpoint(folder, request)
            checkpoint_prefix(folder / "segments.jsonl", checkpoint)
            return job_id, ""
        except (MediaError, OSError, ValueError, TypeError):
            return None, "完了区間の保存情報を確認できないため、最初から認識します。以前の原文と編集本文は保持しています。"

    def start(self, memo_id, language="ja", mode="standard", hotwords="", retry=False):
        if language not in {"ja", "en", "auto"} or mode not in {"standard", "accurate"} or not isinstance(hotwords, str) or len(hotwords) > 1000 or type(retry) is not bool:
            raise MediaError("文字起こし設定を確認してください。")
        settings = {"language": language, "mode": mode, "hotwords": hotwords.strip()}
        with self._locked(memo_id):
            meta = self._read(memo_id)
            resume_from, resume_warning = self._resume_candidate(meta, meta.get("resume_from")) if meta.get("start_settings") == settings else (None, "")
            existing = self._find_job(meta)
            if existing:
                state, _ = self._job(meta, existing)
                if retry and state.get("status") in ACTIVE | {"unavailable"}:
                    raise MediaError("この録音を文字起こし中です。終了してから再試行してください。")
                if not retry:
                    if meta.get("start_settings") != settings:
                        raise MediaError("開始済み操作と設定が異なります。完了後に再試行してください。")
                    self._sync_locked(meta, existing)
                    return existing
                resume_from, resume_warning = None, ""
                if (meta.get("start_settings") == settings and state.get("status") in {"failed", "cancelled", "interrupted"}
                        and (self.transcriptions.folder(existing) / "checkpoint.json").is_file()):
                    resume_from, resume_warning = self._resume_candidate(meta, existing)
            if retry and not existing and meta.get("status") == "starting":
                raise MediaError("開始結果を確認中です。同じ設定のまま再試行してから、認識履歴を確認してください。")
            if not retry and meta.get("start_operation_id") and not (meta.get("status") == "start_failed" and meta.get("start_settings") != settings):
                if meta.get("start_settings") != settings:
                    raise MediaError("開始確認中の設定と異なります。同じ設定で再試行してください。")
                operation = meta["start_operation_id"]
            else:
                operation = uuid4().hex
            meta.update(start_operation_id=operation, start_settings=settings, job_id=None,
                        resume_from=resume_from, resume_warning=resume_warning,
                        start_reserved_at=time.time(), status="starting", error="", updated_at=_now())
            self._write(meta)
        # Do not hold the memo lock while spawning. A very fast worker may
        # complete and link this reserved operation before start() returns.
        manager = None
        try:
            with self._launch_guard(memo_id):
                with self._locked(memo_id):
                    current = self._read(memo_id)
                    if current.get("start_operation_id") != operation or (current.get("status") != "starting" and not current.get("job_id")):
                        raise MediaError("開始操作が中断または変更されています。最新の履歴を確認してください。")
                    found = self._find_job(current)
                    if found:
                        self._sync_locked(current, found)
                        return found
                manager = TranscriptionSources(self.root)
                audio = self.audio_path(memo_id)
                if file_sha256(audio) != meta["audio_sha256"]:
                    raise MediaError("保存済み原音が変更されています。自動認識せず保存先を確認してください。")
                source = manager.prepare_recording(memo_id, audio)
                options = {"operation_id": operation, "audio_memo_id": memo_id}
                if resume_from is not None:
                    options["resume_from"] = resume_from
                job_id = self.transcriptions.start(source, source.tracks[0].index, language, mode,
                                                  hotwords.strip(), **options)
        except _StartBusy:
            raise
        except (MediaError, OSError) as exc:
            with self._locked(memo_id):
                current = self._read(memo_id)
                if current.get("start_operation_id") == operation:
                    found = self._find_job(current)
                    if found:
                        self._sync_locked(current, found)
                        return found
                    current.update(status="start_failed", error=str(exc), updated_at=_now())
                    self._write(current)
            raise MediaError(str(exc)) from exc
        finally:
            if manager is not None:
                manager.close()
        with self._locked(memo_id):
            current = self._read(memo_id)
            if current.get("start_operation_id") != operation:
                raise MediaError("別の開始操作へ切り替わっています。認識履歴を確認してください。")
            self._sync_locked(current, job_id)
        return job_id

    def _sync_locked(self, meta, job_id):
        state, _ = self._job(meta, job_id)
        if meta.get("job_id") not in {None, job_id}:
            return meta
        before = deepcopy(meta)
        meta.update(job_id=job_id, status=state.get("status", "unavailable"), error=state.get("error", ""))
        terminal = meta["status"] not in ACTIVE | {"unavailable"}
        text_changed = False
        if terminal and not any(row.get("job_id") == job_id for row in meta["original_versions"]):
            result = self.transcriptions.result(job_id)
            raw = "\n".join(str(row["original_text"]) for row in result["segments"])
            meta["latest_result_empty"] = not bool(raw.strip())
            meta["original_versions"].append({"job_id": job_id, "status": meta["status"], "text": raw, "saved_at": _now()})
            if raw.strip() and (meta["status"] == "done" or meta.get("original_status") != "done"):
                meta.update(original_text=raw, original_job_id=job_id, original_status=meta["status"])
                if not meta.get("text_edited") and meta["text"] != raw:
                    meta.update(text=raw, revision=meta["revision"] + 1)
                    text_changed = True
        if meta != before:
            meta["updated_at"] = _now()
            if text_changed:
                return self._with_txt(meta)
            self._write(meta)
        return meta

    def sync(self, memo_id):
        try:
            with self._locked(memo_id):
                meta = self._read(memo_id)
                job_id = self._find_job(meta)
                if job_id:
                    meta = self._sync_locked(meta, job_id)
                elif meta.get("status") == "starting" and time.time() - meta.get("start_reserved_at", 0) > 45:
                    try:
                        with self._launch_guard(memo_id):
                            job_id = self._find_job(meta)
                            if job_id:
                                meta = self._sync_locked(meta, job_id)
                            else:
                                meta.update(status="interrupted", error="開始した認識処理を確認できません。同じ設定で再試行できます。", updated_at=_now())
                                self._write(meta)
                    except _StartBusy:
                        pass  # A live launcher still owns this reservation.
                return self._view(meta)
        except OSError as exc:
            raise MediaError("認識結果をメモへ保存できませんでした。原音と認識履歴は保持しています。再試行してください。") from exc

    def cancel(self, memo_id):
        with self._locked(memo_id):
            meta = self._read(memo_id)
            job_id = self._find_job(meta)
            if job_id:
                self.transcriptions.cancel(job_id)
            elif meta.get("status") == "starting":
                with self._launch_guard(memo_id):
                    job_id = self._find_job(meta)
                    if job_id:
                        self.transcriptions.cancel(job_id)
                    else:
                        meta.update(start_operation_id=None, start_settings=None, status="interrupted",
                                    error="開始結果の確認を中止しました。原音から再試行できます。", updated_at=_now())
                        self._write(meta)
        return self.sync(memo_id)

    def delete(self, memo_id, revision):
        folder = self._folder(memo_id)
        if not folder.exists():
            return False
        with self._locked(memo_id):
            meta = self._read(memo_id)
            if type(revision) is not int or revision != meta["revision"]:
                raise MediaError("メモが更新されています。最新の内容を確認して削除してください。")
            job_id = self._find_job(meta)
            if job_id and self._job(meta, job_id)[0].get("status") in ACTIVE | {"unavailable"}:
                raise MediaError("文字起こし中の録音は削除できません。先に中止し、終了を確認してください。")
            if not job_id and meta.get("status") == "starting":
                raise MediaError("開始処理を確認中です。結果を確認してから削除してください。")
            # Inspect every existing child before recursive deletion. Never
            # follow a substituted link or broaden this dedicated ID boundary.
            target = self._folder(memo_id).resolve()
            if target.parent != self.base.resolve() or target.name != memo_id:
                raise MediaError("録音メモの削除先を確認できません。")
            for parent, directories, files in os.walk(target, followlinks=False):
                for name in (*directories, *files):
                    _not_link(Path(parent) / name)
            deleted = self.base / ".deleted"
            deleted.mkdir(exist_ok=True)
            atomic_json(deleted / (memo_id + ".json"), {"id": memo_id, "deleted_at": _now()})
            shutil.rmtree(target)
            return True


def sync_audio_memo(root, memo_id, job_id):
    """Worker terminal hook, including the before-start-return linking race."""
    store = AudioMemoStore(root)
    with store._locked(memo_id):
        meta = store._read(memo_id)
        # Late callbacks from an older retry must not adopt or alter the new job.
        folder = store.transcriptions.folder(job_id)
        _not_link(folder / "state.json")
        state = read_json(folder / "state.json", {})
        if (state.get("audio_memo_id") == memo_id
                and state.get("operation_id") != meta.get("start_operation_id")):
            return store._view(meta)
        return store._view(store._sync_locked(meta, job_id))
