"""Dedicated ASR process. No Streamlit and no personal record-store imports."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import wave

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_core.transcription import (atomic_json, file_lock, read_json, file_sha256,
                                    load_checkpoint, checkpoint_prefix, checkpoint_signature)
from app_core.media_conversion import _identity, _process_options

CHUNK_SECONDS = 300
OVERLAP_SECONDS = 2
TERMINAL = {"done", "cancelled", "failed"}


class Cancelled(Exception):
    pass


def check_cancel(folder):
    if (folder / "cancel").exists():
        raise Cancelled()


def normalized_segments(segments, offset, core_start, core_end, duration, first_id):
    """Assign overlapping recognition segments by midpoint to one core window."""
    number = first_id
    for segment in segments:
        start, end = float(segment.start) + offset, float(segment.end) + offset
        midpoint = (start + end) / 2
        if midpoint < core_start or midpoint >= core_end or not segment.text.strip():
            continue
        start, end = max(core_start, start), min(core_end, duration, end)
        if end <= start:
            continue
        yield {
            "id": number,
            "start": round(start, 3),
            "end": round(end, 3),
            "original_text": segment.text.strip(),
        }
        number += 1


def _sync_audio_memo_terminal(request, folder, state, update):
    """Sync a linked memo without turning a successful ASR into a failure."""
    memo_id = request.get("audio_memo_id")
    job_id = request.get("id")
    root_value = request.get("root")
    if (
        state.get("status") not in TERMINAL
        or not isinstance(memo_id, str)
        or not re.fullmatch("[a-f0-9]{32}", memo_id)
        or not isinstance(job_id, str)
        or not re.fullmatch("[a-f0-9]{32}", job_id)
        or state.get("id") != job_id
        or state.get("audio_memo_id") != memo_id
        or not isinstance(root_value, str)
        or not root_value
    ):
        return
    try:
        root = Path(root_value)
        if not root.is_absolute():
            return
        root = root.resolve()
        expected = root / "transcription_data/jobs" / job_id
        if (
            any(path.is_symlink() for path in (expected.parent.parent, expected.parent, expected))
            or Path(folder).resolve() != expected
        ):
            return
    except (OSError, ValueError, RuntimeError):
        return
    try:
        from app_core.audio_memo_service import sync_audio_memo

        sync_audio_memo(root, memo_id, job_id)
        if state.get("memo_save_error"):
            update(memo_save_error="")
    except Exception as exc:
        message = "録音メモの同期に失敗しました。文字起こしの結果は保持しています。履歴から再試行してください。"
        print(
            f"Audio memo synchronization failed ({type(exc).__name__}); ASR result is preserved.",
            file=sys.stderr,
            flush=True,
        )
        try:
            update(memo_save_error=message)
        except Exception:
            print("Could not record the audio memo synchronization warning.", file=sys.stderr, flush=True)


def run(folder):
    folder = Path(folder).resolve()
    request = read_json(folder / "request.json")
    if not request or folder.name != request["id"]:
        raise ValueError("Invalid job folder")
    state = read_json(folder / "state.json")
    started = time.monotonic()
    stop = threading.Event()
    state_lock = threading.Lock()
    memo_sync_lock = threading.Lock()
    memo_sync_attempted = False

    def update(**values):
        with state_lock:
            if (
                state.get("status") in TERMINAL
                and values.get("status") not in (None, state["status"])
            ):
                return
            state.update(
                values, elapsed=time.monotonic() - started, heartbeat=time.time()
            )
            atomic_json(folder / "state.json", state)

    def sync_memo():
        nonlocal memo_sync_attempted
        with memo_sync_lock:
            if not memo_sync_attempted:
                memo_sync_attempted = True
                _sync_audio_memo_terminal(request, folder, state, update)

    def heartbeat():
        while not stop.wait(2):
            update()

    def cancellation_watch():
        while not stop.wait(0.2):
            if (folder / "cancel").exists():
                # Inference/download/native calls cannot reliably be interrupted
                # from Python; durable segments already flushed remain available.
                try:
                    stop.set()
                    update(status="cancelled", message="中止しました", error="")
                    sync_memo()
                finally:
                    os._exit(0)

    with file_lock(folder / ".worker.lock", blocking=False):
        threading.Thread(target=heartbeat, daemon=True).start()
        try:
            check_cancel(folder)
            update(status="preparing", message="聞き直し用の音声を準備しています")
            source = Path(request["source_path"])
            if list(_identity(source)) != request["identity"]:
                raise ValueError(
                    "元ファイルが変更されました。もう一度確認してください。"
                )
            source_hash = file_sha256(source)
            if request.get("source_sha256", source_hash) != source_hash:
                raise ValueError("元音声のハッシュが変わっています。以前の結果は保持しています。")
            request["source_sha256"] = source_hash
            resumed = None
            prefix_digest = None
            if request.get("resume_from") is not None:
                if not isinstance(request["resume_from"], str) or not re.fullmatch("[a-f0-9]{32}", request["resume_from"]):
                    raise ValueError("再開元の認識履歴IDが不正です。")
                resumed = load_checkpoint(folder, request)
                if resumed.get("copied_from") != request["resume_from"]:
                    raise ValueError("再開元と完了区間の対応を確認できません。")
                prefix_digest = checkpoint_prefix(folder / "segments.jsonl", resumed, exact=True)
            audio = folder / "audio.wav"
            partial = folder / "audio.partial.wav"
            command = [
                request["ffmpeg"],
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(source),
                "-map",
                f"0:{request['track']['index']}",
                "-vn",
                "-sn",
                "-dn",
                "-map_metadata",
                "-1",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(partial),
            ]
            with (folder / "ffmpeg.log").open("wb") as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    **_process_options(),
                )
                try:
                    while process.poll() is None:
                        check_cancel(folder)
                        if time.monotonic() - started > 6 * 3600:
                            raise ValueError("音声の準備が時間内に完了しませんでした。")
                        time.sleep(0.1)
                    if process.returncode:
                        raise ValueError(
                            "音声の準備に失敗しました。ファイルを確認してください。"
                        )
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            if list(_identity(source)) != request["identity"]:
                raise ValueError(
                    "処理中に元ファイルが変更されました。もう一度確認してください。"
                )
            partial.replace(audio)
            # Start hard cancellation only after the FFmpeg child is reaped.
            threading.Thread(target=cancellation_watch, daemon=True).start()
            update(
                status="loading",
                message="認識モデルを準備しています（初回はダウンロード）",
            )
            check_cancel(folder)
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
            import numpy as np
            import ctranslate2

            device = "cpu"
            warning = ""
            if request["device"] == "auto":
                try:
                    if ctranslate2.get_cuda_device_count():
                        if os.name == "nt":
                            import ctypes

                            ctypes.WinDLL("cublas64_12.dll")
                            ctypes.WinDLL("cudnn64_9.dll")
                        device = "cuda"
                except (RuntimeError, OSError):
                    warning = "GPUの実行環境を確認できなかったためCPUで処理しています。"
            kwargs = dict(
                download_root=str(Path(request["root"]) / "tools/asr-models"),
                cpu_threads=max(1, min(8, (os.cpu_count() or 4) // 2)),
            )
            try:
                model_path = download_model(
                    request["model"],
                    cache_dir=kwargs["download_root"],
                    local_files_only=True,
                )
            except Exception:
                model_path = download_model(
                    request["model"], cache_dir=kwargs["download_root"]
                )
            try:
                model = WhisperModel(
                    model_path,
                    device=device,
                    compute_type="int8_float16" if device == "cuda" else "int8",
                    **kwargs,
                )
            except (RuntimeError, OSError):
                if device != "cuda":
                    raise
                warning = "GPUを初期化できなかったためCPUで処理しています。"
                device = "cpu"
                model = WhisperModel(
                    model_path, device="cpu", compute_type="int8", **kwargs
                )
            update(
                status="transcribing",
                message="文字起こしを進めています",
                device=device,
                warning=warning,
                detected_languages=[],
            )
            count = resumed["segments_count"] if resumed else 0
            languages = set(resumed["detected_languages"]) if resumed else set()
            completed = resumed["completed_seconds"] if resumed else 0
            if resumed:
                update(resumed_seconds=completed, segments=count)
            import hashlib
            prefix_digest = prefix_digest or hashlib.sha256()
            with (
                wave.open(str(audio), "rb") as incoming,
                (folder / "segments.jsonl").open(
                    "ab" if resumed else "wb"
                ) as output,
            ):
                rate = incoming.getframerate()
                duration = incoming.getnframes() / rate
                if completed > duration + 0.001:
                    raise ValueError("再開位置が実際の音声より後にあります。")
                starts = () if completed >= duration else range(int(completed), int(duration) + 1, CHUNK_SECONDS)
                for core_start in starts:
                    if core_start >= duration:
                        break
                    check_cancel(folder)
                    if time.monotonic() - started > 48 * 3600:
                        raise ValueError(
                            "処理が48時間を超えたため中断しました。途中結果は保持しています。"
                        )
                    core_end = min(duration, core_start + CHUNK_SECONDS)
                    offset = max(0, core_start - OVERLAP_SECONDS)
                    incoming.setpos(int(offset * rate))
                    samples = (
                        np.frombuffer(
                            incoming.readframes(
                                int((core_end - offset + OVERLAP_SECONDS) * rate)
                            ),
                            dtype=np.int16,
                        ).astype(np.float32)
                        / 32768.0
                    )
                    segments, info = model.transcribe(
                        samples,
                        language=None
                        if request["language"] == "auto"
                        else request["language"],
                        task="transcribe",
                        beam_size=5,
                        vad_filter=True,
                        condition_on_previous_text=False,
                        hotwords=request["hotwords"] or None,
                    )
                    languages.add(info.language)
                    for row in normalized_segments(
                        segments, offset, core_start, core_end, duration, count
                    ):
                        check_cancel(folder)
                        encoded = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                        output.write(encoded)
                        prefix_digest.update(encoded)
                        output.flush()
                        os.fsync(output.fileno())
                        count += 1
                        update(
                            progress=min(0.999, row["end"] / duration),
                            segments=count,
                            detected_languages=sorted(languages),
                        )
                    output.flush()
                    os.fsync(output.fileno())
                    atomic_json(folder / "checkpoint.json", {
                        "schema_version": 1, "job_id": folder.name,
                        "signature": checkpoint_signature(request), "source_sha256": source_hash,
                        "completed_seconds": core_end, "segments_count": count,
                        "segments_bytes": output.tell(), "segments_sha256": prefix_digest.hexdigest(),
                        "detected_languages": sorted(languages),
                    })
                    update(
                        progress=min(0.999, core_end / duration),
                        segments=count,
                        detected_languages=sorted(languages),
                    )
            check_cancel(folder)
            stop.set()
            update(
                status="done",
                progress=1.0,
                message="文字起こしが完了しました",
                segments=count,
            )
        except Cancelled:
            stop.set()
            update(status="cancelled", message="中止しました", error="")
        except Exception as exc:
            import traceback

            traceback.print_exc()
            stop.set()
            update(
                status="failed",
                message="処理を完了できませんでした",
                error=f"{type(exc).__name__}: {str(exc)[:1200]}",
            )
        finally:
            stop.set()
            try:
                (folder / "audio.partial.wav").unlink(missing_ok=True)
            finally:
                sync_memo()


if __name__ == "__main__":
    run(sys.argv[1])
