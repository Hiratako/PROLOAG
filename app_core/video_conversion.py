"""Explicit, background MKV-to-MP4 jobs with destination-first publication.

The input is never rewritten. Only a verified output is published, without
replacing an existing path. This module contains no Streamlit or record I/O.
"""
from __future__ import annotations

import atexit
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

from app_core.matroska import inspect_matroska
from app_core.media_conversion import (
    ACTIVE, UPLOAD_LIMIT, MediaError, Tools, _finite_duration, _identity,
    _probe, _process_options, _tail, acquire_conversion, discover_tools,
    release_conversion,
)


MODES = ("copy", "compatible")
_OWNER = re.compile(r"[0-9a-f]{32}")
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_UNKNOWN = {"", "unknown", "unspecified", "reserved"}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _integer(value, default=0):
    return value if type(value) is int and value >= 0 else default


def _text(value, limit=200):
    return str(value or "").replace("\x00", "")[:limit]


def _duration(row):
    direct = _finite_duration(row.get("duration"))
    if direct is not None:
        return direct
    tags = row.get("tags") or {}
    value = tags.get("DURATION", tags.get("duration", "")) if isinstance(tags, dict) else ""
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", str(value))
    if match:
        hours, minutes, seconds = map(float, match.groups())
        if minutes < 60 and seconds < 60:
            # Matroska DURATION can denote the absolute end timestamp.
            return _finite_duration(hours * 3600 + minutes * 60 + seconds - (_number(row.get("start_time")) or 0.0))
    return None


def _packet_start(tools, path, index):
    """Inspect a bounded packet sample; stream.start_time can misreport PCM."""
    try:
        result = subprocess.run([
            str(tools.ffprobe), "-v", "error", "-protocol_whitelist", "file,pipe",
            "-select_streams", str(index), "-show_packets", "-show_entries",
            "packet=pts_time,dts_time", "-read_intervals", "%+#8", "-of", "json", "-i", str(path),
        ], capture_output=True, timeout=10, check=False, **_process_options())
        if result.returncode:
            return None
        rows = json.loads(result.stdout.decode("utf-8")).get("packets", [])
        if not isinstance(rows, list) or len(rows) > 64:
            return None
        values = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            pts = _number(row.get("pts_time"))
            value = pts if pts is not None else _number(row.get("dts_time"))
            if value is not None:
                values.append(value)
        return min(values) if values else None
    except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError, TypeError, AttributeError):
        return None


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _network(path):
    value = str(path)
    return value.startswith("//") or value.startswith("\\\\?\\UNC\\") or (
        value.startswith("\\\\") and not value.startswith("\\\\?\\")
    )


def _folder_identity(path):
    try:
        if _linked(path) or not path.is_dir():
            raise OSError
        info = path.stat()
        return info.st_dev, info.st_ino
    except (OSError, ValueError) as exc:
        raise MediaError("保存先フォルダーが変更されたか利用できません。既存のPC内フォルダーを選び直してください。") from exc


def video_name(value):
    if not isinstance(value, str) or not value.strip():
        raise MediaError("MP4の保存ファイル名を入力してください。")
    value = value.strip()
    if (len(value) > 180 or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or value.endswith((".", " "))):
        raise MediaError("保存名に使えない文字があります。フォルダーを含めず、短いファイル名を入力してください。")
    if not value.lower().endswith(".mp4"):
        value += ".mp4"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", value):
        raise MediaError("Windowsの予約名です。別のMP4ファイル名を入力してください。")
    return value


def default_video_name(name):
    stem = Path(str(name).replace("\\", "/")).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:130] or "video"
    try:
        return video_name(stem + ".mp4")
    except MediaError:
        return video_name("video_" + stem + ".mp4")


@dataclass(frozen=True)
class VideoTrack:
    index: int
    codec: str
    language: str
    title: str
    duration: float | None
    start_time: float | None
    width: int
    height: int
    pix_fmt: str
    profile: str
    bit_depth: int
    color_transfer: str
    color_primaries: str
    color_space: str
    hdr: bool
    dolby_vision: bool
    rotation: float = 0.0
    field_order: str = ""
    dynamic_hdr: bool = False
    duration_from_tag: bool = False


@dataclass(frozen=True)
class AudioTrack:
    index: int
    codec: str
    language: str
    title: str
    duration: float | None
    start_time: float | None
    channels: int
    sample_rate: int
    profile: str
    default: bool = False
    duration_from_tag: bool = False


@dataclass(frozen=True)
class SubtitleTrack:
    index: int
    codec: str
    language: str
    title: str


@dataclass(frozen=True)
class Source:
    id: str
    path: Path
    name: str
    mode: str
    identity: tuple
    duration: float | None
    start_time: float | None
    videos: tuple[VideoTrack, ...]
    audios: tuple[AudioTrack, ...]
    subtitles: tuple[SubtitleTrack, ...]
    other_streams: tuple[str, ...]
    chapter_count: int


@dataclass(frozen=True)
class Plan:
    id: str
    source_id: str
    video_index: int
    audio_index: int | None
    subtitle_index: int | None
    mode: str
    target: Path
    video_codec: str
    audio_codec: str | None
    subtitle_codec: str | None
    estimated_bytes: int
    warnings: tuple[str, ...]
    dropped_tracks: tuple[str, ...]
    timeline_notes: tuple[str, ...]
    directory_identity: tuple
    expected_duration: float | None = None
    expected_audio_offset: float | None = None
    audio_channels: int | None = None


@dataclass
class Job:
    id: str
    operation_id: str
    plan: Plan
    source: Source
    folder: Path
    started: float = field(default_factory=time.monotonic)
    status: str = "starting"
    progress: float = 0.0
    elapsed: float = 0.0
    size: int = 0
    duration: float | None = None
    output_path: Path | None = None
    error: str = ""
    details: str = ""
    warnings: tuple[str, ...] = ()
    cancel: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    output_temp: Path | None = None
    output_temp_identity: tuple | None = None


@dataclass
class Work:
    folder: Path
    source: Source
    plan: Plan | None = None
    job: Job | None = None
    operations: dict[str, tuple[str, str]] = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)


def _track(tracks, index, label, *, optional=False):
    if optional and index is None:
        return None
    if type(index) is not int:
        raise MediaError(f"{label}トラックを選び直してください。")
    result = next((row for row in tracks if row.index == index), None)
    if result is None:
        raise MediaError(f"選択した{label}トラックがありません。MKVを確認し直してください。")
    return result


def _destination(directory, name, base=None):
    name = video_name(name)
    folder = Path(str(directory).strip().strip('"')).expanduser()
    if not folder.is_absolute() or _network(folder):
        raise MediaError("保存先に、PC内の既存フォルダーのフルパスを指定してください。")
    try:
        folder = folder.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise MediaError("保存先が見つかりません。既存のPC内フォルダーを選んでください。") from exc
    identity = _folder_identity(folder)
    if _network(folder) or base is not None and folder.is_relative_to(base):
        raise MediaError("変換用の一時領域やネットワーク以外の、PC内フォルダーを指定してください。")
    target = folder / name
    if target.exists() or target.is_symlink():
        raise MediaError("同じ保存名が既にあります。上書きせず、別のMP4ファイル名を指定してください。")
    return target, identity


def _check_destination(plan):
    if _folder_identity(plan.target.parent) != plan.directory_identity:
        raise MediaError("計画確認後に保存先フォルダーが変わりました。保存先を選び直して計画を確認してください。")
    if plan.target.exists() or plan.target.is_symlink():
        raise MediaError("同じ保存名が使われました。既存ファイルは変更せず停止しました。別の名前で計画を作り直してください。")


def _publish_no_replace(partial: Path, target: Path):
    """Publish atomically on the same filesystem without a replace fallback."""
    if os.name == "nt":
        # Unlike POSIX rename, Windows rename fails if the destination exists.
        os.rename(partial, target)
    else:
        os.link(partial, target)
        # Once link creation succeeded the result is committed. A cleanup
        # failure must not misreport publication as a failed conversion.
        try:
            partial.unlink()
        except OSError:
            pass


def conversion_command(tools, source, plan, output):
    video = _track(source.videos, plan.video_index, "映像")
    audio = _track(source.audios, plan.audio_index, "音声", optional=True)
    subtitle = _track(source.subtitles, plan.subtitle_index, "字幕", optional=True)
    command = [str(tools.ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
               "-protocol_whitelist", "file,pipe", "-threads", "2", "-copyts", "-start_at_zero",
               "-i", str(source.path), "-map", f"0:{video.index}"]
    if audio:
        command += ["-map", f"0:{audio.index}"]
    if subtitle:
        command += ["-map", f"0:{subtitle.index}"]
    command += ["-map_metadata", "-1", "-map_chapters", "-1", "-dn", "-c:v",
                "copy" if plan.mode == "copy" else "libx264"]
    if plan.mode == "compatible":
        command += ["-preset", "medium", "-crf", "20", "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p", "-pix_fmt", "yuv420p"]
    if video.codec == "hevc" and plan.mode == "copy":
        command += ["-tag:v:0", "hvc1"]
    command += ["-disposition:v:0", "default"]
    if audio:
        command += ["-c:a", plan.audio_codec]
        if plan.audio_codec != "copy":
            command += ["-b:a", "192k", "-ac", str(plan.audio_channels)]
        language = audio.language if re.fullmatch(r"[A-Za-z]{3}", audio.language) else "und"
        command += ["-metadata:s:a:0", "language=" + language, "-disposition:a:0", "default"]
    else:
        command += ["-an"]
    if subtitle:
        language = subtitle.language if re.fullmatch(r"[A-Za-z]{3}", subtitle.language) else "und"
        command += ["-c:s", "mov_text", "-metadata:s:s:0", "language=" + language, "-disposition:s:0", "0"]
    else:
        command += ["-sn"]
    command += ["-threads", "2", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
                "-progress", "pipe:1", "-stats_period", "0.3", "-f", "mp4", "-n", str(output)]
    return command


class VideoManager:
    """One explicit job per owner, sharing the MP3 conversion execution gate."""

    def __init__(self, root: Path, *, tools: Tools | None = None, timeout=21600, ttl=86400):
        self.root = Path(root).resolve()
        self.tools = tools
        self.timeout, self.ttl = timeout, ttl
        self._base: Path | None = None
        self._works: dict[str, Work] = {}
        self._preparing: set[str] = set()
        self._lock = threading.RLock()
        self._encoders: set[str] | None = None
        self._closed = False
        atexit.register(self.close)

    def _owner(self, owner):
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
            raise MediaError("画面を開き直してください。変換の識別情報が不正です。")
        if self._closed:
            raise MediaError("動画変換を再読み込みしてください。")

    def _tools(self):
        if self.tools is None:
            self.tools = discover_tools(self.root)
        return self.tools

    def _folder(self):
        if self._base is None:
            self._base = Path(tempfile.mkdtemp(prefix="proloag-video-")).resolve()
        else:
            self._base.mkdir(exist_ok=True)
        folder = self._base / uuid4().hex
        folder.mkdir()
        return folder

    def _remove(self, folder):
        if (not self._base or folder.parent != self._base or _linked(folder)
                or folder.resolve().parent != self._base):
            raise MediaError("一時領域を安全に確認できません。フォルダーは削除していません。")
        shutil.rmtree(folder)

    def _busy(self, owner):
        work = self._works.get(owner)
        return owner in self._preparing or bool(work and work.job and work.job.status in ACTIVE)

    def _source(self, path, name, mode):
        before = _identity(path)
        if not before[0]:
            raise MediaError("空のMKVです。再生できる動画ファイルを選んでください。")
        try:
            data = _probe(self._tools(), path, include_chapters=True)
        except MediaError as exc:
            raise MediaError("MKVの情報を読み取れません。再生できるMKVか確認し、別ファイルでもお試しください。") from exc
        inspect_matroska(path, data)
        streams = data.get("streams")
        if not isinstance(streams, list) or len(streams) > 512:
            raise MediaError("MKVのトラック情報が不正または多すぎます。トラックを整理した動画を選んでください。")
        videos, audios, subtitles, others, seen = [], [], [], [], set()
        for row in streams:
            if not isinstance(row, dict):
                raise MediaError("MKVのトラック情報を確認できません。別のファイルを選んでください。")
            index = row.get("index")
            if type(index) is not int or index < 0 or index in seen:
                raise MediaError("MKVのトラック番号が不正です。別のファイルを選んでください。")
            seen.add(index)
            tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
            disposition = row.get("disposition") if isinstance(row.get("disposition"), dict) else {}
            side = row.get("side_data_list") if isinstance(row.get("side_data_list"), list) else []
            kind, codec = row.get("codec_type"), _text(row.get("codec_name"), 40)
            language, title = _text(tags.get("language"), 80), _text(tags.get("title"))
            if kind == "video" and not disposition.get("attached_pic"):
                side_types = " ".join(_text(item.get("side_data_type"), 160).lower() for item in side if isinstance(item, dict))
                rotation = next((_number(item.get("rotation")) for item in side
                                 if isinstance(item, dict) and item.get("rotation") is not None), None)
                rotation = rotation if rotation is not None else _number(tags.get("rotate")) or 0.0
                pix_fmt = _text(row.get("pix_fmt"), 40)
                bits = _number(row.get("bits_per_raw_sample"))
                if not bits:
                    match = re.search(r"(?:p|gray)(9|10|12|14|16)(?:le|be)$", pix_fmt)
                    bits = int(match.group(1)) if match else 8
                transfer = _text(row.get("color_transfer"), 40)
                videos.append(VideoTrack(index, codec, language, title, _duration(row), _number(row.get("start_time")),
                    _integer(row.get("width")), _integer(row.get("height")), pix_fmt, _text(row.get("profile"), 80),
                    int(bits), transfer, _text(row.get("color_primaries"), 40), _text(row.get("color_space"), 40),
                    transfer in _HDR_TRANSFERS or "mastering display" in side_types or "content light" in side_types,
                    "dovi" in side_types or "dolby vision" in side_types or _text(row.get("codec_tag_string")).lower() in {"dvh1", "dvhe"},
                    rotation, _text(row.get("field_order"), 40), "dynamic hdr" in side_types or "hdr10+" in side_types,
                    _finite_duration(row.get("duration")) is None))
            elif kind == "audio":
                sample_rate = _number(row.get("sample_rate")) or 0
                audios.append(AudioTrack(index, codec, language, title, _duration(row), _number(row.get("start_time")),
                    _integer(row.get("channels")), int(sample_rate), _text(row.get("profile"), 80), bool(disposition.get("default")),
                    _finite_duration(row.get("duration")) is None))
            elif kind == "subtitle":
                subtitles.append(SubtitleTrack(index, codec, language, title))
            else:
                others.append(f"#{index} / {kind or '種類不明'} / {codec or 'codec不明'} / {title or '名称なし'}")
        if not videos:
            raise MediaError("通常の映像トラックがありません。静止画添付だけでないMKV動画を選んでください。")
        details = data.get("format") or {}
        chapters = data.get("chapters") or []
        if not isinstance(chapters, list):
            raise MediaError("チャプター情報を読み取れません。別のMKVを選んでください。")
        if before != _identity(path):
            raise MediaError("確認中にMKVが変更されました。ファイルの更新完了後に確認し直してください。")
        start = _number(details.get("start_time"))
        duration = _finite_duration(details.get("duration"))
        duration = _finite_duration(duration - (start or 0.0)) if duration is not None else None
        return Source(uuid4().hex, path, name, mode, before, duration,
            start, tuple(videos), tuple(audios), tuple(subtitles), tuple(others), len(chapters))

    def prepare_local(self, owner, value):
        value = str(value).strip().strip('"')
        path = Path(value).expanduser()
        if not path.is_absolute() or path.suffix.lower() != ".mkv" or _network(path):
            raise MediaError("PC内のMKVファイルのフルパスを指定してください。")
        try:
            path = path.resolve(strict=True)
        except (OSError, ValueError, RuntimeError) as exc:
            raise MediaError("MKVが見つかりません。ファイルの場所を確認してください。") from exc
        if _network(path):
            raise MediaError("ネットワーク上ではなくPC内のMKVを指定してください。")
        return self._prepare(owner, path=path, name=path.name)

    def prepare_upload(self, owner, name, buffer):
        if not isinstance(name, str) or Path(name).suffix.lower() != ".mkv":
            raise MediaError("MKVファイルを選択してください。")
        if not isinstance(buffer, (bytes, bytearray, memoryview)) or not 0 < len(buffer) <= UPLOAD_LIMIT:
            raise MediaError("アップロードは空でない200MB以内のMKVに対応します。大容量はPC内ファイルを直接指定してください。")
        return self._prepare(owner, buffer=buffer, name=name.replace("\\", "/").split("/")[-1])

    def _prepare(self, owner, *, path=None, buffer=None, name):
        with self._lock:
            self._owner(owner)
            if self._busy(owner):
                raise MediaError("動画変換中です。完了または中止後にMKVを切り替えてください。")
            folder = self._folder()
            self._preparing.add(owner)
        try:
            if path is None:
                path = folder / "input.mkv"
                with path.open("xb") as output:
                    output.write(buffer)
            source = self._source(path, name, "upload" if buffer is not None else "local")
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

    def plan(self, owner, source_id, *, video_index, audio_index=None, subtitle_index=None,
             mode="copy", directory, name):
        with self._lock:
            self._owner(owner)
            work = self._works.get(owner)
            if not work or work.source.id != source_id:
                raise MediaError("確認済みMKVが変わりました。動画を確認し直してください。")
            if self._busy(owner):
                raise MediaError("変換の完了または中止後に、出力計画を作り直してください。")
            if mode not in MODES:
                raise MediaError("映像保持または互換性重視を選んでください。")
            source = work.source
            if source.identity != _identity(source.path):
                raise MediaError("確認後にMKVが変更されました。動画を確認し直してください。")
            video = _track(source.videos, video_index, "映像")
            audio = _track(source.audios, audio_index, "音声", optional=True)
            subtitle = _track(source.subtitles, subtitle_index, "字幕", optional=True)
            if not video.width or not video.height:
                raise MediaError("映像の解像度を確認できません。別の映像トラックかMKVを選んでください。")
            if video.dolby_vision or video.dynamic_hdr:
                raise MediaError("Dolby Vision・動的HDRの保持は未対応です。通常のSDR/HDR10/HLG映像を含む別トラックかファイルを選んでください。")
            if abs(video.rotation) % 360 > 0.01:
                raise MediaError("回転情報のある映像は未対応です。向きを確定した動画、または別の映像トラックを選んでください。")
            if mode == "copy" and video.codec not in {"h264", "hevc"}:
                raise MediaError(f"{video.codec or '不明codec'}の映像保持は未対応です。SDR映像なら「互換性重視」で計画を作り直してください。")
            if mode == "compatible" and video.hdr:
                raise MediaError("HDRのSDR変換には対応していません。HEVCのHDR10/HLGなら「映像保持」を選ぶか、SDRの別トラック・ファイルを使ってください。")
            if mode == "compatible" and video.bit_depth > 8 and video.color_transfer in _UNKNOWN:
                raise MediaError("10-bit以上の映像でSDR/HDRの判定に必要な情報がありません。映像保持か、SDRと確認できる別の動画を選んでください。")
            if video.hdr and (video.codec != "hevc" or video.color_transfer not in _HDR_TRANSFERS):
                raise MediaError("このHDR映像の保持条件を確認できません。HEVCのHDR10/HLG、またはSDR映像を選んでください。")
            if mode == "compatible" and video.field_order not in {"", "unknown", "progressive"}:
                raise MediaError("インターレース映像の再圧縮には未対応です。H.264/HEVCなら映像保持を選ぶか、プログレッシブ動画を使ってください。")
            if subtitle and subtitle.codec != "subrip":
                raise MediaError("字幕の保持はSubRip（SRT）1本のみです。SubRipを選ぶか「字幕なし」で計画を作り直してください。")
            if audio and not 1 <= audio.channels <= 32:
                raise MediaError("音声のチャンネル数を確認できません。別の音声トラックか「音声なし」を選んでください。")
            target, folder_identity = _destination(directory, name, self._base)
            warnings = []
            audio_codec = None if audio is None else "copy" if (
                mode == "copy" and audio.codec == "aac" and audio.profile.lower() in {"lc", "aac lc", ""}
            ) else "aac"
            channels = None if audio is None else audio.channels if audio_codec == "copy" else 1 if audio.channels == 1 else 2
            if mode == "compatible":
                warnings.append("映像をH.264/yuv420pへ再圧縮します。画質とファイルサイズが変わり、時間がかかります。")
                if video.width % 2 or video.height % 2:
                    warnings.append("奇数の幅・高さは、右端・下端に最大1画素の余白を加えて偶数にします。")
            if audio and audio_codec != "copy":
                warnings.append("選択した音声をAAC 192kbpsへ再圧縮します。")
                if audio.channels > 2:
                    warnings.append(f"{audio.channels}chの音声を2chステレオへ変換します。")
            if video.bit_depth > 8:
                warnings.append("10-bit以上の映像は再生機器によって対応が異なります。" if mode == "copy" else "SDRの階調を8-bitに変換します。")
            if video.hdr:
                warnings.append("HDRの映像を保持します。HDRとHEVCに対応する再生環境が必要です。SDR化は行いません。")
            if video.codec == "hevc" and mode == "copy":
                warnings.append("HEVC映像を保持するため、再生機器にHEVC対応が必要です。")
            if subtitle:
                warnings.append("SubRip字幕をMP4用mov_textへ変換します。装飾や表示は元字幕と異なる場合があります。")
            warnings.append("変換中は元のMKVや保存先フォルダーを編集・移動しないでください。")
            dropped = [f"映像 #{row.index} / {row.codec}" for row in source.videos if row.index != video.index]
            dropped += [f"音声 #{row.index} / {row.codec}" for row in source.audios if not audio or row.index != audio.index]
            dropped += [f"字幕 #{row.index} / {row.codec}" for row in source.subtitles if not subtitle or row.index != subtitle.index]
            dropped += list(source.other_streams)
            if source.chapter_count:
                dropped.append(f"チャプター {source.chapter_count}件")
            dropped.append("元ファイルの全体メタデータ・添付・未選択のトラック")
            actual_video_start = _packet_start(self._tools(), source.path, video.index)
            actual_audio_start = _packet_start(self._tools(), source.path, audio.index) if audio else None
            duration = video.duration
            if video.duration_from_tag and duration is not None and actual_video_start is not None:
                duration = _finite_duration(duration + (video.start_time or 0.0) - actual_video_start)
            duration = duration or source.duration
            estimated = max(32 * 1024 * 1024, int(source.identity[0] * (1.1 if mode == "copy" else 2.0)))
            notes = ["選択した映像・音声・字幕の相対タイミングを保持し、MP4の開始位置を非負の時刻へそろえます。",
                     "長さの短い音声に合わせて映像を切り詰めません。"]
            offset = actual_audio_start - actual_video_start if audio and actual_audio_start is not None and actual_video_start is not None else None
            if actual_video_start is None or audio and actual_audio_start is None:
                warnings.append("先頭パケット時刻を時間内に取得できませんでした。映像・音声の開始差は厳密に比較できません。")
            if offset is not None and abs(offset) > 0.1:
                notes.append(f"選択音声の開始差（映像基準）: {offset:+.3f}秒。この差を保ちます。")
            if duration is None:
                warnings.append("元動画の長さを取得できず、残り時間と出力の長さの厳密な比較はできません。")
            warnings.append("必要容量は概算です。再圧縮後の大きさは内容で変わるため、十分な空き容量を用意してください。")
            if source.identity != _identity(source.path):
                raise MediaError("計画の準備中にMKVが変更されました。動画を確認し直してください。")
            plan = Plan(uuid4().hex, source.id, video.index, audio.index if audio else None, subtitle.index if subtitle else None,
                mode, target, "copy" if mode == "copy" else "libx264", audio_codec, "mov_text" if subtitle else None,
                estimated, tuple(warnings), tuple(dropped), tuple(notes), folder_identity, duration, offset, channels)
            work.plan, work.touched = plan, time.monotonic()
            return plan

    def _check_encoders(self, plan):
        needed = ({"libx264"} if plan.mode == "compatible" else set()) | ({"aac"} if plan.audio_codec == "aac" else set()) | ({"mov_text"} if plan.subtitle_index is not None else set())
        if not needed:
            return
        if self._encoders is None:
            try:
                result = subprocess.run([str(self._tools().ffmpeg), "-hide_banner", "-encoders"],
                    capture_output=True, timeout=10, check=False, **_process_options())
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise MediaError("FFmpegの機能を確認できません。FFmpeg/FFprobeの配置を確認してください。") from exc
            if result.returncode:
                raise MediaError("FFmpegの機能を確認できません。正常に実行できるFFmpegを用意してください。")
            self._encoders = {match.group(1) for match in re.finditer(rb"(?m)^\s*[VAS][A-Z.]{5}\s+(\S+)\s", result.stdout)}
            self._encoders = {value.decode("ascii", errors="ignore") for value in self._encoders}
        missing = needed - self._encoders
        if missing:
            raise MediaError("FFmpegに必要なエンコーダーがありません（" + ", ".join(sorted(missing)) + "）。対応するFFmpegを用意するか、保持できる別の計画を選んでください。")

    def start(self, owner, plan_id, *, operation_id):
        with self._lock:
            self._owner(owner)
            work = self._works.get(owner)
            if not work:
                raise MediaError("MKVを確認して出力計画を作成してください。")
            if not isinstance(operation_id, str) or not _OWNER.fullmatch(operation_id):
                raise MediaError("処理の識別情報が不正です。出力計画を確認し直してください。")
            known = work.operations.get(operation_id)
            if known:
                if known[0] != plan_id:
                    raise MediaError("同じ開始操作の変換条件が変わっています。計画を確認し直してください。")
                return known[1]
            plan = work.plan
            if not plan or plan.id != plan_id:
                raise MediaError("出力計画が変わりました。現在の計画を確認し直してください。")
            if self._preparing or any(self._busy(key) for key in self._works):
                raise MediaError("別の動画変換が実行中です。完了または中止後に開始してください。")
            if work.source.identity != _identity(work.source.path):
                raise MediaError("計画確認後にMKVが変更されました。動画を確認し直してください。")
            _check_destination(plan)
            self._check_encoders(plan)
            if shutil.disk_usage(plan.target.parent).free < plan.estimated_bytes + 32 * 1024 * 1024:
                raise MediaError("保存先の空き容量が概算必要量より少ないため開始していません。容量を空けるか別の保存先を選んでください。")
            job_id = uuid4().hex
            if not acquire_conversion(self.root, job_id):
                raise MediaError("MP3またはMP4の変換が実行中です。完了または中止後に開始してください。")
            folder = work.folder / uuid4().hex
            try:
                folder.mkdir()
                job = Job(job_id, operation_id, plan, work.source, folder)
                work.job = job
                job.thread = threading.Thread(target=self._worker, args=(work, job), name="proloag-video-" + job.id[:8], daemon=True)
                work.operations[operation_id] = (plan.id, job.id)
                work.touched = time.monotonic()
                job.thread.start()
            except BaseException as exc:
                work.operations.pop(operation_id, None)
                if work.job and work.job.id == job_id:
                    work.job.status = "failed"
                    work.job.error = "変換処理を開始できませんでした。もう一度開始してください。"
                try:
                    if folder.exists():
                        shutil.rmtree(folder)
                except OSError:
                    pass
                finally:
                    release_conversion(self.root, job_id)
                if isinstance(exc, Exception) and not isinstance(exc, MediaError):
                    raise MediaError("変換処理を開始できませんでした。画面を更新して再試行してください。") from exc
                raise
            return job.id

    def _cleanup_output_temp(self, job):
        folder = job.output_temp
        if folder is None or not folder.exists():
            return
        if (folder.parent != job.plan.target.parent or _folder_identity(folder) != job.output_temp_identity
                or _folder_identity(folder.parent) != job.plan.directory_identity):
            raise MediaError("一時出力の場所が変更されたため自動削除を止めました。保存先を確認してください。")
        # The chosen destination is user-owned. Never recursively remove
        # unexpected files that appeared inside our temporary output folder.
        partial = folder / "partial.mp4"
        if partial.exists() or partial.is_symlink():
            if _linked(partial) or not partial.is_file():
                raise MediaError("一時出力が別の項目に置き換えられたため、自動削除を止めました。")
            partial.unlink()
        folder.rmdir()

    def _validate_output(self, job, partial):
        data = _probe(self._tools(), partial)
        if not {"mov", "mp4"}.intersection(str(data.get("format", {}).get("format_name", "")).split(",")):
            raise MediaError("出力がMP4として確認できません。元MKVか変換方式を変更して再試行してください。")
        rows = data.get("streams") or []
        videos = [row for row in rows if row.get("codec_type") == "video"]
        audios = [row for row in rows if row.get("codec_type") == "audio"]
        subtitles = [row for row in rows if row.get("codec_type") == "subtitle"]
        if (len(videos) != 1 or len(audios) != int(job.plan.audio_index is not None)
                or len(subtitles) != int(job.plan.subtitle_index is not None)
                or len(rows) != len(videos) + len(audios) + len(subtitles)):
            raise MediaError("出力トラックの本数が計画と一致しません。選択トラックを変えて再試行してください。")
        source_video = _track(job.source.videos, job.plan.video_index, "映像")
        video = videos[0]
        if video.get("codec_name") != (source_video.codec if job.plan.mode == "copy" else "h264"):
            raise MediaError("出力映像のcodecが計画と一致しません。別の変換方式を選んでください。")
        width = source_video.width + (source_video.width % 2 if job.plan.mode == "compatible" else 0)
        height = source_video.height + (source_video.height % 2 if job.plan.mode == "compatible" else 0)
        if video.get("width") != width or video.get("height") != height or (job.plan.mode == "compatible" and video.get("pix_fmt") != "yuv420p"):
            raise MediaError("出力映像の解像度・色形式を確認できません。別の映像トラックで再試行してください。")
        if job.plan.mode == "copy":
            for attribute in ("pix_fmt", "color_transfer", "color_primaries", "color_space"):
                before = getattr(source_video, attribute)
                if before not in _UNKNOWN and video.get(attribute) != before:
                    raise MediaError("映像保持で色形式・HDR情報の一致を確認できません。対応できる別の映像を選んでください。")
        if audios:
            source_audio = _track(job.source.audios, job.plan.audio_index, "音声")
            expected = source_audio.codec if job.plan.audio_codec == "copy" else "aac"
            if audios[0].get("codec_name") != expected or audios[0].get("channels") != job.plan.audio_channels:
                raise MediaError("出力音声が計画と一致しません。別の音声トラックか「音声なし」を選んでください。")
            astart = _packet_start(self._tools(), partial, audios[0]["index"])
            vstart = _packet_start(self._tools(), partial, video["index"])
            if (job.plan.expected_audio_offset is not None and astart is not None and vstart is not None
                    and abs(astart - vstart - job.plan.expected_audio_offset) > 0.25):
                raise MediaError("映像と音声の開始差を保持できませんでした。別の音声トラックか変換方式を選んでください。")
            if job.plan.expected_audio_offset is not None and (astart is None or vstart is None):
                job.warnings += ("出力の先頭パケット時刻を取得できず、映像・音声の開始差は厳密に比較できませんでした。",)
        if subtitles and subtitles[0].get("codec_name") != "mov_text":
            raise MediaError("字幕の変換結果を確認できません。「字幕なし」で計画を作り直してください。")
        duration = _finite_duration(data.get("format", {}).get("duration"))
        expected = job.plan.expected_duration
        actual_video = _finite_duration(video.get("duration"))
        if (source_video.duration is not None and expected is not None and actual_video is not None
                and abs(actual_video - expected) > max(0.5, expected * 0.0001)):
            raise MediaError("出力映像の長さが元の情報と一致しません。途中で切れた動画でないか確認し、別方式で再試行してください。")
        return duration

    def _worker(self, work, job):
        outcome, process = "failed", None
        progress_file, errors_file = job.folder / "progress.txt", job.folder / "errors.txt"
        try:
            if job.cancel.is_set():
                outcome = "cancelled"
                return
            if job.source.identity != _identity(job.source.path):
                raise MediaError("開始前に元MKVが変更されました。動画を確認し直してください。")
            _check_destination(job.plan)
            folder = Path(tempfile.mkdtemp(prefix=".proloag-video-", dir=job.plan.target.parent))
            job.output_temp, job.output_temp_identity = folder, _folder_identity(folder)
            partial = folder / "partial.mp4"
            with progress_file.open("wb") as progress, errors_file.open("wb") as errors:
                process = subprocess.Popen(conversion_command(self._tools(), job.source, job.plan, partial),
                    stdin=subprocess.DEVNULL, stdout=progress, stderr=errors, **_process_options())
                with self._lock:
                    job.process = process
                    job.status = "cancelling" if job.cancel.is_set() else "running"
                while process.poll() is None:
                    elapsed = time.monotonic() - job.started
                    if job.cancel.is_set() or elapsed > self.timeout:
                        self._stop_process(process)
                        if not job.cancel.is_set():
                            raise MediaError("変換が制限時間を超えました。短い動画に分けるか、対応映像の「映像保持」を選んでください。")
                        break
                    if shutil.disk_usage(job.plan.target.parent).free < 32 * 1024 * 1024:
                        self._stop_process(process)
                        raise MediaError("保存先の空き容量が少なくなったため停止しました。既存ファイルは変更していません。容量を空けるか別の保存先で再試行してください。")
                    matches = re.findall(r"(?:^|\n)out_time_us=(\d+)", _tail(progress_file))
                    with self._lock:
                        job.elapsed = elapsed
                        if matches and job.plan.expected_duration:
                            job.progress = max(job.progress, min(0.99, int(matches[-1]) / 1_000_000 / job.plan.expected_duration))
                        if partial.exists():
                            job.size = partial.stat().st_size
                    job.cancel.wait(0.2)
            if job.cancel.is_set():
                outcome = "cancelled"
                return
            if process.returncode or not partial.is_file() or partial.stat().st_size == 0:
                raise MediaError("MP4変換に失敗しました。映像保持なら互換性重視、または別の音声・字幕なしで計画を作り直してください。")
            if job.source.identity != _identity(job.source.path):
                raise MediaError("変換中に元MKVが変更されました。出力は確定していません。動画を確認し直してください。")
            duration = self._validate_output(job, partial)
            output_size = partial.stat().st_size
            with partial.open("r+b") as output:
                os.fsync(output.fileno())
            with self._lock:
                if job.cancel.is_set():
                    outcome = "cancelled"
                    return
                _check_destination(job.plan)
                if job.source.identity != _identity(job.source.path):
                    raise MediaError("出力検査中に元MKVが変更されました。保存は確定していません。動画を確認し直してください。")
                try:
                    _publish_no_replace(partial, job.plan.target)
                except FileExistsError as exc:
                    raise MediaError("変換中に同じ保存名が使われました。既存ファイルは変更していません。別の名前で計画を作り直してください。") from exc
                job.output_path, job.duration = job.plan.target, duration
                job.size, job.progress = output_size, 1.0
                outcome = "done"
        except Exception as exc:
            with self._lock:
                job.error = str(exc) if isinstance(exc, MediaError) else "変換・保存を完了できませんでした。保存先の容量・権限を確認するか、別の動画・トラックで再試行してください。"
                job.details = _tail(errors_file, 6000)
        finally:
            if process is not None and process.poll() is None:
                try:
                    self._stop_process(process)
                except (OSError, subprocess.TimeoutExpired):
                    outcome = "failed"
                    if not job.error:
                        job.error = "変換プロセスの終了を確認できません。一時出力は残しています。FFmpegが終了してから再試行してください。"
                    # A live child keeps its output and shared execution slot.
                    # Reap it before normal cleanup, even after a stop error.
                    with self._lock:
                        job.status = "cancelling"
                    while process.poll() is None:
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            try:
                                process.kill()
                            except OSError:
                                pass
                        except OSError:
                            time.sleep(0.2)
            try:
                self._cleanup_output_temp(job)
            except (MediaError, OSError) as exc:
                message = str(exc) if isinstance(exc, MediaError) else "変換用の一時フォルダーを空にできなかったため残しています。未知の項目は削除していません。保存先を確認してください。"
                job.warnings += (message,)
                if not job.error:
                    job.error = message
            release_conversion(self.root, job.id)
            with self._lock:
                if job.cancel.is_set() and outcome != "done":
                    outcome = "cancelled"
                job.status, job.elapsed = outcome, time.monotonic() - job.started
                job.process = None
                work.touched = time.monotonic()

    @staticmethod
    def _stop_process(process):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def snapshot(self, owner):
        with self._lock:
            self._owner(owner)
            work = self._works.get(owner)
            if not work:
                return {"source": None, "plan": None, "job": None}
            work.touched = time.monotonic()
            job = work.job
            view = None if job is None else {key: getattr(job, key) for key in (
                "id", "operation_id", "plan", "status", "progress", "elapsed", "size", "duration", "error", "details", "warnings", "output_path")}
            return {"source": work.source, "plan": work.plan, "job": view}

    def cancel(self, owner):
        with self._lock:
            self._owner(owner)
            work = self._works.get(owner)
            if work and work.job and work.job.status in ACTIVE:
                work.job.cancel.set()
                work.job.status = "cancelling"

    def clear(self, owner):
        with self._lock:
            self._owner(owner)
            if self._busy(owner):
                raise MediaError("変換が停止してから一時データをクリアしてください。")
            work = self._works.get(owner)
            if work:
                self._remove(work.folder)
                del self._works[owner]

    def prune(self):
        with self._lock:
            if self._closed:
                return
            now = time.monotonic()
            for owner, work in list(self._works.items()):
                if not self._busy(owner) and now - work.touched > self.ttl:
                    try:
                        self._remove(work.folder)
                    except (MediaError, OSError):
                        continue
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
            if job.thread and job.thread.ident is not None and job.thread is not threading.current_thread():
                job.thread.join(timeout=5)
        with self._lock:
            if self._base and not any(job.thread and job.thread.is_alive() for job in jobs):
                shutil.rmtree(self._base, ignore_errors=True)
                self._works.clear()
