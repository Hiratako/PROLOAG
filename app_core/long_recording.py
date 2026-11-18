"""Process-owned native recordings, separate from Streamlit sessions/JSON locks."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from uuid import uuid4

from app_core.media_conversion import MediaError, _process_options
from app_core.transcription import atomic_json, file_lock, read_json


RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
MAX_SECONDS = 10800
ACTIVE = {"starting", "recording", "paused", "stopping"}
TERMINAL = {"stopped", "done", "interrupted", "failed"}
BLOCKING = ACTIVE | {"unavailable"}
RESERVE_BYTES = 64 * 1024**2
_ID = re.compile(r"[0-9a-f]{32}")
_mutex = threading.RLock()


def now_text():
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def dependency_dir(root):
    return Path(root) / f".deps-record{sys.version_info.major}{sys.version_info.minor}"


def engine_available(root):
    # Never import sounddevice or load PortAudio in the Streamlit process.
    return (dependency_dir(root) / "sounddevice.py").is_file()


def worker_env(root):
    env = os.environ.copy()
    paths = [str(dependency_dir(root)), str(root)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env.update(PYTHONPATH=os.pathsep.join(paths), PYTHONNOUSERSITE="1",
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return env


def not_link(path):
    try:
        value = Path(path).lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
        raise MediaError("録音の保存先にリンクは使用できません。")


class WindowsProcess:
    """A process HANDLE plus creation identity, never os.kill(pid, 0)."""
    def __init__(self, pid, creation_time=None):
        if os.name != "nt" or type(pid) is not int or pid <= 0:
            raise MediaError("PC録音はWindows版で利用できます。")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel, self.handle, self.pid = kernel, kernel.OpenProcess(0x1000 | 0x100000, False, pid), pid
        if not self.handle:
            raise MediaError("録音の所有プロセスを確認できません。")
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(self.handle, *(ctypes.byref(value) for value in times)):
            self.close()
            raise MediaError("録音の所有プロセスを確認できません。")
        self.creation_time = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        if creation_time is not None and self.creation_time != str(creation_time):
            self.close()
            raise MediaError("録音の所有プロセスが変わっています。")

    def alive(self):
        return bool(self.handle) and self.kernel.WaitForSingleObject(self.handle, 0) == 0x102

    def close(self):
        if getattr(self, "handle", None):
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def process_identity(pid=None):
    process = WindowsProcess(os.getpid() if pid is None else pid)
    try:
        return {"pid": process.pid, "creation_time": process.creation_time}
    finally:
        process.close()


def process_alive(owner):
    try:
        process = WindowsProcess(owner["pid"], owner["creation_time"])
        try:
            return process.alive()
        finally:
            process.close()
    except (MediaError, KeyError, TypeError, OSError):
        return False


class LongRecordingStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / "transcription_data" / "recordings"

    def _safe_base(self):
        for path in (self.root, self.root / "transcription_data", self.base):
            not_link(path)
        if not self.base.resolve().is_relative_to(self.root):
            raise MediaError("録音の保存先を確認してください。")

    def folder(self, recording_id):
        if not isinstance(recording_id, str) or not _ID.fullmatch(recording_id):
            raise MediaError("録音IDが不正です。")
        self._safe_base()
        folder = self.base / recording_id
        not_link(folder)
        if folder.resolve().parent != self.base.resolve():
            raise MediaError("録音の保存先を確認してください。")
        for name in ("recording.json", "control.json", "chunks", "worker.log"):
            not_link(folder / name)
        return folder

    @contextmanager
    def _catalog(self):
        self._safe_base()
        self.base.mkdir(parents=True, exist_ok=True)
        path = self.base / ".catalog.lock"
        not_link(path)
        with _mutex:
            deadline = time.monotonic() + 5
            while True:
                lock = file_lock(path, blocking=False)
                try:
                    lock.__enter__()
                    break
                except OSError as exc:
                    if time.monotonic() > deadline:
                        raise MediaError("別の録音操作を待っています。少し待って再試行してください。") from exc
                    time.sleep(0.025)
            try:
                yield
            finally:
                lock.__exit__(None, None, None)

    @contextmanager
    def worker_guard(self):
        self._safe_base()
        path = self.base / ".worker.lock"
        not_link(path)
        lock = file_lock(path, blocking=False)
        try:
            lock.__enter__()
        except OSError as exc:
            raise MediaError("このアプリでは既に録音中、または録音を復旧中です。") from exc
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def _read(self, recording_id):
        path = self.folder(recording_id) / "recording.json"
        try:
            if path.stat().st_size > 1024**2:
                raise ValueError("oversized manifest")
            state = read_json(path)
            if (not isinstance(state, dict) or state.get("schema_version") != 1 or state.get("id") != recording_id
                    or state.get("status") not in ACTIVE | TERMINAL or state.get("rate") != RATE
                    or state.get("channels") != CHANNELS or state.get("sample_width") != SAMPLE_WIDTH
                    or type(state.get("max_seconds")) is not int or not 1 <= state["max_seconds"] <= MAX_SECONDS
                    or type(state.get("total_frames")) is not int or not 0 <= state["total_frames"] <= RATE * state["max_seconds"]
                    or type(state.get("saved_frames")) is not int or not 0 <= state["saved_frames"] <= state["total_frames"]
                    or not isinstance(state.get("segments"), list) or len(state["segments"]) > MAX_SECONDS
                    or type(state.get("created")) not in {float, int} or not math.isfinite(state["created"])
                    or not isinstance(state.get("owner"), dict) or type(state["owner"].get("pid")) is not int
                    or not isinstance(state["owner"].get("creation_time"), str)
                    or not isinstance(state.get("events"), list) or not isinstance(state.get("title"), str)):
                raise ValueError("invalid manifest")
            frames = 0
            for index, segment in enumerate(state["segments"]):
                if (not isinstance(segment, dict) or segment.get("index") != index
                        or segment.get("file") != f"chunks/{index:06d}.wav" or type(segment.get("frames")) is not int or segment["frames"] <= 0
                        or not isinstance(segment.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", segment["sha256"])):
                    raise ValueError("invalid segment")
                frames += segment["frames"]
            if frames > state["total_frames"]:
                raise ValueError("invalid frame total")
            return state
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise MediaError("録音履歴を読み取れません。原音を削除せず保存先を確認してください。") from exc

    def _view(self, state):
        result = dict(state)
        if state["status"] in ACTIVE:
            worker = state.get("worker")
            if ((isinstance(worker, dict) and not process_alive(worker))
                    or (not worker and time.time() - state["created"] > 45)):
                result.update(status="interrupted", error="録音プロセスが終了しています。保存済み音声の復旧を実行できます。", needs_recovery=True)
        result["duration"] = state["total_frames"] / RATE
        result["saved_duration"] = state.get("saved_frames", sum(row["frames"] for row in state["segments"])) / RATE
        result["elapsed"] = max(0, state.get("finished_epoch", time.time()) - state["created"])
        result["active"] = result["status"] in BLOCKING
        try:
            result["free_bytes"] = shutil.disk_usage(self.root).free
        except OSError:
            result["free_bytes"] = None
        return result

    def get(self, recording_id):
        return self._view(self._read(recording_id))

    def history(self):
        self._safe_base()
        if not self.base.exists():
            return []
        values = []
        for folder in self.base.iterdir():
            if not _ID.fullmatch(folder.name):
                continue
            try:
                values.append(self.get(folder.name))
            except (MediaError, OSError) as exc:
                values.append({"id": folder.name, "title": "読み取れない録音", "created": 0, "created_at": "",
                               "status": "unavailable", "active": True, "error": str(exc), "duration": 0,
                               "saved_duration": 0, "memo_id": None, "import_error": "", "level": 0})
        return sorted(values, key=lambda value: value["created"], reverse=True)

    def devices(self):
        if not engine_available(self.root):
            raise MediaError("PC録音エンジンが未導入です。録音用セットアップを実行してください。")
        try:
            result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("recording_worker.py")), "devices"],
                                    env=worker_env(self.root), cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                                    encoding="utf-8", **_process_options())
            rows = json.loads(result.stdout) if result.returncode == 0 else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get("id")) is not int
                                                or not isinstance(row.get("name"), str) for row in rows):
                raise ValueError("device query failed")
            return rows
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise MediaError("入力機器を確認できません。Windowsのマイク設定と録音用エンジンを確認してください。") from exc

    def start(self, title="", device=None, max_seconds=MAX_SECONDS, prevent_sleep=True, operation_id=None):
        if (not isinstance(title, str) or len(title) > 200 or "\0" in title or type(max_seconds) is not int
                or not 1 <= max_seconds <= MAX_SECONDS or type(prevent_sleep) is not bool
                or (device is not None and (type(device) is not int or device < 0))):
            raise MediaError("録音のタイトル・入力機器・録音上限を確認してください。")
        operation_id = operation_id or uuid4().hex
        if not isinstance(operation_id, str) or not _ID.fullmatch(operation_id):
            raise MediaError("録音開始の操作IDが不正です。")
        fingerprint = hashlib.sha256(json.dumps([title.strip(), device, max_seconds, prevent_sleep], ensure_ascii=False).encode()).hexdigest()
        with self._catalog():
            rows = self.history()
            old = next((row for row in rows if row.get("operation_id") == operation_id), None)
            if old:
                if old.get("fingerprint") != fingerprint:
                    raise MediaError("同じ録音開始操作の設定が変わっています。新しい操作として確認してください。")
                return old
            if any(row.get("status") == "unavailable" for row in rows):
                raise MediaError("確認できない録音履歴があります。新しい録音の前に保存先を確認してください。")
            if any(self._read(row["id"])["status"] in ACTIVE for row in rows):
                raise MediaError("録音中、または未完了の録音があります。停止・復旧してから開始してください。")
            with self.worker_guard():
                if not engine_available(self.root):
                    raise MediaError("PC録音エンジンが未導入です。録音用セットアップを実行してください。")
                devices = self.devices()
                if device is None:
                    selected = next((row for row in devices if row.get("default")), None) or next(iter(devices), None)
                else:
                    selected = next((row for row in devices if row["id"] == device), None)
                if selected is None:
                    raise MediaError("使用できるWASAPI入力機器がありません。マイクを接続して確認してください。")
                if shutil.disk_usage(self.root).free < max_seconds * RATE * SAMPLE_WIDTH * 2 + RESERVE_BYTES:
                    raise MediaError("録音とメモ保存に必要なディスク空き容量が不足しています。")
                owner = process_identity()
                recording_id = uuid4().hex
                folder = self.folder(recording_id)
                (folder / "chunks").mkdir(parents=True)
                created = time.time()
                state = {"schema_version": 1, "id": recording_id, "operation_id": operation_id, "fingerprint": fingerprint,
                         "title": title.strip() or "録音 " + datetime.now().strftime("%Y-%m-%d %H:%M"),
                         "created": created, "created_at": now_text(), "status": "starting", "rate": RATE,
                         "channels": CHANNELS, "sample_width": SAMPLE_WIDTH, "max_seconds": max_seconds,
                         "prevent_sleep": prevent_sleep, "device": selected, "owner": owner, "worker": None,
                         "total_frames": 0, "saved_frames": 0, "segments": [], "events": [], "heartbeat": created,
                         "level": 0.0, "last_saved_at": "", "error": "", "memo_id": None, "import_error": ""}
                atomic_json(folder / "recording.json", state)
                atomic_json(folder / "control.json", {"id": recording_id, "revision": 0, "desired": "recording"})
            try:
                with (folder / "worker.log").open("xb") as log:
                    process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("recording_worker.py")), str(folder)],
                                               env=worker_env(self.root), cwd=self.root, stdin=subprocess.DEVNULL,
                                               stdout=log, stderr=log, **_process_options())
                threading.Thread(target=process.wait, daemon=True).start()
            except OSError as exc:
                state.update(status="failed", error="録音プロセスを起動できません。保存先・実行環境を確認してください。")
                atomic_json(folder / "recording.json", state)
                raise MediaError(state["error"]) from exc
            return self._view(state)

    def _control(self, recording_id, desired):
        with self._catalog():
            state = self._read(recording_id)
            if state["status"] in TERMINAL:
                return self._view(state)
            path = self.folder(recording_id) / "control.json"
            old = read_json(path, {"id": recording_id, "revision": 0, "desired": state["status"]})
            if not isinstance(old, dict) or old.get("id") != recording_id or type(old.get("revision")) is not int:
                raise MediaError("録音操作の状態を確認できません。原音は保持しています。")
            if old.get("desired") == "stopped" and desired != "stopped":
                raise MediaError("録音の停止を確認中です。停止後に新しい録音を開始してください。")
            if old.get("desired") != desired:
                atomic_json(path, {"id": recording_id, "revision": old["revision"] + 1, "desired": desired, "requested_at": now_text()})
            view = self._view(state)
            if desired == "stopped":
                view.update(status="stopping", active=True)
            return view

    def pause(self, recording_id):
        return self._control(recording_id, "paused")

    def resume(self, recording_id):
        return self._control(recording_id, "recording")

    def stop(self, recording_id):
        return self._control(recording_id, "stopped")

    def recover(self, recording_id):
        from app_core.recording_worker import import_memo, recover_files
        with self._catalog(), self.worker_guard():
            state = self._read(recording_id)
            recover_files(self.folder(recording_id), state)
            if state["status"] in ACTIVE:
                state.update(status="interrupted", level=0.0, error="録音が中断しました。保存できた音声を復旧しました。",
                             finished_epoch=time.time(), finished_at=now_text())
            state["heartbeat"] = time.time()
            atomic_json(self.folder(recording_id) / "recording.json", state)
            import_memo(self.root, self.folder(recording_id), state)
            return self._view(state)
