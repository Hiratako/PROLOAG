"""Explicit isolated recorder installation; never opens a microphone stream."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4


VERSIONS = {"sounddevice": "0.5.6", "cffi": "2.1.1", "pycparser": "3.12"}


def diagnose(target: Path) -> dict:
    code = (
        "import sys,json,pathlib,importlib.metadata as md; "
        "target=pathlib.Path(sys.argv[1]).resolve(); sys.path.insert(0,str(target)); "
        "import sounddevice,cffi,pycparser; "
        "modules=[sounddevice,cffi,pycparser]; "
        "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(target) for m in modules); "
        "print(json.dumps({'packages':{n:md.version(n) for n in ['sounddevice','cffi','pycparser']},"
        "'portaudio':sounddevice.get_portaudio_version(),'microphone_opened':False}))"
    )
    env = os.environ.copy()
    env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-B", "-s", "-c", code, str(target)], env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    if result.returncode:
        raise RuntimeError("録音用ライブラリの読込確認に失敗しました。" + result.stderr[-1200:])
    info = json.loads(result.stdout.strip().splitlines()[-1])
    if info["packages"] != VERSIONS:
        raise RuntimeError("録音用ライブラリの版が固定定義と異なります。")
    return info


@contextmanager
def installation_guard(root: Path):
    lock = root / "transcription_data/recordings/.worker.lock"
    if not lock.exists():
        yield
        return
    if lock.is_symlink():
        raise RuntimeError("録音のロック保存先を確認してください。")
    with lock.open("r+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("録音中はライブラリを導入しません。停止・保存後に再試行してください。") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--prepared", type=Path, help="Use an already verified isolated runtime (internal).")
    args = parser.parse_args()
    root = args.root.resolve()
    if not (root / "main.py").is_file() or not (root / "app_core").is_dir():
        raise RuntimeError("PROLOAGのアプリフォルダーを指定してください。")
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        raise RuntimeError("アプリと同じPython 3.11または3.12で実行してください。")
    target = root / f".deps-record{sys.version_info.major}{sys.version_info.minor}"
    if target.is_symlink() or target.resolve().parent != root:
        raise RuntimeError("録音用ライブラリの導入先を確認してください。")
    if target.exists():
        print(json.dumps({"status": "already_ready", "target": str(target), **diagnose(target)}, ensure_ascii=False))
        return
    stage = root / (target.name + ".install-" + uuid4().hex)
    if stage.resolve().parent != root or stage.exists():
        raise RuntimeError("導入用の一時フォルダーを確認してください。")
    with installation_guard(root):
        if args.prepared:
            prepared = args.prepared.resolve()
            diagnose(prepared)
            shutil.copytree(prepared, stage)
        else:
            stage.mkdir()
            requirement = Path(__file__).resolve().parents[1] / "requirements-recording.txt"
            command = [sys.executable, "-B", "-m", "pip", "--isolated", "install",
                       "--index-url", "https://pypi.org/simple", "--disable-pip-version-check",
                       "--no-warn-script-location", "--no-input", "--target", str(stage),
                       "-r", str(requirement)]
            subprocess.run(command, check=True)
        info = diagnose(stage)
        if target.exists() or stage.resolve().parent != root or target.resolve().parent != root:
            raise RuntimeError("導入先が変わりました。既存の環境は上書きしません。")
        os.replace(stage, target)
    print(json.dumps({"status": "installed", "target": str(target), **info}, ensure_ascii=False))


if __name__ == "__main__":
    main()
