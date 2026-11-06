from array import array
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import media_conversion as media


def wait_for(manager, owner, *, running=False, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = manager.snapshot(owner)["job"]
        if job and (
            job["status"] == "running" if running else job["status"] not in media.ACTIVE
        ):
            return job
        time.sleep(0.03)
    raise AssertionError("Conversion did not reach the expected state")


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pgmed_fixtures_")
        cls.fixtures = Path(cls.temp.name)
        cls.tools = media.discover_tools(cls.fixtures)
        cls.video = cls.fixtures / "日本語 空白 & 動画.mp4"
        base = [
            str(cls.tools.ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=96x64:r=10:d=2",
        ]
        audio = [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=jpn",
            "-metadata:s:a:0",
            "title=日本語",
            "-metadata:s:a:1",
            "language=eng",
            "-metadata:s:a:1",
            "title=English",
            "-shortest",
        ]
        subprocess.run(
            base + audio + ["-y", str(cls.video)],
            check=True,
            capture_output=True,
            timeout=30,
            **media._process_options(),
        )
        cls.silent = cls.fixtures / "silent.mp4"
        subprocess.run(
            base + ["-an", "-c:v", "mpeg4", "-y", str(cls.silent)],
            check=True,
            capture_output=True,
            timeout=30,
            **media._process_options(),
        )
        cls.long = cls.fixtures / "long.mp4"
        subprocess.run(
            [
                str(cls.tools.ffmpeg),
                "-nostdin",
                "-v",
                "error",
                "-stream_loop",
                "4",
                "-i",
                str(cls.video),
                "-map",
                "0",
                "-c",
                "copy",
                "-t",
                "10",
                "-y",
                str(cls.long),
            ],
            check=True,
            capture_output=True,
            timeout=30,
            **media._process_options(),
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="pgmed_test_")
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.manager = media.MediaManager(self.root, tools=self.tools)
        self.addCleanup(self.manager.close)
        self.owner = uuid4().hex
        self.source_before = hashlib.sha256(self.video.read_bytes()).hexdigest()

    def tearDown(self):
        self.assertEqual(
            hashlib.sha256(self.video.read_bytes()).hexdigest(), self.source_before
        )

    def prepare(self):
        return self.manager.prepare_local(self.owner, str(self.video))

    def start(self, source=None, track=None, bitrate=192, operation_id=None):
        source = source or self.prepare()
        identifier = self.manager.start(
            self.owner,
            source.id,
            source.tracks[0].index if track is None else track,
            bitrate,
            operation_id=operation_id or uuid4().hex,
        )
        return identifier, wait_for(self.manager, self.owner)

    def test_empty_manager_and_read_only_snapshot_create_no_files(self):
        self.assertEqual(
            self.manager.snapshot(self.owner), {"source": None, "job": None}
        )
        self.assertIsNone(self.manager._base)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_probe_selects_two_audio_tracks_and_handles_unicode_paths(self):
        source = self.manager.prepare_local(self.owner, '"' + str(self.video) + '"')
        self.assertEqual([t.language for t in source.tracks], ["jpn", "eng"])
        self.assertAlmostEqual(source.duration, 2, delta=0.1)
        self.assertEqual(source.mode, "local")
        self.assertFalse(
            (self.manager._works[self.owner].folder / "input.mp4").exists()
        )

    def test_upload_is_copied_only_on_prepare_and_removed_on_clear(self):
        source = self.manager.prepare_upload(
            self.owner, self.video.name, memoryview(self.video.read_bytes())
        )
        self.assertEqual(source.mode, "upload")
        self.assertEqual(source.path.read_bytes(), self.video.read_bytes())
        folder = source.path.parent
        self.manager.clear(self.owner)
        self.assertFalse(folder.exists())
        self.assertTrue(self.video.exists())

    def test_no_audio_corrupt_empty_and_wrong_container_are_actionable(self):
        for path, message in [
            (self.silent, "音声トラック"),
            (self.root / "bad.mp4", "読み取れ"),
            (self.root / "empty.mp4", "空の"),
        ]:
            if path.name == "bad.mp4":
                path.write_bytes(b"not a movie")
            if path.name == "empty.mp4":
                path.write_bytes(b"")
            with (
                self.subTest(path=path.name),
                self.assertRaisesRegex(media.MediaError, message),
            ):
                self.manager.prepare_local(self.owner, str(path))
        self.assertIsNone(self.manager.snapshot(self.owner)["source"])
        self.assertEqual(list(self.manager._base.iterdir()), [])

    def test_invalid_new_video_keeps_previous_result(self):
        source = self.prepare()
        identifier, job = self.start(source)
        before = self.manager.result_bytes(self.owner, identifier)
        with self.assertRaises(media.MediaError):
            self.manager.prepare_upload(self.owner, "bad.mp4", b"broken")
        self.assertEqual(self.manager.result_bytes(self.owner, identifier), before)

    def test_upload_limit_does_not_apply_to_direct_local_file(self):
        with patch.object(media, "UPLOAD_LIMIT", 1):
            with self.assertRaisesRegex(media.MediaError, "200MB"):
                self.manager.prepare_upload(
                    self.owner, "video.mp4", self.video.read_bytes()
                )
            self.assertEqual(self.prepare().name, self.video.name)

    def test_mp3_has_only_chosen_english_audio(self):
        source = self.prepare()
        identifier, job = self.start(source, track=source.tracks[1].index)
        self.assertEqual(job["status"], "done", job)
        result = self.manager._result(self.owner, identifier)
        info = media._probe(self.tools, result)
        self.assertEqual([row["codec_type"] for row in info["streams"]], ["audio"])
        self.assertAlmostEqual(job["duration"], 2, delta=0.15)
        decoded = subprocess.run(
            [
                str(self.tools.ffmpeg),
                "-v",
                "error",
                "-i",
                str(result),
                "-t",
                "1",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=15,
            **media._process_options(),
        ).stdout
        samples = array("h", decoded)[1000:7000]
        frequency = sum(a <= 0 < b for a, b in zip(samples, samples[1:])) / (
            len(samples) / 8000
        )
        self.assertAlmostEqual(frequency, 880, delta=25)

    def test_each_bitrate_is_applied(self):
        source = self.prepare()
        sizes = []
        for bitrate in media.BITRATES:
            identifier, job = self.start(source, bitrate=bitrate)
            self.assertEqual(job["status"], "done", job)
            self.assertEqual(job["bitrate"], bitrate)
            sizes.append(job["size"])
        self.assertEqual(sizes, sorted(set(sizes)))

    def test_exact_operation_retry_reuses_result_and_rejects_changed_conditions(self):
        source = self.prepare()
        operation = uuid4().hex
        identifier, job = self.start(source, operation_id=operation)
        result = self.manager._result(self.owner, identifier)
        before = (result.stat().st_mtime_ns, result.read_bytes())
        self.assertEqual(
            self.manager.start(
                self.owner,
                source.id,
                source.tracks[0].index,
                192,
                operation_id=operation,
            ),
            identifier,
        )
        self.assertEqual((result.stat().st_mtime_ns, result.read_bytes()), before)
        with self.assertRaisesRegex(media.MediaError, "異なる"):
            self.manager.start(
                self.owner,
                source.id,
                source.tracks[0].index,
                320,
                operation_id=operation,
            )

    def test_invalid_track_bitrate_owner_and_stale_source_do_not_start(self):
        source = self.prepare()
        for index, bitrate in [
            (-1, 192),
            (999, 192),
            (source.tracks[0].index, 0),
            (source.tracks[0].index, True),
        ]:
            with (
                self.subTest(index=index, bitrate=bitrate),
                self.assertRaises(media.MediaError),
            ):
                self.manager.start(
                    self.owner, source.id, index, bitrate, operation_id=uuid4().hex
                )
        with self.assertRaises(media.MediaError):
            self.manager.snapshot("../escape")
        with self.assertRaises(media.MediaError):
            self.manager.start(
                self.owner, "old", source.tracks[0].index, 192, operation_id=uuid4().hex
            )
        self.assertIsNone(self.manager.snapshot(self.owner)["job"])

    def test_modified_source_is_rejected_before_start(self):
        path = self.root / "copy.mp4"
        shutil.copy2(self.video, path)
        source = self.manager.prepare_local(self.owner, str(path))
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(media.MediaError, "変更"):
            self.manager.start(
                self.owner,
                source.id,
                source.tracks[0].index,
                192,
                operation_id=uuid4().hex,
            )

    def realtime(self):
        original = media.conversion_command

        def command(*args):
            values = original(*args)
            values.insert(values.index("-i"), "-re")
            return values

        return patch.object(media, "conversion_command", side_effect=command)

    def test_running_job_cancel_removes_partial_and_can_retry(self):
        source = self.manager.prepare_local(self.owner, str(self.long))
        with self.realtime():
            self.manager.start(
                self.owner,
                source.id,
                source.tracks[0].index,
                192,
                operation_id=uuid4().hex,
            )
            wait_for(self.manager, self.owner, running=True)
            self.assertTrue(self.manager.cancel(self.owner))
            job = wait_for(self.manager, self.owner)
        self.assertEqual(job["status"], "cancelled")
        folder = self.manager._works[self.owner].job.folder
        self.assertFalse((folder / "partial.mp3").exists())
        self.assertFalse((folder / "audio.mp3").exists())
        identifier, job = self.start(source)
        self.assertEqual(job["status"], "done", job)

    def test_active_job_prevents_duplicate_clear_and_second_session_conversion(self):
        source = self.prepare()
        other = uuid4().hex
        other_source = self.manager.prepare_local(other, str(self.video))
        with self.realtime():
            operation = uuid4().hex
            job_id = self.manager.start(
                self.owner,
                source.id,
                source.tracks[0].index,
                192,
                operation_id=operation,
            )
            wait_for(self.manager, self.owner, running=True)
            self.assertEqual(
                self.manager.start(
                    self.owner,
                    source.id,
                    source.tracks[0].index,
                    192,
                    operation_id=operation,
                ),
                job_id,
            )
            for call in [
                lambda: self.manager.clear(self.owner),
                lambda: self.prepare(),
                lambda: self.manager.start(
                    other,
                    other_source.id,
                    other_source.tracks[0].index,
                    192,
                    operation_id=uuid4().hex,
                ),
            ]:
                with self.assertRaises(media.MediaError):
                    call()
            self.manager.cancel(self.owner)
            wait_for(self.manager, self.owner)

    def test_timeout_ends_worker_and_does_not_publish_partial(self):
        source = self.prepare()
        self.manager.timeout = 0.1
        with self.realtime():
            identifier, job = self.start(source)
        self.assertEqual(job["status"], "failed")
        self.assertIn("時間内", job["error"])
        with self.assertRaises(media.MediaError):
            self.manager.result_bytes(self.owner, identifier)

    def test_conversion_failure_keeps_input_and_can_retry(self):
        source = self.prepare()
        original = media.conversion_command

        def bad(*args):
            return ["missing-ffmpeg-not-real.exe"] + original(*args)[1:]

        with patch.object(media, "conversion_command", side_effect=bad):
            identifier, job = self.start(source)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.manager.snapshot(self.owner)["source"], source)
        identifier, job = self.start(source)
        self.assertEqual(job["status"], "done", job)

    def test_source_changed_during_conversion_is_not_published(self):
        path = self.root / "moving.mp4"
        shutil.copy2(self.video, path)
        source = self.manager.prepare_local(self.owner, str(path))
        with self.realtime():
            self.manager.start(
                self.owner,
                source.id,
                source.tracks[0].index,
                192,
                operation_id=uuid4().hex,
            )
            wait_for(self.manager, self.owner, running=True)
            current = path.stat()
            os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns + 10000000))
            job = wait_for(self.manager, self.owner)
        self.assertEqual(job["status"], "failed")
        self.assertIn("変更", job["error"])

    def test_missing_encoder_and_insufficient_disk_do_not_start_jobs(self):
        source = self.prepare()
        with patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=b"no encoder"),
        ):
            with self.assertRaisesRegex(media.MediaError, "エンコーダー"):
                self.manager.start(
                    self.owner,
                    source.id,
                    source.tracks[0].index,
                    192,
                    operation_id=uuid4().hex,
                )
        self.assertIsNone(self.manager.snapshot(self.owner)["job"])
        with patch.object(shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(media.MediaError, "空き容量"):
                self.manager.start(
                    self.owner,
                    source.id,
                    source.tracks[0].index,
                    192,
                    operation_id=uuid4().hex,
                )
        self.assertIsNone(self.manager.snapshot(self.owner)["job"])

    def test_large_result_guard_still_allows_streamed_direct_save(self):
        source = self.prepare()
        identifier, job = self.start(source)
        with self.assertRaisesRegex(media.MediaError, "直接保存"):
            self.manager.result_bytes(self.owner, identifier, limit=1)
        saved = self.manager.save_result(
            self.owner, identifier, str(self.root), "音声 & copy.mp3"
        )
        self.assertEqual(
            saved.read_bytes(), self.manager.result_bytes(self.owner, identifier)
        )
        self.manager.clear(self.owner)
        self.assertTrue(saved.exists())

    def test_existing_output_is_never_overwritten(self):
        identifier, job = self.start()
        target = self.root / "exists.mp3"
        target.write_bytes(b"keep me")
        with self.assertRaisesRegex(media.MediaError, "同じ名前"):
            self.manager.save_result(
                self.owner, identifier, str(self.root), target.name
            )
        self.assertEqual(target.read_bytes(), b"keep me")

    def test_failed_direct_save_removes_only_the_new_partial(self):
        identifier, job = self.start()
        target = self.root / "partial.mp3"

        def broken(source, destination, **kwargs):
            destination.write(b"partial")
            raise OSError("synthetic disk full")

        with (
            patch.object(shutil, "copyfileobj", side_effect=broken),
            self.assertRaises(media.MediaError),
        ):
            self.manager.save_result(
                self.owner, identifier, str(self.root), target.name
            )
        self.assertFalse(target.exists())
        self.assertTrue(self.manager.result_bytes(self.owner, identifier))

    def test_session_cannot_read_another_sessions_result(self):
        identifier, job = self.start()
        with self.assertRaises(media.MediaError):
            self.manager.result_bytes(uuid4().hex, identifier)
        with self.assertRaises(media.MediaError):
            self.manager.result_bytes(self.owner, uuid4().hex)

    def test_removed_temp_folder_can_be_cleared_and_prepared_again(self):
        self.manager.prepare_upload(self.owner, "video.mp4", self.video.read_bytes())
        shutil.rmtree(self.manager._base)
        self.manager.clear(self.owner)
        source = self.manager.prepare_upload(
            self.owner, "video.mp4", self.video.read_bytes()
        )
        self.assertTrue(source.path.is_file())

    def test_prune_and_shutdown_remove_only_owned_temp_files(self):
        source = self.manager.prepare_upload(
            self.owner, "video.mp4", self.video.read_bytes()
        )
        folder = source.path.parent
        self.manager._works[self.owner].touched -= 90000
        self.manager.prune()
        self.assertFalse(folder.exists())
        self.assertTrue(self.video.exists())
        self.prepare()
        base = self.manager._base
        self.manager.close()
        self.assertFalse(base.exists())

    def test_bad_paths_and_output_names_are_rejected(self):
        for value in [
            "relative.mp4",
            "https://example.com/video.mp4",
            str(self.video) + ".exe",
            str(self.root / "missing.mp4"),
        ]:
            with self.subTest(value=value), self.assertRaises(media.MediaError):
                self.manager.prepare_local(self.owner, value)
        for value in [
            "../outside.mp3",
            "C:\\outside.mp3",
            "CON.mp3",
            "bad?.mp3",
            "",
            "bad\n.mp3",
        ]:
            with self.subTest(value=value), self.assertRaises(media.MediaError):
                media.mp3_name(value)
        self.assertEqual(media.mp3_name("audio"), "audio.mp3")

    def test_tool_discovery_supports_bundled_directory_without_path(self):
        folder = self.root / "tools/ffmpeg/bin"
        folder.mkdir(parents=True)
        for name in ("ffmpeg", "ffprobe"):
            (folder / (name + (".exe" if os.name == "nt" else ""))).touch()
        with (
            patch.object(shutil, "which", return_value=None),
            patch.dict(os.environ, {"PROLOAG_FFMPEG_DIR": ""}),
        ):
            tools = media.discover_tools(self.root)
            self.assertEqual(tools.ffmpeg.parent, folder)
            with self.assertRaisesRegex(media.MediaError, "見つかりません"):
                media.discover_tools(self.root / "missing")


if __name__ == "__main__":
    unittest.main()
