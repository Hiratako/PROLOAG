"""Dedicated ASR process. No Streamlit and no personal record-store imports."""

from __future__ import annotations

import gc
import json
import math
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

from app_core.transcription import (MODELS, atomic_json, file_lock, read_json, file_sha256,
                                    load_checkpoint, checkpoint_prefix, checkpoint_signature)
from app_core.media_conversion import _identity, _process_options

CHUNK_SECONDS = 300
OVERLAP_SECONDS = 2
TERMINAL = {"done", "cancelled", "failed"}
MAX_AUDIO_SECONDS = 24 * 3600


def gpu_failure_kind(error):
    """Classify only known GPU failures, never generic model or storage errors."""
    if not isinstance(error, (RuntimeError, OSError, ValueError)):
        return None
    if isinstance(error, OSError) and getattr(error, "winerror", None) in {126, 127}:
        return "missing_runtime"
    message = str(error).lower()
    gpu = any(value in message for value in ("cuda", "cublas", "cudnn"))
    if gpu and any(value in message for value in (
        "out of memory", "out_of_memory", "alloc_failed", "memory allocation",
    )):
        return "out_of_memory"
    if gpu and any(value in message for value in (
        "not found", "cannot be loaded", "could not load", "failed to load", "dll",
    )):
        return "missing_runtime"
    if gpu and any(value in message for value in (
        "driver", "no kernel image", "not supported", "unsupported", "no device",
        "invalid device", "initialization", "initialisation", "insufficient",
        "failed", "error", "status_", "not compiled",
    )):
        return "runtime_failure"
    if (isinstance(error, ValueError) and "compute type" in message
            and "target device or backend" in message):
        return "unsupported_compute_type"
    return None


def prepare_device(requested, root, runtime):
    """GPU is optional; the selected DLL-directory lease outlives execution."""
    if requested == "cpu":
        return "cpu", "", "", None
    lease = None
    try:
        if not runtime.get_cuda_device_count():
            return "cpu", "GPUを検出できなかったためCPUで処理します。", "not_available", None
        if os.name == "nt":
            import ctypes

            # ctypes uses secure DLL search flags, which do not search PATH.
            # Select one actual cuBLAS location explicitly: AddDllDirectory
            # alone does not define precedence between multiple directories.
            library = "cublas64_12.dll"
            directories = [Path(root).resolve() / "tools/asr-cuda/bin"]
            cuda_path = os.environ.get("CUDA_PATH", "").strip().strip('"')
            if cuda_path and Path(cuda_path).is_absolute():
                directories.append(Path(cuda_path) / "bin")
            directories.extend(Path(value.strip().strip('"'))
                               for value in os.environ.get("PATH", "").split(os.pathsep)
                               if value.strip().strip('"'))
            for directory in directories:
                if directory.is_absolute() and (directory / library).is_file():
                    directory = directory.resolve()
                    lease = os.add_dll_directory(str(directory))
                    library = str(directory / library)
                    break
            try:
                # CT2 4.8.4 uses native CUDA Conv1D; cuDNN is optional. Runtime
                # variants that need it are covered by model-load/run fallback.
                ctypes.WinDLL(library)
            except OSError:
                if lease is not None:
                    lease.close()
                return "cpu", "GPU用ライブラリを読み込めなかったためCPUで処理します。", "missing_runtime", None
        return "cuda", "", "", lease
    except (RuntimeError, OSError, ValueError) as error:
        if lease is not None:
            lease.close()
        kind = "missing_runtime" if isinstance(error, OSError) else gpu_failure_kind(error)
        if kind is None:
            raise
        return "cpu", "GPUの実行環境を確認できなかったためCPUで処理します。", kind, None


def release_gpu_model(model):
    """Free CT2 weights before CPU retry even while an exception holds frames."""
    unload = getattr(getattr(model, "model", None), "unload_model", None)
    if callable(unload):
        try:
            unload()
        except (RuntimeError, OSError, ValueError) as error:
            if gpu_failure_kind(error) is None:
                raise
            # A failed CUDA context may reject cleanup too. The worker stays
            # on CPU and its process lifetime still bounds remaining handles.
    gc.collect()


class GPUInferenceFailure(RuntimeError):
    def __init__(self, kind, error):
        super().__init__(str(error))
        self.kind = kind


def inference_call(device, action, *args, **kwargs):
    """Only native model calls may authorize the GPU-to-CPU retry path."""
    try:
        return action(*args, **kwargs)
    except (RuntimeError, OSError, ValueError) as error:
        kind = gpu_failure_kind(error) if device == "cuda" else None
        if kind is None:
            raise
        raise GPUInferenceFailure(kind, error) from error


def inference_segments(device, segments):
    iterator = inference_call(device, iter, segments)
    while True:
        try:
            value = inference_call(device, next, iterator)
        except StopIteration:
            return
        yield value


def _mkv_audio_limit(request):
    """Bound timestamp padding as well as decoded content, before native work."""
    duration = request.get("duration")
    if (request.get("timeline") != "container_start" or type(duration) not in {int, float}
            or not math.isfinite(duration) or not 0 < duration <= MAX_AUDIO_SECONDS):
        raise ValueError("MKVの長さ・時刻設定を確認できません。元ファイルは変更していません。")
    return min(MAX_AUDIO_SECONDS, duration + 1)


def _validate_mkv_audio(path, limit):
    """Reject incomplete/oversized PCM before model loading or publishing WAV."""
    try:
        if not 44 <= path.stat().st_size <= math.ceil(limit * 32000) + 1024**2:
            raise ValueError("invalid WAV size")
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("invalid PCM format")
            frames = audio.getnframes()
            if not 0 < frames <= math.ceil(limit * 16000):
                raise ValueError("invalid PCM duration")
            audio.setpos(frames - 1)
            if len(audio.readframes(1)) != 2:
                raise ValueError("truncated PCM")
    except (OSError, EOFError, wave.Error, ValueError) as exc:
        raise ValueError("抽出したMKV音声の形式・長さ・完全性を確認できません。元ファイルは保持しています。") from exc


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


def require_model_assets(model_path):
    # Older Hub caches can expose a snapshot directory before its weights exist.
    for name in ("config.json", "model.bin"):
        asset = Path(model_path) / name
        if not asset.is_file() or asset.stat().st_size == 0:
            raise OSError("The recognition model snapshot is incomplete.")
    return model_path


def run(folder):
    folder = Path(folder).resolve()
    request = read_json(folder / "request.json")
    if not request or folder.name != request["id"]:
        raise ValueError("Invalid job folder")
    state = read_json(folder / "state.json")
    cuda_runtime_lease = None
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
            requested_device = request.get("device", "cpu")
            if (not isinstance(request.get("model"), str) or request["model"] not in MODELS.values()
                    or not isinstance(requested_device, str) or requested_device not in {"cpu", "auto"}):
                raise ValueError("認識モデル・処理デバイスの設定を確認してください。")
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
            if request.get("media_kind") == "mkv":
                limit = _mkv_audio_limit(request)
                # Keep the container clock, normalize its origin to zero, and
                # materialize delayed audio/gaps as PCM silence. ASR, playback
                # and subtitle timestamps then use the same video timeline.
                position = command.index("-i")
                command[position:position] = ["-xerror", "-copyts", "-start_at_zero"]
                command[-1:-1] = [
                    "-af", "aresample=16000:async=1:first_pts=0",
                    "-t", str(limit + 1),
                    "-fs", str(math.ceil((limit + 1) * 32000) + 1024**2),
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
            if request.get("media_kind") == "mkv":
                _validate_mkv_audio(partial, limit)
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

            device, warning, fallback_reason, cuda_runtime_lease = prepare_device(
                requested_device, request["root"], ctranslate2
            )
            kwargs = dict(
                download_root=str(Path(request["root"]) / "tools/asr-models"),
                cpu_threads=max(1, min(8, (os.cpu_count() or 4) // 2)),
            )
            try:
                model_path = require_model_assets(download_model(
                    request["model"],
                    cache_dir=kwargs["download_root"],
                    local_files_only=True,
                ))
            except Exception:
                try:
                    model_path = require_model_assets(download_model(
                        request["model"], cache_dir=kwargs["download_root"]
                    ))
                except Exception as exc:
                    raise RuntimeError(
                        f"認識モデル（{request['model']}）を取得できませんでした。"
                        "インターネット接続・空き容量・保存先の書き込み権限を確認して再試行してください。"
                    ) from exc
            update(
                device=device, requested_device=requested_device, model=request["model"],
                compute_type="int8_float16" if device == "cuda" else "int8",
                device_history=[], gpu_fallback_reason=fallback_reason, warning=warning,
            )
            try:
                model = WhisperModel(
                    model_path,
                    device=device,
                    compute_type="int8_float16" if device == "cuda" else "int8",
                    **kwargs,
                )
            except (RuntimeError, OSError, ValueError) as error:
                kind = gpu_failure_kind(error)
                if device != "cuda" or requested_device != "auto" or kind is None:
                    raise
                fallback_reason = kind
                warning = "GPUを初期化できなかったため、同じ認識モデルのままCPUで処理します。"
                device = "cpu"
                update(device=device, compute_type="int8", gpu_fallback_reason=kind, warning=warning)
                model = WhisperModel(
                    model_path, device="cpu", compute_type="int8", **kwargs
                )
            history = [{"device": device, "from_seconds": resumed["completed_seconds"] if resumed else 0,
                        "reason": fallback_reason}]
            update(
                status="transcribing",
                message="文字起こしを進めています",
                device=device,
                requested_device=requested_device,
                model=request["model"],
                compute_type="int8_float16" if device == "cuda" else "int8",
                device_history=list(history),
                gpu_fallback_reason=fallback_reason,
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
                    window_position, window_count = output.tell(), count
                    window_digest, window_languages = prefix_digest.copy(), set(languages)
                    while True:
                        segments = None
                        try:
                            segments, info = inference_call(device, model.transcribe,
                                samples,
                                language=None if request["language"] == "auto" else request["language"],
                                task="transcribe", beam_size=5, vad_filter=True,
                                condition_on_previous_text=False,
                                hotwords=request["hotwords"] or None,
                            )
                            languages.add(info.language)
                            for row in normalized_segments(
                                inference_segments(device, segments), offset, core_start, core_end, duration, count
                            ):
                                check_cancel(folder)
                                encoded = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
                                output.write(encoded)
                                prefix_digest.update(encoded)
                                output.flush()
                                os.fsync(output.fileno())
                                count += 1
                                update(
                                    progress=min(0.999, row["end"] / duration), segments=count,
                                    detected_languages=sorted(languages),
                                )
                            break
                        except GPUInferenceFailure as error:
                            kind = error.kind
                            if device != "cuda" or requested_device != "auto":
                                raise
                            check_cancel(folder)
                            # A CUDA generator may have flushed part of this
                            # window. Keep the committed prefix exactly once;
                            # CPU re-runs only the uncommitted current window.
                            if segments is not None and hasattr(segments, "close"):
                                segments.close()
                            output.seek(window_position)
                            output.truncate()
                            output.flush()
                            os.fsync(output.fileno())
                            count, prefix_digest = window_count, window_digest
                            languages = set(window_languages)
                            release_gpu_model(model)
                            model = None
                            device = "cpu"
                            history.append({"device": "cpu", "from_seconds": core_start, "reason": kind})
                            warning = "GPUの処理を続行できなかったため、同じ認識モデルのままCPUへ切り替えました。完了区間を保持し、未完了区間を再認識します。"
                            update(
                                status="loading", message="同じ認識モデルをCPUで準備しています",
                                device=device, compute_type="int8", device_history=list(history),
                                gpu_fallback_reason=kind, warning=warning,
                                progress=min(0.999, core_start / duration), segments=count,
                                detected_languages=sorted(languages),
                            )
                            check_cancel(folder)
                            model = WhisperModel(model_path, device="cpu", compute_type="int8", **kwargs)
                            update(status="transcribing", message="CPUで文字起こしを続けています")
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
                try:
                    sync_memo()
                finally:
                    if cuda_runtime_lease is not None:
                        cuda_runtime_lease.close()


if __name__ == "__main__":
    run(sys.argv[1])
