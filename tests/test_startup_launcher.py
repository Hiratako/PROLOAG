from __future__ import annotations

import ast
import base64
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app_core import startup_launcher


ROOT = Path(__file__).resolve().parents[1]


class FakeProcess:
    def __init__(self, *, poll_result=None, wait_result=0, wait_error=None) -> None:
        self.poll_result = poll_result
        self.wait_result = wait_result
        self.wait_error = wait_error
        self.returncode = poll_result
        self.poll_calls = 0
        self.wait_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        self.poll_calls += 1
        return self.poll_result

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.wait_error is not None:
            error = self.wait_error
            self.wait_error = None
            raise error
        return self.wait_result

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


@contextmanager
def health_server(*, status=200, body=b"ok"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/_stcore/health":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StartupReadinessTests(unittest.TestCase):
    def test_launcher_message_literals_are_english(self) -> None:
        source = Path(startup_launcher.__file__).read_text(encoding="utf-8-sig")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertTrue(node.value.isascii(), repr(node.value))

    def test_ready_feedback_is_english(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="proloag_startup_") as temporary:
            main_py = Path(temporary) / "main.py"
            main_py.write_text("# synthetic app\n", encoding="utf-8")
            with redirect_stdout(output), patch.object(startup_launcher.time, "monotonic", side_effect=[0.0, 4.2]):
                result = startup_launcher.launch(
                    main_py, "http://localhost:19009/",
                    existing_ready_check=lambda _url: False,
                    browser_opener=lambda _url: True,
                    popen_factory=lambda *_args, **_kwargs: FakeProcess(),
                    waiter=lambda *_args, **_kwargs: startup_launcher.READY,
                )
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().splitlines(), [
            "Preparing PROLOAG. The browser will open when the app is ready...",
            "Ready (4.2s). Opening the browser.",
        ])

    def test_health_url_uses_streamlit_health_endpoint(self) -> None:
        self.assertEqual(
            startup_launcher.health_url("http://localhost:19001/"),
            "http://localhost:19001/_stcore/health",
        )
        self.assertEqual(
            startup_launcher.health_url("http://localhost:19001"),
            "http://localhost:19001/_stcore/health",
        )

    def test_is_ready_accepts_real_local_health_response(self) -> None:
        with health_server(body=b"ok") as app_url:
            self.assertTrue(startup_launcher.is_ready(app_url, timeout=1.0))

        with health_server(body=b"healthy") as app_url:
            self.assertTrue(startup_launcher.is_ready(app_url, timeout=1.0))

    def test_is_ready_rejects_error_or_unexpected_health_response(self) -> None:
        with health_server(status=503, body=b"ok") as app_url:
            self.assertFalse(startup_launcher.is_ready(app_url, timeout=1.0))

        with health_server(body=b"starting") as app_url:
            self.assertFalse(startup_launcher.is_ready(app_url, timeout=1.0))

    def test_wait_until_ready_reports_process_early_exit(self) -> None:
        process = FakeProcess(poll_result=7)
        result = startup_launcher.wait_until_ready(
            process,
            "http://localhost:19002/",
            timeout_seconds=30,
            poll_interval=0,
            ready_check=lambda _url: False,
        )
        self.assertEqual(result, "exited")
        self.assertGreaterEqual(process.poll_calls, 1)

    def test_wait_until_ready_reports_timeout_without_real_wait(self) -> None:
        process = FakeProcess(poll_result=None)
        sleep = Mock()
        result = startup_launcher.wait_until_ready(
            process,
            "http://localhost:19003/",
            timeout_seconds=0,
            poll_interval=0,
            ready_check=lambda _url: False,
            sleep=sleep,
        )
        self.assertEqual(result, "timeout")
        sleep.assert_not_called()

    def test_launch_order_is_server_then_readiness_then_browser(self) -> None:
        events: list[str] = []
        process = FakeProcess(wait_result=0)

        def popen_factory(*args, **kwargs):
            events.append("server")
            self.assertTrue(args or kwargs)
            return process

        def open_browser(url):
            events.append("browser")
            self.assertEqual(url, "http://localhost:19004/")
            return True

        def wait_for_ready(*_args, **_kwargs):
            events.append("health-readiness")
            return "ready"

        original_wait = process.wait

        def recorded_wait(timeout=None):
            events.append("process-wait")
            return original_wait(timeout)

        process.wait = recorded_wait
        with tempfile.TemporaryDirectory(prefix="proloag_startup_") as temporary:
            main_py = Path(temporary) / "main.py"
            main_py.write_text("# synthetic app\n", encoding="utf-8")
            result = startup_launcher.launch(
                main_py,
                "http://localhost:19004/",
                python_executable="synthetic-python",
                browser_opener=open_browser,
                popen_factory=popen_factory,
                existing_ready_check=lambda _url: False,
                waiter=wait_for_ready,
                timeout_seconds=3,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            ["server", "health-readiness", "browser", "process-wait"],
        )

    def test_existing_ready_server_opens_browser_without_duplicate_process(self) -> None:
        opener = Mock(return_value=True)
        popen = Mock(side_effect=AssertionError("must not start a duplicate server"))
        with tempfile.TemporaryDirectory(prefix="proloag_startup_") as temporary:
            main_py = Path(temporary) / "main.py"
            main_py.write_text("# synthetic app\n", encoding="utf-8")
            result = startup_launcher.launch(
                main_py,
                "http://localhost:19005/",
                browser_opener=opener,
                popen_factory=popen,
                existing_ready_check=lambda _url: True,
            )

        self.assertEqual(result, 0)
        opener.assert_called_once_with("http://localhost:19005/")
        popen.assert_not_called()

    def test_launch_does_not_open_browser_when_server_exits_or_times_out(self) -> None:
        for readiness in ("exited", "timeout"):
            with self.subTest(readiness=readiness):
                process = FakeProcess(poll_result=9 if readiness == "exited" else None)
                opener = Mock()
                with tempfile.TemporaryDirectory(prefix="proloag_startup_") as temporary:
                    main_py = Path(temporary) / "main.py"
                    main_py.write_text("# synthetic app\n", encoding="utf-8")
                    result = startup_launcher.launch(
                        main_py,
                        "http://localhost:19006/",
                        browser_opener=opener,
                        popen_factory=lambda *_args, **_kwargs: process,
                        existing_ready_check=lambda _url: False,
                        waiter=lambda *_args, **_kwargs: readiness,
                        timeout_seconds=0,
                    )
                self.assertNotEqual(result, 0)
                opener.assert_not_called()
                if readiness == "timeout":
                    self.assertGreaterEqual(process.terminate_calls, 1)

    def test_keyboard_interrupt_stops_owned_server(self) -> None:
        process = FakeProcess(wait_error=KeyboardInterrupt())
        opener = Mock(return_value=True)
        with tempfile.TemporaryDirectory(prefix="proloag_startup_") as temporary:
            main_py = Path(temporary) / "main.py"
            main_py.write_text("# synthetic app\n", encoding="utf-8")
            result = startup_launcher.launch(
                main_py,
                "http://localhost:19007/",
                browser_opener=opener,
                popen_factory=lambda *_args, **_kwargs: process,
                existing_ready_check=lambda _url: False,
                waiter=lambda *_args, **_kwargs: "ready",
            )

        self.assertNotEqual(result, 0)
        opener.assert_called_once()
        self.assertGreaterEqual(process.terminate_calls, 1)

    def test_launcher_never_changes_json_records(self) -> None:
        with tempfile.TemporaryDirectory(prefix="proloag_startup_data_") as temporary:
            root = Path(temporary)
            main_py = root / "main.py"
            main_py.write_text("# synthetic app\n", encoding="utf-8")
            json_paths = []
            for relative in (
                "study_logs/2026/09/20260905.json",
                "diary_entries/2026/09/20260905.json",
                "settings/user_settings.json",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"sentinel":"unchanged"}\n', encoding="utf-8")
                json_paths.append(path)
            before = {path: file_digest(path) for path in json_paths}

            result = startup_launcher.launch(
                main_py,
                "http://localhost:19008/",
                browser_opener=lambda _url: True,
                popen_factory=Mock(side_effect=AssertionError("unexpected process")),
                existing_ready_check=lambda _url: True,
            )

            self.assertEqual(result, 0)
            self.assertEqual(before, {path: file_digest(path) for path in json_paths})


class StartupPowerShellIntegrationTests(unittest.TestCase):
    scripts = (
        ROOT / "run_app.ps1",
        ROOT / "scripts" / "portable_release" / "run_portable_app.ps1",
    )

    def test_japanese_welcome_lines_are_preserved(self) -> None:
        source = self.scripts[0].read_text(encoding="utf-8-sig")
        portable = self.scripts[1].read_text(encoding="utf-8-sig")
        for text in ("PROLOAGへようこそ！", "今日の記録を、次の物語へ。"):
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            self.assertIn(f'Decode-Utf8Text "{encoded}"', source)
            self.assertIn(text, portable)

    def test_powershell_launchers_delegate_browser_timing_to_python_launcher(self) -> None:
        for path in self.scripts:
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8-sig")
                self.assertIn("startup_launcher.py", source)
                self.assertNotIn("Start-Process $url", source)
                self.assertNotIn('Start-Process "http://localhost:', source)

    def test_powershell_launchers_have_valid_syntax(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is None:
            self.skipTest("PowerShell is not available")

        parser = (
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$env:PROLOAG_PS_PARSE_TARGET, [ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count -gt 0) { $errors | ForEach-Object { "
            "[Console]::Error.WriteLine($_.Message) }; exit 1 }"
        )
        for path in self.scripts:
            with self.subTest(path=path.name):
                completed = subprocess.run(
                    [powershell, "-NoProfile", "-Command", parser],
                    cwd=ROOT,
                    env={**os.environ, "PROLOAG_PS_PARSE_TARGET": str(path)},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=20,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
