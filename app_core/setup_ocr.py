"""Explicit OCR installation with dependencies isolated from Streamlit and ASR."""

from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / f".deps-ocr{sys.version_info.major}{sys.version_info.minor}"
    subprocess.run(
        [
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
            str(root / "requirements-ocr.txt"),
        ],
        check=True,
    )
    print("Installed OCR engine:", target)
    print("Recognition models download only when OCR is started.")


if __name__ == "__main__":
    main()
