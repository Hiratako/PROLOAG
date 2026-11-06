from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable


READY = "ready"
EXITED = "exited"
TIMEOUT = "timeout"


def health_url(app_url: str) -> str:
    return f"{app_url.rstrip('/')}/_stcore/health"


def is_ready(app_url: str, timeout: float = 0.5) -> bool:
    """Return true only after Streamlit's local health endpoint responds."""
    try:
        with urllib.request.urlopen(health_url(app_url), timeout=timeout) as response:
            body = response.read(32).strip().lower()
            return response.status == 200 and body in {b"ok", b"healthy"}
    except (OSError, urllib.error.URLError, ValueError):
        return False


def wait_until_ready(
    process: Any,
    app_url: str,
    *,
    timeout_seconds: float = 90.0,
    poll_interval: float = 0.2,
    ready_check: Callable[[str], bool] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str:
    ready_check_fn = ready_check if ready_check is not None else is_ready
    monotonic_fn = monotonic if monotonic is not None else time.monotonic
    sleep_fn = sleep if sleep is not None else time.sleep
    started = monotonic_fn()
    next_message = 5.0
    while True:
        if process.poll() is not None:
            return EXITED
        if ready_check_fn(app_url):
            return READY
        elapsed = monotonic_fn() - started
        if elapsed >= timeout_seconds:
            return TIMEOUT
        if elapsed >= next_message:
            print(f"  Still preparing... {int(elapsed)}s elapsed", flush=True)
            next_message += 5.0
        sleep_fn(min(poll_interval, max(timeout_seconds - elapsed, 0.0)))


def stop_process(process: Any, timeout_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)


def streamlit_command(python_executable: str, app_path: Path, app_url: str) -> list[str]:
    from urllib.parse import urlsplit

    parsed = urlsplit(app_url)
    port = parsed.port or 8502
    host = parsed.hostname or "localhost"
    return [
        python_executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--global.developmentMode",
        "false",
    ]


def open_browser(app_url: str, browser_opener: Callable[[str], Any]) -> None:
    try:
        opened = browser_opener(app_url)
        if opened is False:
            print(f"Could not open the browser automatically. Please open this URL manually: {app_url}", flush=True)
    except (OSError, webbrowser.Error) as exc:
        print(f"Could not open the browser automatically: {exc}", flush=True)
        print(f"Please open this URL manually: {app_url}", flush=True)


def launch(
    app_path: str | Path,
    app_url: str,
    *,
    python_executable: str = sys.executable,
    timeout_seconds: float = 90.0,
    existing_ready_check: Callable[[str], bool] | None = None,
    browser_opener: Callable[[str], Any] | None = None,
    popen_factory: Callable[..., Any] | None = None,
    waiter: Callable[..., str] | None = None,
) -> int:
    """Start Streamlit first and open the browser only when it is reachable."""
    main_path = Path(app_path).resolve()
    if not main_path.is_file():
        print(f"Application file not found: {main_path}", flush=True)
        return 2

    existing_ready_check_fn = existing_ready_check if existing_ready_check is not None else is_ready
    browser_opener_fn = browser_opener if browser_opener is not None else webbrowser.open
    popen_factory_fn = popen_factory if popen_factory is not None else subprocess.Popen
    waiter_fn = waiter if waiter is not None else wait_until_ready

    if existing_ready_check_fn(app_url):
        print("PROLOAG is already running. Opening the browser.", flush=True)
        open_browser(app_url, browser_opener_fn)
        return 0

    print("Preparing PROLOAG. The browser will open when the app is ready...", flush=True)
    started = time.monotonic()
    try:
        process = popen_factory_fn(
            streamlit_command(python_executable, main_path, app_url),
            cwd=str(main_path.parent),
        )
    except OSError as exc:
        print(f"Could not start Streamlit: {exc}", flush=True)
        return 1
    try:
        status = waiter_fn(process, app_url, timeout_seconds=timeout_seconds)
        if status == EXITED:
            code = process.poll()
            print(f"Streamlit exited before it was ready (exit code: {code}).", flush=True)
            return int(code) if isinstance(code, int) and code != 0 else 1
        if status == TIMEOUT:
            print(f"Startup was not confirmed within {timeout_seconds:g} seconds. The browser has not been opened.", flush=True)
            stop_process(process)
            return 1
        if status != READY:
            print(f"Unknown startup status: {status}", flush=True)
            stop_process(process)
            return 1

        elapsed = time.monotonic() - started
        print(f"Ready ({elapsed:.1f}s). Opening the browser.", flush=True)
        open_browser(app_url, browser_opener_fn)
        return_code = int(process.wait() or 0)
        if return_code in {3221225786, -1073741510}:
            return 130
        return return_code
    except KeyboardInterrupt:
        print("\nShutdown requested. Stopping Streamlit.", flush=True)
        stop_process(process)
        return 130


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start PROLOAG and open it after readiness.")
    parser.add_argument("--app", required=True, help="Path to main.py")
    parser.add_argument("--url", default="http://localhost:8502/")
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return launch(args.app, args.url, timeout_seconds=max(args.timeout, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
