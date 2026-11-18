"""Native recorder worker. Importing this module never opens a microphone.

PCMWriter and injected run() sources support accelerated synthetic tests.
Only NativeInput (explicit run default) imports sounddevice and opens input.
"""
from __future__ import annotations

from array import array
from contextlib import contextmanager
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
from uuid import uuid4
import wave

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_core.long_recording import (ACTIVE, CHANNELS, MAX_SECONDS, RATE, RESERVE_BYTES, SAMPLE_WIDTH,
    TERMINAL, LongRecordingStore, WindowsProcess, not_link, now_text, process_identity)
from app_core.media_conversion import MediaError
from app_core.transcription import atomic_json, read_json


CHUNK_SECONDS = 60
CHECKPOINT_SECONDS = 2
MAX_BLOCK_BYTES = RATE * SAMPLE_WIDTH * 2
QUEUE_BLOCKS = 50


def query_devices():
    """PortAudio metadata only: do not instantiate any Stream here."""
    import sounddevice as sd
    hosts = sd.query_hostapis()
    devices = sd.query_devices()
    rows = []
    for index, device in enumerate(devices):
        host = hosts[device["hostapi"]]
        if "WASAPI" not in host["name"].upper() or device["max_input_channels"] < 1:
            continue
        rows.append({"id": index, "name": str(device["name"]), "default": index == host["default_input_device"],
                     "hostapi": "Windows WASAPI", "default_samplerate": device["default_samplerate"]})
    return sorted(rows, key=lambda row: (not row["default"], row["name"], row["id"]))


class NativeInput:
    def __init__(self, callback, device):
        import sounddevice as sd
        if os.name != "nt":
            raise MediaError("PC録音はWindows WASAPIで利用できます。")
        current = sd.query_devices(device["id"])
        host = sd.query_hostapis(current["hostapi"])
        if "WASAPI" not in host["name"].upper() or current["max_input_channels"] < 1 or current["name"] != device["name"]:
            raise MediaError("入力機器が変わっています。マイクを確認して選び直してください。")
        self.stream = sd.RawInputStream(samplerate=RATE, channels=CHANNELS, dtype="int16", blocksize=1600,
            device=device["id"], latency="high", callback=callback,
            extra_settings=sd.WasapiSettings(exclusive=False, auto_convert=True))

    @property
    def active(self):
        return self.stream.active

    def start(self):
        self.stream.start()

    def stop(self):
        self.stream.stop()

    def close(self):
        self.stream.close()


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunk_path(folder, index, suffix="wav"):
    not_link(folder / "chunks")
    path = folder / "chunks" / f"{index:06d}.{suffix}"
    not_link(path)
    if path.resolve().parent != (folder / "chunks").resolve():
        raise MediaError("録音区間の保存先を確認できません。")
    return path


def _wave_info(path):
    not_link(path)
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (CHANNELS, SAMPLE_WIDTH, RATE, "NONE"):
            raise MediaError("録音区間の音声形式が不正です。原音を保持しています。")
        frames = source.getnframes()
        if not frames or path.stat().st_size != 44 + frames * SAMPLE_WIDTH:
            raise MediaError("録音区間の長さを確認できません。原音を保持しています。")
    return frames


def _seal_part(folder, state, index):
    part = _chunk_path(folder, index, "part.pcm")
    final = _chunk_path(folder, index)
    allowed = RATE * state["max_seconds"] - sum(row["frames"] for row in state["segments"])
    frames = part.stat().st_size // SAMPLE_WIDTH
    if frames > allowed:
        raise MediaError("録音上限を超える途中データは上書きせず保持します。保存先を確認してください。")
    if frames <= 0:
        return
    temporary = final.with_name(final.name + "." + uuid4().hex + ".tmp")
    if final.exists():
        # A crash may have finalized the WAV just before its manifest ACK.
        if _wave_info(final) != frames:
            raise MediaError("未登録の録音区間と原音が一致しません。上書きしません。")
        with part.open("rb") as original, wave.open(str(final), "rb") as saved:
            remaining = frames
            while remaining:
                take = min(remaining, RATE)
                if original.read(take * SAMPLE_WIDTH) != saved.readframes(take):
                    raise MediaError("未登録の録音区間が変更されています。上書きしません。")
                remaining -= take
    else:
        try:
            with temporary.open("xb") as raw, part.open("rb") as source:
                with wave.open(raw, "wb") as output:
                    output.setnchannels(CHANNELS)
                    output.setsampwidth(SAMPLE_WIDTH)
                    output.setframerate(RATE)
                    remaining = frames * SAMPLE_WIDTH
                    while remaining:
                        block = source.read(min(1024**2, remaining))
                        if not block:
                            raise MediaError("録音途中の音声を読み切れません。原音を保持しています。")
                        output.writeframesraw(block)
                        remaining -= len(block)
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(temporary, final)
        finally:
            temporary.unlink(missing_ok=True)
    state["segments"].append({"index": index, "file": f"chunks/{index:06d}.wav", "frames": frames,
                              "sha256": _sha(final), "bytes": final.stat().st_size})
    state.update(total_frames=sum(row["frames"] for row in state["segments"]), saved_frames=sum(row["frames"] for row in state["segments"]),
                 active_file=None, active_frames=0, last_saved_at=now_text(), heartbeat=time.time())
    atomic_json(folder / "recording.json", state)
    # This indexed partial is already represented in an independently verified
    # WAV and committed manifest. No unrelated or unconfirmed raw file is removed.
    part.unlink()


def recover_files(folder, state):
    """Called only with the root recording worker lease held by the caller."""
    folder = Path(folder)
    expected = LongRecordingStore(folder.parents[2]).folder(state["id"])
    if folder.resolve() != expected.resolve():
        raise MediaError("録音復旧の保存先が不正です。")
    frames = 0
    for index, segment in enumerate(state["segments"]):
        path = _chunk_path(folder, index)
        if _wave_info(path) != segment["frames"] or _sha(path) != segment["sha256"]:
            raise MediaError("確定済み録音の内容が変わっています。自動復旧せず原音を保持します。")
        frames += segment["frames"]
    state.update(total_frames=frames, saved_frames=frames)
    parts = []
    not_link(folder / "chunks")
    for path in (folder / "chunks").iterdir():
        if re.fullmatch(r"\d{6}\.part\.pcm", path.name):
            not_link(path)
            parts.append((int(path.name[:6]), path))
    for index, part in sorted(parts):
        if index < len(state["segments"]):
            continue  # ACK was committed; never duplicate a leftover partial.
        if index != len(state["segments"]):
            raise MediaError("録音区間に欠番があります。原音を保持して保存先を確認してください。")
        if part.stat().st_size:
            _seal_part(folder, state, index)
    # A fully renamed WAV may survive even when no partial was left.
    while _chunk_path(folder, len(state["segments"])).is_file():
        index = len(state["segments"])
        path = _chunk_path(folder, index)
        count = _wave_info(path)
        if state["total_frames"] + count > RATE * state["max_seconds"]:
            raise MediaError("録音上限を超える区間は自動で取り込みません。")
        state["segments"].append({"index": index, "file": f"chunks/{index:06d}.wav", "frames": count,
                                  "sha256": _sha(path), "bytes": path.stat().st_size})
        state["total_frames"] += count
    state.update(saved_frames=state["total_frames"], active_file=None, active_frames=0, last_saved_at=now_text())
    atomic_json(folder / "recording.json", state)


class PCMWriter:
    """Bounded-block PCM writer with independent WAV chunks and checkpoints."""
    def __init__(self, folder, state, chunk_seconds=CHUNK_SECONDS, checkpoint_seconds=CHECKPOINT_SECONDS, clock=time.monotonic):
        self.folder, self.state, self.clock = Path(folder), state, clock
        if not 0 < chunk_seconds <= 60 or not 0 < checkpoint_seconds <= 10:
            raise ValueError("invalid checkpoint/chunk settings")
        self.chunk_frames = max(1, int(chunk_seconds * RATE))
        self.max_frames = state["max_seconds"] * RATE
        self.frames = sum(row["frames"] for row in state["segments"])
        self.active_frames = 0
        self.stream = None
        self.last_checkpoint = clock()
        self.checkpoint_seconds = checkpoint_seconds

    def _open(self):
        path = _chunk_path(self.folder, len(self.state["segments"]), "part.pcm")
        self.stream = path.open("xb", buffering=0)
        self.active_frames = 0
        self.state.update(active_file=f"chunks/{len(self.state['segments']):06d}.part.pcm", active_frames=0)

    def append(self, data):
        if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) > MAX_BLOCK_BYTES or len(data) % SAMPLE_WIDTH:
            raise MediaError("入力音声のブロック形式を確認できません。")
        view = memoryview(data)
        while view and self.frames < self.max_frames:
            if self.stream is None:
                self._open()
            count = min(len(view) // SAMPLE_WIDTH, self.chunk_frames - self.active_frames, self.max_frames - self.frames)
            payload = view[:count * SAMPLE_WIDTH]
            while payload:
                written = self.stream.write(payload)
                if not written:
                    raise OSError("short PCM write")
                payload = payload[written:]
            self.frames += count
            self.active_frames += count
            view = view[count * SAMPLE_WIDTH:]
            self.state.update(total_frames=self.frames, active_frames=self.active_frames)
            if self.active_frames == self.chunk_frames:
                self._seal()
        if self.clock() - self.last_checkpoint >= self.checkpoint_seconds:
            self.checkpoint()
        return self.frames >= self.max_frames

    def checkpoint(self):
        if self.stream:
            self.stream.flush()
            os.fsync(self.stream.fileno())
        self.state.update(total_frames=self.frames, saved_frames=self.frames, last_saved_at=now_text(), heartbeat=time.time())
        atomic_json(self.folder / "recording.json", self.state)
        self.last_checkpoint = self.clock()

    def _seal(self):
        if self.stream:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            self.stream = None
        _seal_part(self.folder, self.state, len(self.state["segments"]))
        self.active_frames = 0
        self.frames = self.state["total_frames"]
        self.last_checkpoint = self.clock()

    def finish(self):
        if self.stream:
            try:
                self.stream.flush()
                os.fsync(self.stream.fileno())
            finally:
                self.stream.close()
                self.stream = None
        recover_files(self.folder, self.state)
        self.frames = self.state["total_frames"]
        self.active_frames = 0

    def seal_pause(self):
        """Seal just the current span; prior immutable chunks need no reread."""
        if self.stream is not None:
            self._seal()
        else:
            self.checkpoint()


@contextmanager
def keep_awake(enabled):
    kernel = None
    if enabled and os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        kernel.SetThreadExecutionState.restype = ctypes.c_uint32
        if not kernel.SetThreadExecutionState(0x80000000 | 0x00000001):
            raise MediaError("録音中のスリープ抑止を設定できません。Windowsの電源設定を確認してください。")
    try:
        yield
    finally:
        if kernel is not None:
            kernel.SetThreadExecutionState(0x80000000)


def import_memo(root, folder, state):
    if not state.get("segments"):
        return
    try:
        from app_core.audio_memo_service import AudioMemoStore
        memo = AudioMemoStore(root).import_long_recording(state["id"])
        state.update(memo_id=memo["id"], import_error="")
    except Exception as exc:
        state["import_error"] = "音声は保存済みですがメモへの取り込みを完了できませんでした。履歴の復旧を再試行してください。（" + type(exc).__name__ + "）"
    atomic_json(Path(folder) / "recording.json", state)


class _Interrupted(MediaError):
    pass


class _RequestedStop(Exception):
    pass


def run(folder, source_factory=None, owner_factory=None, clock=time.monotonic, wall_clock=time.time, sleeper=time.sleep):
    folder = Path(folder).resolve()
    store = LongRecordingStore(folder.parents[2])
    if store.folder(folder.name).resolve() != folder:
        raise MediaError("録音プロセスの保存先が不正です。")
    with store.worker_guard():
        state = store._read(folder.name)
        if state["status"] not in ACTIVE:
            return state  # A recovery/stop won the startup race; never reopen mic.
        source = owner = writer = awake = None
        blocks = queue.Queue(maxsize=QUEUE_BLOCKS)
        paused = threading.Event()
        closing = threading.Event()
        fatal = []
        last_callback = [clock()]

        def callback(indata, frames, timing, status):
            if closing.is_set() or paused.is_set():
                return
            if status:
                fatal[:] = ["入力音声の欠落を検出したため停止しました。保存済み音声は保持しています。"]
                return
            if type(frames) is not int or frames <= 0 or len(indata) != frames * SAMPLE_WIDTH or len(indata) > MAX_BLOCK_BYTES:
                fatal[:] = ["入力音声の形式を確認できないため停止しました。"]
                return
            last_callback[0] = clock()
            try:
                blocks.put_nowait(bytes(indata))
            except queue.Full:
                fatal[:] = ["音声の保存が追い付かず入力欠落を検出したため停止しました。"]

        final_status, final_error = "stopped", ""
        started = False

        def set_awake(enabled):
            nonlocal awake
            if awake is not None:
                awake.__exit__(None, None, None)
                awake = None
            if enabled and state["prevent_sleep"]:
                guard = keep_awake(True)
                guard.__enter__()
                awake = guard

        def control_state():
            path = folder / "control.json"
            not_link(path)
            value = read_json(path, {})
            if not isinstance(value, dict) or value.get("id") != state["id"] or value.get("desired") not in {"recording", "paused", "stopped"}:
                raise MediaError("録音操作の状態を確認できません。保存済み音声を保持して停止します。")
            return value["desired"]

        try:
            try:
                owner = (owner_factory or (lambda value: WindowsProcess(value["pid"], value["creation_time"])))(state["owner"])
            except MediaError as exc:
                raise _Interrupted("アプリ本体の所有情報が変わったため、録音を開始しませんでした。") from exc
            if not owner.alive():
                raise _Interrupted("アプリ本体が終了したため、録音を開始しませんでした。")
            state.update(worker=process_identity(), heartbeat=wall_clock(), status="starting")
            atomic_json(folder / "recording.json", state)
            writer = PCMWriter(folder, state, clock=clock)
            desired = control_state()
            if desired == "stopped":
                raise _RequestedStop()
            if desired == "paused":
                paused.set()
                state["status"] = "paused"
            else:
                source = (source_factory or NativeInput)(callback, state["device"])
                set_awake(True)
                source.start()
                last_callback[0] = clock()
                started = True
                state["status"] = "recording"
            state["heartbeat"] = wall_clock()
            atomic_json(folder / "recording.json", state)
            last_poll, last_wall, last_status = clock(), wall_clock(), clock()
            while True:
                current, wall = clock(), wall_clock()
                if current - last_poll > 5 or wall - last_wall > 5 or wall < last_wall - 1:
                    final_status, final_error = "interrupted", "PCのスリープまたは処理の中断を検出して停止しました。空白時間は音声へ補完しません。"
                    break
                last_poll, last_wall = current, wall
                if not owner.alive():
                    final_status, final_error = "interrupted", "アプリ本体が終了したため録音を停止し、保存済み音声を保持しました。"
                    break
                if fatal:
                    final_status, final_error = "interrupted", fatal[0]
                    break
                desired = control_state()
                if desired == "stopped":
                    break
                if desired == "paused" and not paused.is_set():
                    paused.set()
                    source.stop()
                    set_awake(False)
                    while not blocks.empty():
                        if writer.append(blocks.get_nowait()):
                            final_status = "done"
                    writer.seal_pause()
                    state.update(status="paused", level=0.0)
                    state["events"].append({"kind": "pause", "at": now_text(), "frame": writer.frames})
                    atomic_json(folder / "recording.json", state)
                    last_poll, last_wall = clock(), wall_clock()
                elif desired == "recording" and paused.is_set():
                    if source is None:
                        source = (source_factory or NativeInput)(callback, state["device"])
                    set_awake(True)
                    paused.clear()
                    last_callback[0] = clock()
                    source.start()
                    last_callback[0] = clock()
                    started = True
                    state["status"] = "recording"
                    state["events"].append({"kind": "resume", "at": now_text(), "frame": writer.frames})
                    atomic_json(folder / "recording.json", state)
                    last_poll, last_wall = clock(), wall_clock()
                if writer.frames >= writer.max_frames:
                    final_status = "done"
                    break
                if shutil.disk_usage(store.root).free < RESERVE_BYTES:
                    final_status, final_error = "interrupted", "ディスクの空き容量が少ないため録音を停止しました。保存済み音声は保持しています。"
                    break
                if not paused.is_set():
                    if not source.active or current - last_callback[0] > 3:
                        final_status, final_error = "interrupted", "マイクの切断または音声入力の途絶を検出して停止しました。"
                        break
                    try:
                        data = blocks.get(timeout=0.05)
                    except queue.Empty:
                        data = None
                    if data is not None:
                        if writer.append(data):
                            final_status = "done"
                            break
                        samples = array("h", data)
                        state["level"] = min(1.0, max((abs(value) for value in samples), default=0) / 32768)
                else:
                    sleeper(0.05)
                if clock() - writer.last_checkpoint >= CHECKPOINT_SECONDS:
                    writer.checkpoint()
                if clock() - last_status >= 1:
                    state["heartbeat"] = wall_clock()
                    atomic_json(folder / "recording.json", state)
                    last_status = clock()
        except _RequestedStop:
            final_status = "stopped"
        except Exception as exc:
            final_status = "interrupted" if started or isinstance(exc, _Interrupted) else "failed"
            final_error = str(exc) if isinstance(exc, MediaError) else "録音処理に失敗しました。原音は保持しています。（" + type(exc).__name__ + "）"
        finally:
            closing.set()
            if source is not None:
                try:
                    source.stop()
                except Exception:
                    pass
                try:
                    source.close()
                except Exception:
                    pass
            set_awake(False)
            if writer is not None:
                try:
                    while not blocks.empty() and writer.frames < writer.max_frames:
                        writer.append(blocks.get_nowait())
                    writer.finish()
                except Exception as exc:
                    final_status = "interrupted"
                    final_error = "途中の音声を確定できませんでした。原音は保持しています。復旧を実行してください。（" + type(exc).__name__ + "）"
            if owner is not None:
                owner.close()
            state.update(status=final_status, error=final_error, level=0.0, heartbeat=wall_clock(), finished_at=now_text(), finished_epoch=wall_clock())
            try:
                atomic_json(folder / "recording.json", state)
                import_memo(store.root, folder, state)
            except OSError:
                print("Recording final state could not be saved; raw chunks/partials were retained.", file=sys.stderr, flush=True)
        return state


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "devices":
        try:
            print(json.dumps(query_devices(), ensure_ascii=False))
        except Exception:
            print("Input device query failed.", file=sys.stderr)
            raise SystemExit(1)
    elif len(sys.argv) == 2:
        run(sys.argv[1])
    else:
        raise SystemExit("Expected a recording folder or devices query")
