"""Local FFmpeg jobs, independent of Streamlit reruns and personal JSON data."""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

UPLOAD_LIMIT = 200 * 1024 * 1024
PREVIEW_LIMIT = 64 * 1024 * 1024
BITRATES = (128, 192, 320)
ACTIVE = {"starting", "running", "cancelling"}


class MediaError(ValueError):
    """An actionable message suitable for the local conversion page."""


@dataclass(frozen=True)
class Tools:
    ffmpeg: Path
    ffprobe: Path


def discover_tools(root: Path) -> Tools:
    """Prefer an explicitly bundled pair, then an optional directory, then PATH."""
    name = lambda value: value + (".exe" if os.name == "nt" else "")
    directories = [Path(root) / "tools/ffmpeg/bin", Path(root) / "tools/ffmpeg"]
    configured = os.environ.get("PROLOAG_FFMPEG_DIR", "").strip()
    if configured:
        directories += [Path(configured), Path(configured) / "bin"]
    for directory in directories:
        pair = [directory / name(value) for value in ("ffmpeg", "ffprobe")]
        if all(path.is_file() for path in pair):
            return Tools(*(path.resolve() for path in pair))
    pair = [shutil.which(value) for value in ("ffmpeg", "ffprobe")]
    if all(pair):
        return Tools(*(Path(value).resolve() for value in pair))
    raise MediaError(
        "FFmpegとFFprobeが見つかりません。ページ下部のセットアップ案内を確認してください。"
    )


def _process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _finite_duration(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _probe(tools: Tools, path: Path) -> dict:
    command = [
        str(tools.ffprobe),
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        "-i",
        str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=30, check=False, **_process_options()
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            "動画の確認が時間内に完了しませんでした。ファイルの状態を確認してください。"
        ) from exc
    except OSError as exc:
        raise MediaError(
            "FFprobeを起動できません。実行ファイルの配置を確認してください。"
        ) from exc
    if result.returncode:
        raise MediaError(
            "ファイルを読み取れません。破損していないMP4を選択してください。"
        )
    try:
        data = json.loads(result.stdout.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeError) as exc:
        raise MediaError("メディア情報を取得できませんでした。") from exc


def _identity(path: Path) -> tuple:
    try:
        stat = path.stat()
        if not path.is_file():
            raise OSError
        return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino
    except OSError as exc:
        raise MediaError(
            "動画が見つからないか、読み取りできません。場所を確認してください。"
        ) from exc


def mp3_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaError("保存ファイル名を入力してください。")
    value = value.strip()
    if (
        len(value) > 180
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
        or value.endswith((".", " "))
    ):
        raise MediaError("保存ファイル名に使えない文字、または長すぎる名前です。")
    if not value.lower().endswith(".mp3"):
        value += ".mp3"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", value):
        raise MediaError("別の保存ファイル名を指定してください。")
    return value


def default_name(name: str) -> str:
    stem = Path(name.replace("\\", "/")).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:130] or "audio"
    return mp3_name(stem + "_audio.mp3")


@dataclass(frozen=True)
class Track:
    index: int
    codec: str
    language: str
    title: str
    channels: int
    duration: float | None


@dataclass(frozen=True)
class Source:
    id: str
    path: Path
    name: str
    mode: str
    identity: tuple
    duration: float | None
    tracks: tuple[Track, ...]


@dataclass
class Job:
    id: str
    operation_id: str
    fingerprint: tuple
    folder: Path
    source: Source
    track: Track
    bitrate: int
    started: float = field(default_factory=time.monotonic)
    status: str = "starting"
    progress: float = 0.0
    elapsed: float = 0.0
    size: int = 0
    duration: float | None = None
    error: str = ""
    details: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None


@dataclass
class Work:
    folder: Path
    source: Source
    job: Job | None = None
    touched: float = field(default_factory=time.monotonic)


def conversion_command(
    tools: Tools, source: Source, track: Track, bitrate: int, output: Path
) -> list[str]:
    return [
        str(tools.ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-protocol_whitelist",
        "file,pipe",
        "-threads",
        "2",
        "-i",
        str(source.path),
        "-map",
        f"0:{track.index}",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{bitrate}k",
        "-ac",
        "1" if track.channels == 1 else "2",
        "-threads",
        "2",
        "-progress",
        "pipe:1",
        "-stats_period",
        "0.3",
        "-n",
        str(output),
    ]


def _tail(path: Path, size=12000) -> str:
    try:
        with path.open("rb") as source:
            source.seek(max(0, path.stat().st_size - size))
            return source.read(size).decode("utf-8", errors="replace")
    except OSError:
        return ""


class MediaManager:
    """One active conversion across sessions; session ownership isolates results.

    Temp storage is created only by explicit preparation. Cleanup touches only
    directories created by this manager. Local source videos are never copied,
    overwritten, or deleted. No Streamlit calls run on worker threads.
    """

    def __init__(
        self, root: Path, *, tools: Tools | None = None, timeout=21600, ttl=86400
    ):
        self.root = Path(root).resolve()
        self.tools = tools
        self.timeout = timeout
        self.ttl = ttl
        self._base: Path | None = None
        self._works: dict[str, Work] = {}
        self._preparing: set[str] = set()
        self._lock = threading.RLock()
        self._encoder_checked = False
        self._closed = False
        atexit.register(self.close)

    def _owner(self, owner):
        if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{32}", owner):
            raise MediaError("画面を開き直してください。処理の識別情報が不正です。")
        if self._closed:
            raise MediaError("変換機能を再読み込みしてください。")

    def _tools(self):
        if self.tools is None:
            self.tools = discover_tools(self.root)
        return self.tools

    def _folder(self):
        if self._base is None:
            self._base = Path(tempfile.mkdtemp(prefix="proloag-media-")).resolve()
        else:
            self._base.mkdir(exist_ok=True)
        folder = self._base / uuid4().hex
        folder.mkdir()
        return folder

    def _remove(self, folder):
        assert (
            self._base
            and folder.resolve().is_relative_to(self._base)
            and folder.resolve() != self._base
        )
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=False)

    def prune(self):
        with self._lock:
            now = time.monotonic()
            for owner, work in list(self._works.items()):
                if (
                    not work.job or work.job.status not in ACTIVE
                ) and now - work.touched > self.ttl:
                    try:
                        self._remove(work.folder)
                    except OSError:
                        continue
                    del self._works[owner]

    def _busy(self, owner):
        work = self._works.get(owner)
        return owner in self._preparing or bool(
            work and work.job and work.job.status in ACTIVE
        )

    def _source(self, path: Path, name: str, mode: str) -> Source:
        before = _identity(path)
        if before[0] == 0:
            raise MediaError("空のファイルです。音声のあるMP4を選択してください。")
        data = _probe(self._tools(), path)
        if not {"mov", "mp4"}.intersection(
            str(data.get("format", {}).get("format_name", "")).split(",")
        ):
            raise MediaError("MP4形式として読み取れません。MP4動画を選択してください。")
        tracks = []
        for row in data.get("streams", []):
            if row.get("codec_type") != "audio":
                continue
            index = row.get("index")
            if type(index) is not int or index < 0:
                raise MediaError("音声トラック情報が不正です。")
            tags = row.get("tags", {})
            channels = row.get("channels", 0)
            tracks.append(
                Track(
                    index,
                    str(row.get("codec_name", ""))[:40],
                    str(tags.get("language", ""))[:80],
                    str(tags.get("title", ""))[:200],
                    channels if type(channels) is int else 0,
                    _finite_duration(row.get("duration")),
                )
            )
        if not tracks:
            raise MediaError(
                "この動画には音声トラックがありません。音声のある動画を選択してください。"
            )
        if len(tracks) > 256 or len({track.index for track in tracks}) != len(tracks):
            raise MediaError("音声トラックの数・情報が不正です。")
        if before != _identity(path):
            raise MediaError("確認中に動画が変更されました。もう一度確認してください。")
        return Source(
            uuid4().hex,
            path,
            name,
            mode,
            before,
            _finite_duration(data.get("format", {}).get("duration")),
            tuple(tracks),
        )

    def prepare_local(self, owner: str, value: str) -> Source:
        value = str(value).strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        path = Path(value).expanduser()
        if (
            not path.is_absolute()
            or path.suffix.lower() != ".mp4"
            or str(path).startswith(("\\\\", "//"))
        ):
            raise MediaError("PC内のMP4ファイルのフルパスを指定してください。")
        try:
            path = path.resolve(strict=True)
        except (OSError, ValueError, RuntimeError) as exc:
            raise MediaError(
                "動画が見つかりません。ファイルの場所を確認してください。"
            ) from exc
        return self._prepare(owner, path=path, name=path.name)

    def prepare_upload(self, owner: str, name: str, buffer) -> Source:
        if not isinstance(name, str) or Path(name).suffix.lower() != ".mp4":
            raise MediaError("MP4ファイルを選択してください。")
        if (
            not isinstance(buffer, (bytes, bytearray, memoryview))
            or not 0 < len(buffer) <= UPLOAD_LIMIT
        ):
            raise MediaError(
                "アップロードは空でない200MB以内のファイルに対応します。大容量はPC内のファイルを直接指定してください。"
            )
        return self._prepare(
            owner, buffer=buffer, name=name.replace("\\", "/").split("/")[-1]
        )

    def _prepare(self, owner, *, path=None, buffer=None, name):
        with self._lock:
            self._owner(owner)
            if self._busy(owner):
                raise MediaError(
                    "変換中です。中止または完了後に動画を切り替えてください。"
                )
            self._tools()
            folder = self._folder()
            self._preparing.add(owner)
        try:
            if path is None:
                path = folder / "input.mp4"
                with path.open("xb") as output:
                    output.write(buffer)
            source = self._source(
                path, name, "upload" if buffer is not None else "local"
            )
            with self._lock:
                self._owner(owner)
                old = self._works.get(owner)
                if old:
                    self._remove(old.folder)
                self._works[owner] = Work(folder, source)
                return source
        except BaseException:
            if folder.exists():
                self._remove(folder)
            raise
        finally:
            with self._lock:
                self._preparing.discard(owner)

    def _check_encoder(self):
        if self._encoder_checked:
            return
        try:
            result = subprocess.run(
                [str(self._tools().ffmpeg), "-hide_banner", "-encoders"],
                capture_output=True,
                timeout=10,
                check=False,
                **_process_options(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError(
                "FFmpegを起動できません。実行ファイルの配置を確認してください。"
            ) from exc
        if result.returncode or b"libmp3lame" not in result.stdout:
            raise MediaError("MP3エンコーダー（libmp3lame）を含むFFmpegが必要です。")
        self._encoder_checked = True

    def start(self, owner, source_id, track_index, bitrate, *, operation_id):
        with self._lock:
            self._owner(owner)
            if (
                type(bitrate) is not int
                or bitrate not in BITRATES
                or type(track_index) is not int
            ):
                raise MediaError("音質と音声トラックを選び直してください。")
            if not isinstance(operation_id, str) or not re.fullmatch(
                r"[0-9a-f]{32}", operation_id
            ):
                raise MediaError("処理の識別情報が不正です。")
            work = self._works.get(owner)
            if not work or work.source.id != source_id:
                raise MediaError("対象動画が変わりました。動画を確認し直してください。")
            fingerprint = (source_id, track_index, bitrate)
            if work.job and work.job.operation_id == operation_id:
                if work.job.fingerprint != fingerprint:
                    raise MediaError("同じ操作に異なる変換条件が指定されました。")
                return work.job.id
            if self._preparing or any(self._busy(key) for key in self._works):
                raise MediaError(
                    "別の変換が実行中です。完了または中止後に開始できます。"
                )
            if work.source.identity != _identity(work.source.path):
                raise MediaError(
                    "確認後に動画が変更されました。もう一度「動画を確認」を押してください。"
                )
            track = next(
                (row for row in work.source.tracks if row.index == track_index), None
            )
            if track is None:
                raise MediaError("選択した音声トラックが見つかりません。")
            self._check_encoder()
            duration = track.duration or work.source.duration
            if (
                duration
                and shutil.disk_usage(work.folder).free
                < duration * bitrate * 1000 / 8 + 32 * 1024 * 1024
            ):
                raise MediaError(
                    "一時保存先の空き容量が不足しています。空きを確保してから再試行してください。"
                )
            folder = work.folder / uuid4().hex
            folder.mkdir()
            job = Job(
                uuid4().hex,
                operation_id,
                fingerprint,
                folder,
                work.source,
                track,
                bitrate,
            )
            if work.job:
                shutil.rmtree(work.job.folder)
            work.job = job
            work.touched = time.monotonic()
            job.thread = threading.Thread(
                target=self._worker,
                args=(owner, work, job),
                name="proloag-media-" + job.id[:8],
                daemon=True,
            )
            try:
                job.thread.start()
            except Exception:
                job.status = "failed"
                job.error = "変換処理を開始できませんでした。もう一度試してください。"
                raise MediaError(job.error)
            return job.id

    def _worker(self, owner, work, job):
        partial, output = job.folder / "partial.mp3", job.folder / "audio.mp3"
        progress_file, errors_file = (
            job.folder / "progress.txt",
            job.folder / "errors.txt",
        )
        process = None
        try:
            if job.cancel.is_set():
                return
            with progress_file.open("wb") as progress, errors_file.open("wb") as errors:
                process = subprocess.Popen(
                    conversion_command(
                        self._tools(), job.source, job.track, job.bitrate, partial
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=progress,
                    stderr=errors,
                    **_process_options(),
                )
                with self._lock:
                    job.process = process
                    job.status = "cancelling" if job.cancel.is_set() else "running"
                while process.poll() is None:
                    elapsed = time.monotonic() - job.started
                    if job.cancel.is_set() or elapsed > self.timeout:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                        if not job.cancel.is_set():
                            raise MediaError(
                                "変換が時間内に完了しませんでした。対象動画を見直して再試行してください。"
                            )
                        break
                    matches = re.findall(
                        r"(?:^|\n)out_time_us=(\d+)", _tail(progress_file)
                    )
                    duration = job.track.duration or job.source.duration
                    with self._lock:
                        job.elapsed = elapsed
                        if matches and duration:
                            job.progress = max(
                                job.progress,
                                min(0.99, int(matches[-1]) / 1_000_000 / duration),
                            )
                    job.cancel.wait(0.2)
            if job.cancel.is_set():
                return
            if (
                process.returncode != 0
                or not partial.is_file()
                or not partial.stat().st_size
            ):
                raise MediaError(
                    "MP3への変換に失敗しました。別の音声トラックや動画で再試行できます。"
                )
            if job.source.identity != _identity(job.source.path):
                raise MediaError(
                    "変換中に元動画が変更されました。動画を確認し直してください。"
                )
            data = _probe(self._tools(), partial)
            if "mp3" not in str(data.get("format", {}).get("format_name", "")).split(
                ","
            ) or not any(
                row.get("codec_type") == "audio" for row in data.get("streams", [])
            ):
                raise MediaError(
                    "変換結果の音声を確認できませんでした。もう一度試してください。"
                )
            with self._lock:
                if not job.cancel.is_set():
                    os.replace(partial, output)
                    job.size = output.stat().st_size
                    job.duration = _finite_duration(
                        data.get("format", {}).get("duration")
                    )
                    job.progress = 1.0
                    job.status = "done"
        except Exception as exc:
            with self._lock:
                job.status = "failed"
                job.error = (
                    str(exc)
                    if isinstance(exc, MediaError)
                    else "変換処理を完了できませんでした。保存容量やファイルの状態を確認してください。"
                )
                job.details = _tail(errors_file, 6000)
        finally:
            cleanup_error = False
            try:
                if process and process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                partial.unlink(missing_ok=True)
                if job.cancel.is_set():
                    output.unlink(missing_ok=True)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_error = True
            with self._lock:
                if job.cancel.is_set():
                    job.status = "cancelled"
                    job.size = 0
                elif job.status in ACTIVE:
                    job.status = "failed"
                    job.error = (
                        "変換処理を完了できませんでした。もう一度試してください。"
                    )
                if cleanup_error:
                    job.details += "\n一時ファイルを片付けられませんでした。処理終了後に結果のクリアを試してください。"
                job.elapsed = time.monotonic() - job.started
                job.process = None
                work.touched = time.monotonic()

    def snapshot(self, owner):
        with self._lock:
            self._owner(owner)
            work = self._works.get(owner)
            if not work:
                return {"source": None, "job": None}
            work.touched = time.monotonic()
            job = work.job
            view = (
                None
                if job is None
                else {
                    key: getattr(job, key)
                    for key in (
                        "id",
                        "status",
                        "progress",
                        "elapsed",
                        "bitrate",
                        "size",
                        "duration",
                        "error",
                        "details",
                        "fingerprint",
                    )
                }
            )
            return {"source": work.source, "job": view}

    def cancel(self, owner):
        with self._lock:
            self._owner(owner)
            work = self._works.get(owner)
            if not work or not work.job or work.job.status not in ACTIVE:
                return False
            work.job.cancel.set()
            work.job.status = "cancelling"
            return True

    def _result(self, owner, job_id):
        self._owner(owner)
        work = self._works.get(owner)
        if (
            not work
            or not work.job
            or work.job.id != job_id
            or work.job.status != "done"
        ):
            raise MediaError("変換結果が見つかりません。画面を更新してください。")
        work.touched = time.monotonic()
        output = work.job.folder / "audio.mp3"
        if not output.is_file():
            raise MediaError(
                "一時ファイルが見つかりません。動画を確認し直してください。"
            )
        return output

    def result_bytes(self, owner, job_id, limit=PREVIEW_LIMIT):
        with self._lock:
            output = self._result(owner, job_id)
            if output.stat().st_size > limit:
                raise MediaError("大きな変換結果は「PCへ直接保存」をご利用ください。")
            return output.read_bytes()

    def save_result(self, owner, job_id, directory, name):
        name = mp3_name(name)
        folder = Path(str(directory).strip().strip('"')).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise MediaError("保存先に既存フォルダーのフルパスを指定してください。")
        folder = folder.resolve()
        if self._base and folder.is_relative_to(self._base):
            raise MediaError("一時フォルダー以外の保存先を指定してください。")
        target = folder / name
        with self._lock:
            output = self._result(owner, job_id)
            created = None
            try:
                with target.open("xb") as destination:
                    created = os.fstat(destination.fileno())
                    with output.open("rb") as source:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
            except FileExistsError as exc:
                raise MediaError(
                    "同じ名前のファイルがあります。別のファイル名を指定してください。"
                ) from exc
            except OSError as exc:
                if created and target.exists():
                    current = target.stat()
                    if (current.st_dev, current.st_ino) == (
                        created.st_dev,
                        created.st_ino,
                    ):
                        target.unlink()
                raise MediaError(
                    "保存できません。フォルダーの書き込み権限と空き容量を確認してください。"
                ) from exc
            return target

    def clear(self, owner):
        with self._lock:
            self._owner(owner)
            if self._busy(owner):
                raise MediaError("変換を中止してから一時ファイルをクリアしてください。")
            work = self._works.get(owner)
            if work:
                self._remove(work.folder)
                del self._works[owner]

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = [work.job for work in self._works.values() if work.job]
            for job in jobs:
                if job.status in ACTIVE:
                    job.cancel.set()
        for job in jobs:
            if job.thread and job.thread is not threading.current_thread():
                job.thread.join(timeout=5)
        with self._lock:
            if self._base and not any(
                job.thread and job.thread.is_alive() for job in jobs
            ):
                shutil.rmtree(self._base, ignore_errors=True)
            self._works.clear()
