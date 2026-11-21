"""GPU failure/retry contracts using synthetic PCM and deterministic engines."""

import ctypes
import io
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from app_core import transcription as tr
from app_core import transcription_worker as worker
from app_core.media_conversion import Source, Track, _identity


class Samples:
    def astype(self, dtype):
        return self

    def __truediv__(self, value):
        return self


def segment(text):
    return SimpleNamespace(start=2.1, end=3.0, text=text)


def result(rows):
    return iter(rows), SimpleNamespace(language="ja")


class WorkerGPUFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proloag_asr_gpu_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = tr.TranscriptionStore(self.root)
        self.job_id = "a" * 32
        self.folder = self.store.folder(self.job_id)
        self.folder.mkdir(parents=True)
        self.source = self.root / "synthetic.wav"

    def seed(self, seconds=10, *, requested="auto", model="large-v3"):
        with wave.open(str(self.source), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            remaining = seconds * 16000
            while remaining:
                take = min(remaining, 16000)
                stream.writeframesraw(b"\0\0" * take)
                remaining -= take
        request = {
            "id": self.job_id, "root": str(self.root), "source_path": str(self.source),
            "source_name": self.source.name, "identity": list(_identity(self.source)),
            "source_sha256": tr.file_sha256(self.source), "duration": float(seconds),
            "track": {"index": 0}, "ffmpeg": "synthetic-ffmpeg", "language": "ja",
            "model": model, "device": requested, "hotwords": "",
        }
        tr.atomic_json(self.folder / "request.json", request)
        tr.atomic_json(self.folder / "state.json", {
            "id": self.job_id, "name": self.source.name, "created": time.time(),
            "status": "starting", "progress": 0.0, "error": "",
        })
        return request

    def weights(self, name):
        path = self.root / "synthetic-weights" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def fake_download(self, name, **kwargs):
        path = self.weights(name)
        (path / "config.json").write_bytes(b"{}")
        (path / "model.bin").write_bytes(b"synthetic weights")
        return str(path)

    def run_fake(self, factory, *, selected="cuda", lease=None, downloader=None):
        source = self.source

        class Process:
            returncode = 0

            def __init__(self, command, **kwargs):
                shutil.copyfile(source, command[-1])

            def poll(self):
                return 0

            def wait(self, **kwargs):
                return 0

        faster = ModuleType("faster_whisper")
        faster.__path__ = []
        faster.WhisperModel = factory
        utils = ModuleType("faster_whisper.utils")
        utils.download_model = downloader or self.fake_download
        numpy = ModuleType("numpy")
        numpy.int16, numpy.float32 = "int16", "float32"
        numpy.frombuffer = lambda *args, **kwargs: Samples()
        translate = ModuleType("ctranslate2")
        modules = {"faster_whisper": faster, "faster_whisper.utils": utils,
                   "numpy": numpy, "ctranslate2": translate}
        with patch.dict(sys.modules, modules), \
                patch.object(worker, "prepare_device", return_value=(selected, "", "", lease)), \
                patch.object(worker.subprocess, "Popen", Process), \
                patch.object(worker.threading, "Thread"), \
                patch.object(sys, "stderr", io.StringIO()):
            worker.run(self.folder)
        return self.store.result(self.job_id)

    def test_model_download_failure_guides_retry_without_showing_upstream_details(self):
        self.seed(requested="cpu", model="turbo")
        download = Mock(side_effect=OSError("upstream-specific URL"))
        build = Mock()
        value = self.run_fake(build, selected="cpu", downloader=download)
        self.assertEqual(value["state"]["status"], "failed")
        self.assertIn("認識モデル（turbo）を取得できませんでした", value["state"]["error"])
        self.assertIn("再試行", value["state"]["error"])
        self.assertNotIn("upstream-specific", value["state"]["error"])
        self.assertTrue(download.call_args_list[0].kwargs["local_files_only"])
        self.assertEqual(download.call_count, 2)
        self.assertFalse(download.call_args_list[1].kwargs.get("local_files_only", False))
        build.assert_not_called()

    def test_incomplete_cached_model_is_retried_online_before_initializing(self):
        self.seed(requested="cpu", model="large-v3")
        snapshot = self.weights("large-v3")
        (snapshot / "config.json").write_bytes(b"{}")
        (snapshot / "model.bin").touch()

        def acquire(name, *, local_files_only=False, **kwargs):
            if local_files_only:
                return str(snapshot)
            return self.fake_download(name, **kwargs)

        download = Mock(side_effect=acquire)
        build = Mock(return_value=SimpleNamespace(
            transcribe=lambda *args, **kwargs: result([segment("recognition after retry")])
        ))
        value = self.run_fake(build, selected="cpu", downloader=download)
        self.assertEqual(value["state"]["status"], "done")
        self.assertEqual(download.call_count, 2)
        self.assertTrue(download.call_args_list[0].kwargs["local_files_only"])
        self.assertFalse(download.call_args_list[1].kwargs.get("local_files_only", False))
        self.assertEqual(build.call_args.args[0], str(snapshot))

    def test_gpu_loading_oom_keeps_exact_model_and_uses_cpu(self):
        self.seed()
        builds = []
        lease = Mock()

        def factory(path, **kwargs):
            builds.append((path, kwargs["device"], kwargs["compute_type"]))
            if kwargs["device"] == "cuda":
                raise RuntimeError("CUDA failed with error out of memory")
            return SimpleNamespace(transcribe=lambda *args, **kw: result([segment("CPU result")]))

        value = self.run_fake(factory, lease=lease)
        self.assertEqual(value["state"]["status"], "done")
        path = str(self.weights("large-v3"))
        self.assertEqual(builds, [(path, "cuda", "int8_float16"), (path, "cpu", "int8")])
        self.assertEqual(value["state"]["device"], "cpu")
        self.assertEqual(value["state"]["requested_device"], "auto")
        self.assertEqual(value["state"]["gpu_fallback_reason"], "out_of_memory")
        self.assertTrue(value["state"]["warning"])
        lease.close.assert_called_once()

    def test_midjob_oom_retries_only_failed_window_without_duplicate_segments(self):
        request = self.seed(610)
        gpu_calls, cpu_calls, builds = [], [], []
        unload = Mock()

        def gpu(*args, **kwargs):
            gpu_calls.append(1)
            if len(gpu_calls) == 1:
                return result([segment("committed GPU prefix")])

            def rows():
                yield segment("uncommitted GPU tail")
                raise RuntimeError("CUDA error: out of memory")

            return rows(), SimpleNamespace(language="en")

        def cpu(*args, **kwargs):
            cpu_calls.append(1)
            return result([segment("CPU retry" if len(cpu_calls) == 1 else "CPU final")])

        def factory(path, **kwargs):
            builds.append((path, kwargs["device"]))
            return SimpleNamespace(transcribe=gpu, model=SimpleNamespace(unload_model=unload)) \
                if kwargs["device"] == "cuda" else SimpleNamespace(transcribe=cpu)

        value = self.run_fake(factory)
        self.assertEqual(value["state"]["status"], "done")
        self.assertEqual([row["id"] for row in value["segments"]], [0, 1, 2])
        self.assertEqual([row["original_text"] for row in value["segments"]],
                         ["committed GPU prefix", "CPU retry", "CPU final"])
        self.assertEqual([row["start"] for row in value["segments"]], [2.1, 300.1, 600.1])
        self.assertEqual(len(gpu_calls), 2)
        self.assertEqual(len(cpu_calls), 2)
        path = str(self.weights("large-v3"))
        self.assertEqual(builds, [(path, "cuda"), (path, "cpu")])
        self.assertEqual(value["state"]["device_history"], [
            {"device": "cuda", "from_seconds": 0, "reason": ""},
            {"device": "cpu", "from_seconds": 300, "reason": "out_of_memory"},
        ])
        self.assertEqual(value["state"]["detected_languages"], ["ja"])
        checkpoint = tr.load_checkpoint(self.folder, request)
        tr.checkpoint_prefix(self.folder / "segments.jsonl", checkpoint, exact=True)
        self.assertEqual(checkpoint["completed_seconds"], 610)
        self.assertEqual(checkpoint["segments_count"], 3)
        unload.assert_called_once()

    def test_cpu_retry_failure_preserves_completed_checkpoint(self):
        request = self.seed(610)
        calls = []

        def gpu(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return result([segment("committed")])

            def rows():
                yield segment("discarded GPU tail")
                raise RuntimeError("cuDNN failed with status CUDNN_STATUS_ALLOC_FAILED")

            return rows(), SimpleNamespace(language="ja")

        def factory(path, **kwargs):
            if kwargs["device"] == "cpu":
                raise RuntimeError("CPU model load failed")
            return SimpleNamespace(transcribe=gpu)

        value = self.run_fake(factory)
        self.assertEqual(value["state"]["status"], "failed")
        self.assertEqual(value["state"]["device"], "cpu")
        self.assertEqual([row["original_text"] for row in value["segments"]], ["committed"])
        checkpoint = tr.load_checkpoint(self.folder, request)
        self.assertEqual(checkpoint["completed_seconds"], 300)
        tr.checkpoint_prefix(self.folder / "segments.jsonl", checkpoint, exact=True)

    def test_unrelated_model_and_inference_errors_are_not_hidden_by_cpu_retry(self):
        for phase in ("load", "inference"):
            with self.subTest(phase=phase):
                self.seed()
                builds = []

                def factory(path, **kwargs):
                    builds.append(kwargs["device"])
                    if phase == "load":
                        raise RuntimeError("Unable to open model.bin")

                    def transcribe(*args, **kw):
                        raise RuntimeError("invalid vocabulary")

                    return SimpleNamespace(transcribe=transcribe)

                value = self.run_fake(factory)
                self.assertEqual(value["state"]["status"], "failed")
                self.assertEqual(builds, ["cuda"])

    def test_explicit_cpu_does_not_retry_a_gpu_named_error(self):
        self.seed(requested="cpu", model="turbo")
        builds = []

        def factory(path, **kwargs):
            builds.append(kwargs["device"])
            raise RuntimeError("CUDA failed with error out of memory")

        value = self.run_fake(factory, selected="cpu")
        self.assertEqual(value["state"]["status"], "failed")
        self.assertEqual(builds, ["cpu"])

    def test_disk_error_with_cuda_text_does_not_trigger_cpu_retry(self):
        self.seed()
        builds = []
        original_fsync = worker.os.fsync
        transcript = self.folder / "segments.jsonl"

        def fsync(descriptor):
            if transcript.exists():
                current, expected = worker.os.fstat(descriptor), transcript.stat()
                if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
                    raise OSError("CUDA failed path: cannot save transcript on disk")
            return original_fsync(descriptor)

        def factory(path, **kwargs):
            builds.append(kwargs["device"])
            return SimpleNamespace(transcribe=lambda *args, **kw: result([segment("saved text")]))

        with patch.object(worker.os, "fsync", side_effect=fsync):
            value = self.run_fake(factory)
        self.assertEqual(value["state"]["status"], "failed")
        self.assertIn("cannot save transcript", value["state"]["error"])
        self.assertEqual(builds, ["cuda"])

    def test_unknown_model_is_rejected_before_decoding_or_loading(self):
        self.seed(model="unapproved/model")
        factory = Mock(side_effect=AssertionError("unexpected engine load"))
        value = self.run_fake(factory)
        self.assertEqual(value["state"]["status"], "failed")
        self.assertIn("認識モデル", value["state"]["error"])
        factory.assert_not_called()
        self.assertFalse((self.folder / "audio.wav").exists())

    def test_cancelled_failed_window_does_not_start_cpu_retry(self):
        self.seed()
        builds = []

        def transcribe(*args, **kwargs):
            (self.folder / "cancel").touch()
            raise RuntimeError("CUDA error: out of memory")

        def factory(path, **kwargs):
            builds.append(kwargs["device"])
            return SimpleNamespace(transcribe=transcribe)

        value = self.run_fake(factory)
        self.assertEqual(value["state"]["status"], "cancelled")
        self.assertEqual(builds, ["cuda"])


class DeviceSelectionTests(unittest.TestCase):
    def test_cpu_selection_never_queries_cuda(self):
        runtime = Mock()
        self.assertEqual(worker.prepare_device("cpu", Path("unused"), runtime), ("cpu", "", "", None))
        runtime.get_cuda_device_count.assert_not_called()

    def test_missing_gpu_is_a_reported_cpu_choice(self):
        value = worker.prepare_device("auto", Path("unused"), SimpleNamespace(get_cuda_device_count=lambda: 0))
        self.assertEqual(value[0], "cpu")
        self.assertEqual(value[2], "not_available")
        self.assertTrue(value[1])

    @unittest.skipUnless(sys.platform == "win32", "Windows DLL discovery contract")
    def test_app_local_dll_directory_lease_is_kept_alive(self):
        with tempfile.TemporaryDirectory(prefix="proloag_cuda_lease_") as location:
            root = Path(location).resolve()
            directory = root / "tools/asr-cuda/bin"
            directory.mkdir(parents=True)
            (directory / "cublas64_12.dll").touch()
            lease = Mock()
            with patch.dict(worker.os.environ, {"CUDA_PATH": "", "PATH": ""}), \
                    patch.object(worker.os, "add_dll_directory", return_value=lease) as add, \
                    patch.object(ctypes, "WinDLL") as load:
                value = worker.prepare_device("auto", root, SimpleNamespace(get_cuda_device_count=lambda: 1))
            self.assertEqual(value, ("cuda", "", "", lease))
            add.assert_called_once_with(str(directory))
            load.assert_called_once_with(str(directory / "cublas64_12.dll"))
            lease.close.assert_not_called()
            lease.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows DLL discovery contract")
    def test_cuda_path_is_registered_and_loaded_by_absolute_path(self):
        with tempfile.TemporaryDirectory(prefix="proloag_cuda_path_") as location:
            root = Path(location).resolve()
            cuda_root = root / "cuda"
            directory = cuda_root / "bin"
            directory.mkdir(parents=True)
            (directory / "cublas64_12.dll").touch()
            lease = Mock()
            with patch.dict(worker.os.environ, {"CUDA_PATH": str(cuda_root), "PATH": ""}), \
                    patch.object(worker.os, "add_dll_directory", return_value=lease) as add, \
                    patch.object(ctypes, "WinDLL") as load:
                value = worker.prepare_device("auto", root, SimpleNamespace(get_cuda_device_count=lambda: 1))
            self.assertEqual(value, ("cuda", "", "", lease))
            add.assert_called_once_with(str(directory))
            load.assert_called_once_with(str(directory / "cublas64_12.dll"))
            lease.close.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows DLL discovery contract")
    def test_empty_app_local_directory_allows_absolute_path_candidate(self):
        with tempfile.TemporaryDirectory(prefix="proloag_cuda_path_") as location:
            root = Path(location).resolve()
            (root / "tools/asr-cuda/bin").mkdir(parents=True)
            directory = root / "installed-cuda-bin"
            directory.mkdir()
            (directory / "cublas64_12.dll").touch()
            lease = Mock()
            search_path = "relative-entry" + worker.os.pathsep + str(directory)
            with patch.dict(worker.os.environ, {"CUDA_PATH": "relative-cuda", "PATH": search_path}), \
                    patch.object(worker.os, "add_dll_directory", return_value=lease) as add, \
                    patch.object(ctypes, "WinDLL") as load:
                value = worker.prepare_device("auto", root, SimpleNamespace(get_cuda_device_count=lambda: 1))
            self.assertEqual(value, ("cuda", "", "", lease))
            add.assert_called_once_with(str(directory))
            load.assert_called_once_with(str(directory / "cublas64_12.dll"))
            lease.close.assert_not_called()

    def test_gpu_error_classifier_rejects_unrelated_errors(self):
        for error in (RuntimeError("disk full"), OSError("Permission denied"),
                      ValueError("invalid model"), MemoryError("out of memory")):
            with self.subTest(error=str(error)):
                self.assertIsNone(worker.gpu_failure_kind(error))
        self.assertEqual(worker.gpu_failure_kind(RuntimeError("This CTranslate2 package was not compiled with CUDA support")), "runtime_failure")
        self.assertEqual(worker.gpu_failure_kind(ValueError("Requested float16 compute type, but the target device or backend do not support efficient float16 computation")), "unsupported_compute_type")


class ModelRegistryTests(unittest.TestCase):
    def test_new_models_keep_legacy_modes_and_store_requested_device_independently(self):
        self.assertEqual(tr.MODELS, {"standard": "small", "accurate": "medium", "precise": "large-v3", "fast": "turbo"})
        with tempfile.TemporaryDirectory(prefix="proloag_asr_modes_") as location:
            root = Path(location)
            path = root / "synthetic.wav"
            path.write_bytes(b"synthetic audio")
            source = Source("c" * 32, path, path.name, "local", _identity(path), 1.0,
                            (Track(0, "pcm_s16le", "", "", 1, 1.0),))
            store = tr.TranscriptionStore(root)
            with patch.object(tr, "engine_available", return_value=True), \
                    patch.object(tr, "discover_tools", return_value=SimpleNamespace(ffmpeg=Path("synthetic-ffmpeg"))), \
                    patch.object(tr.subprocess, "Popen"), patch.object(tr.threading, "Thread"):
                for mode, model in tr.MODELS.items():
                    for requested in ("cpu", "auto"):
                        with self.subTest(mode=mode, requested=requested):
                            job_id = store.start(source, 0, "ja", mode, device=requested)
                            folder = store.folder(job_id)
                            request = tr.read_json(folder / "request.json")
                            self.assertEqual((request["model"], request["device"]), (model, requested))
                            state = tr.read_json(folder / "state.json")
                            tr.atomic_json(folder / "state.json", {**state, "status": "done"})

    def test_checkpoint_signature_legacy_values_are_unchanged(self):
        request = {"model": "medium", "device": "cpu", "source_sha256": "d" * 64,
                   "duration": 300, "track": {"index": 0}, "language": "ja", "hotwords": ""}
        self.assertEqual(tr.checkpoint_signature(request),
                         "9be491fc222c69ae0170548d36016430971c87d54747b8703c901fbda116c7d1")


if __name__ == "__main__":
    unittest.main()
