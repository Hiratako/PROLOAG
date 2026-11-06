"""Explicit optional installation, using the same Python as the app launcher."""

from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / f".deps-asr{sys.version_info.major}{sys.version_info.minor}"
    command = [
        sys.executable,
        "-B",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-warn-script-location",
        "--target",
        str(target),
        "--upgrade",
        "-r",
        str(root / "requirements-transcription.txt"),
    ]
    subprocess.run(command, check=True)
    print("Installed transcription engine:", target)
    print("Models download only when you start transcription in the app.")


if __name__ == "__main__":
    main()
