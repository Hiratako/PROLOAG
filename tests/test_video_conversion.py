"""Real, synthetic MKV conversion and output ownership boundaries."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app_core import video_conversion as video
from app_core.media_conversion import MediaError, _process_options, discover_tools


class VideoConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_temp = tempfile.TemporaryDirectory(prefix="pgvideo_fixtures_")
        cls.fixtures = Path(cls.fixture_temp.name)
        cls.tools = discover_tools(cls.fixtures)
        cls.subtitles = cls.fixtures / "字幕.srt"
        cls.subtitles.write_text("1\n00:00:00,200 --> 00:00:01,500\n日本語 subtitle\n", encoding="utf-8")
        cls.mkv = cls.fixtures / "日本語 複数音声.mkv"
        cls.silent = cls.fixtures / "silent.mkv"
        cls.unsupported = cls.fixtures / "mpeg4.mkv"
        cls.ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=96x64:r=10:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2",
            "-i", str(cls.subtitles),
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a:0", "aac", "-c:a:1", "flac", "-c:s", "srt",
            "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=eng",
            "-metadata:s:s:0", "language=jpn", str(cls.mkv),
        )
        cls.ffmpeg("-i", str(cls.mkv), "-map", "0:v", "-c", "copy", str(cls.silent))
        cls.ffmpeg("-i", str(cls.mkv), "-map", "0:v", "-c:v", "mpeg4", str(cls.unsupported))

    @classmethod
    def tearDownClass(cls):
        cls.fixture_temp.cleanup()

    @classmethod
    def ffmpeg(cls, *arguments):
        return subprocess.run(
            [str(cls.tools.ffmpeg), "-v", "error", "-nostdin", "-y", *arguments],
            check=True, capture_output=True, timeout=30, **_process_options(),
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pgvideo_test_")
        self.root = Path(self.temporary.name)
        self.owner = uuid4().hex
        self.output_dir = self.root / "exports"
        self.output_dir.mkdir()
        self.sibling = self.output_dir / "keep.txt"
        self.sibling.write_bytes(b"unrelated output")
        self.source_path = self.root / "input.mkv"
        shutil.copy2(self.mkv, self.source_path)
        self.before = self.source_path.read_bytes()
        self.manager = video.VideoManager(self.root / "app", tools=self.tools)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.manager.close()
        self.assertEqual(self.sibling.read_bytes(), b"unrelated output")
        self.assertEqual(self.source_path.read_bytes(), self.before)
        self.temporary.cleanup()

    def prepare(self, path=None):
        return self.manager.prepare_local(self.owner, str(path or self.source_path))

    def plan(self, source=None, *, mode="copy", audio=0, subtitle=None, name="converted.mp4"):
        source = source or self.prepare()
        return self.manager.plan(
            self.owner, source.id, video_index=source.videos[0].index,
            audio_index=None if audio is None else source.audios[audio].index,
            subtitle_index=None if subtitle is None else source.subtitles[subtitle].index,
            mode=mode, directory=str(self.output_dir), name=name,
        )

    def wait(self, identifier, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.manager.snapshot(self.owner)["job"]
            if job and job["id"] == identifier and job["status"] in {"done", "failed", "cancelled"}:
                return job
            time.sleep(.025)
        self.fail(f"Video job did not settle: {self.manager.snapshot(self.owner)}")

    def start(self, plan):
        identifier = self.manager.start(self.owner, plan.id, operation_id=uuid4().hex)
        return self.wait(identifier)

    def probe(self, path):
        result = subprocess.run(
            [str(self.tools.ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            check=True, capture_output=True, timeout=30, **_process_options(),
        )
        return json.loads(result.stdout)

    def stream_hashes(self, path, stream):
        result = subprocess.run(
            [str(self.tools.ffprobe), "-v", "error", "-select_streams", stream,
             "-show_packets", "-show_entries", "packet=data_hash", "-show_data_hash", "sha256",
             "-of", "json", str(path)],
            check=True, capture_output=True, timeout=30, **_process_options(),
        )
        return [packet["data_hash"] for packet in json.loads(result.stdout)["packets"]]

    def test_prepare_and_plan_do_not_start_conversion_or_create_output(self):
        source = self.prepare()
        before = set(self.root.rglob("*"))
        original_popen = subprocess.Popen

        def probe_only(command, *args, **kwargs):
            self.assertEqual(Path(command[0]).resolve(), self.tools.ffprobe.resolve(),
                             "planning may probe packet times but must not start conversion")
            return original_popen(command, *args, **kwargs)

        with patch.object(video.subprocess, "Popen", side_effect=probe_only):
            plan = self.plan(source)
        self.assertIsNone(self.manager.snapshot(self.owner)["job"])
        self.assertEqual(set(self.root.rglob("*")), before)
        self.assertFalse(plan.target.exists())

    def test_fast_mode_copies_h264_and_aac_packets(self):
        plan = self.plan()
        job = self.start(plan)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(self.stream_hashes(plan.target, "v:0"), self.stream_hashes(self.source_path, "v:0"))
        self.assertEqual(self.stream_hashes(plan.target, "a:0"), self.stream_hashes(self.source_path, "a:0"))
        self.assertEqual([item["codec_name"] for item in self.probe(plan.target)["streams"]], ["h264", "aac"])
        self.manager.clear(self.owner)
        self.manager.close()
        self.assertTrue(plan.target.is_file())

    def test_fast_mode_keeps_video_but_converts_selected_flac_audio(self):
        plan = self.plan(audio=1)
        self.assertTrue(plan.warnings)
        job = self.start(plan)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(self.stream_hashes(plan.target, "v:0"), self.stream_hashes(self.source_path, "v:0"))
        audio = next(item for item in self.probe(plan.target)["streams"] if item["codec_type"] == "audio")
        self.assertEqual(audio["codec_name"], "aac")
        self.assertEqual(audio["tags"]["language"], "eng")

    def test_delayed_pcm_uses_packet_time_when_stream_start_time_is_inaccurate(self):
        wav = self.root / "tone.wav"
        delayed = self.root / "delayed-pcm.mkv"
        self.ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:sample_rate=16000:duration=2",
                    "-c:a", "pcm_s16le", str(wav))
        self.ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10:d=8",
                    "-itsoffset", "5", "-i", str(wav), "-map", "0:v", "-map", "1:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(delayed))
        original = delayed.read_bytes()
        source = self.prepare(delayed)
        plan = self.plan(source)
        job = self.start(plan)
        self.assertEqual(job["status"], "done", job)
        streams = self.probe(plan.target)["streams"]
        output_video = next(stream for stream in streams if stream["codec_type"] == "video")
        output_audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        self.assertAlmostEqual(float(output_audio["start_time"]) - float(output_video["start_time"]), 5, delta=.15)
        packets = subprocess.run(
            [str(self.tools.ffprobe), "-v", "error", "-select_streams", "a:0", "-read_intervals", "%+#1",
             "-show_packets", "-show_entries", "packet=pts_time", "-of", "json", str(plan.target)],
            check=True, capture_output=True, timeout=30, **_process_options(),
        )
        self.assertAlmostEqual(float(json.loads(packets.stdout)["packets"][0]["pts_time"])
                               - float(output_video["start_time"]), 5, delta=.15)
        self.assertEqual(output_audio["codec_name"], "aac")
        self.assertEqual(delayed.read_bytes(), original)

    def test_compatible_mode_encodes_h264_yuv420p(self):
        plan = self.plan(mode="compatible", audio=1)
        job = self.start(plan)
        self.assertEqual(job["status"], "done", job)
        encoded = next(item for item in self.probe(plan.target)["streams"] if item["codec_type"] == "video")
        self.assertEqual((encoded["codec_name"], encoded["pix_fmt"], encoded["width"], encoded["height"]),
                         ("h264", "yuv420p", 96, 64))
        self.assertNotEqual(self.stream_hashes(plan.target, "v:0"), self.stream_hashes(self.source_path, "v:0"))

    def test_selected_subrip_is_soft_mov_text_and_unselected_tracks_are_dropped(self):
        plan = self.plan(subtitle=0)
        job = self.start(plan)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual([item["codec_name"] for item in self.probe(plan.target)["streams"]], ["h264", "aac", "mov_text"])
        extracted = self.ffmpeg("-i", str(plan.target), "-map", "0:s:0", "-f", "srt", "pipe:1").stdout.decode("utf-8")
        self.assertIn("日本語 subtitle", extracted)
        self.assertTrue(plan.dropped_tracks)

    def test_silent_video_and_explicit_no_audio_are_supported(self):
        for source_path in (self.silent, self.source_path):
            source = self.prepare(source_path)
            with self.subTest(audio_tracks=len(source.audios)):
                plan = self.plan(source, audio=None, name=f"silent-{len(source.audios)}.mp4")
                job = self.start(plan)
                self.assertEqual(job["status"], "done", job)
                self.assertEqual([item["codec_type"] for item in self.probe(plan.target)["streams"]], ["video"])

    def test_unsupported_fast_codec_requires_compatible_mode(self):
        source = self.prepare(self.unsupported)
        with self.assertRaises(MediaError):
            self.plan(source, audio=None)
        plan = self.plan(source, mode="compatible", audio=None)
        self.assertEqual(self.start(plan)["status"], "done")

    def test_upload_is_owned_and_clearing_does_not_remove_source_or_completed_output(self):
        source = self.manager.prepare_upload(self.owner, "アップロード.mkv", self.before)
        self.assertEqual(source.path.read_bytes(), self.before)
        uploaded = source.path
        plan = self.plan(source)
        self.assertEqual(self.start(plan)["status"], "done")
        self.manager.clear(self.owner)
        self.assertFalse(uploaded.exists())
        self.assertTrue(plan.target.is_file())

    def test_existing_output_is_never_overwritten_during_plan_or_start(self):
        target = self.output_dir / "converted.mp4"
        target.write_bytes(b"existing")
        with self.assertRaises(MediaError):
            self.plan()
        self.assertEqual(target.read_bytes(), b"existing")
        target.unlink()
        plan = self.plan()
        target.write_bytes(b"arrived after plan")
        try:
            job = self.start(plan)
        except MediaError:
            pass
        else:
            self.assertEqual(job["status"], "failed", job)
        self.assertEqual(target.read_bytes(), b"arrived after plan")

    def test_competing_output_at_publish_is_preserved(self):
        plan = self.plan()
        publish = video._publish_no_replace

        def competing_writer(partial, target):
            target.write_bytes(b"competing writer")
            return publish(partial, target)

        with patch.object(video, "_publish_no_replace", side_effect=competing_writer):
            job = self.start(plan)
        self.assertEqual(job["status"], "failed", job)
        self.assertEqual(plan.target.read_bytes(), b"competing writer")
        self.assertEqual(set(self.output_dir.iterdir()), {self.sibling, plan.target})

    def test_input_change_after_plan_is_rejected_before_conversion(self):
        plan = self.plan()
        self.source_path.write_bytes(self.before + b"changed")
        self.before = self.source_path.read_bytes()
        try:
            job = self.start(plan)
        except MediaError:
            pass
        else:
            self.assertEqual(job["status"], "failed", job)
        self.assertFalse(plan.target.exists())

    def test_thread_start_failure_is_actionable_and_releases_shared_slot(self):
        plan = self.plan()
        with patch.object(video.threading.Thread, "start", side_effect=RuntimeError("no thread available")):
            with self.assertRaises(MediaError):
                self.manager.start(self.owner, plan.id, operation_id=uuid4().hex)
        self.assertFalse(plan.target.exists())
        token = uuid4().hex
        self.assertTrue(video.acquire_conversion(self.manager.root, token))
        video.release_conversion(self.manager.root, token)
        self.manager.close()

    def test_disk_space_exhaustion_during_conversion_stops_and_cleans_partial(self):
        plan = self.plan(mode="compatible")
        original_command = video.conversion_command
        available = shutil.disk_usage(self.output_dir)
        main_thread = threading.current_thread()

        def slow_command(*args):
            command = original_command(*args)
            command.insert(command.index("-i"), "-re")
            return command

        def disk_space(path):
            return available if threading.current_thread() is main_thread else available._replace(free=0)

        with (patch.object(video, "conversion_command", side_effect=slow_command),
              patch.object(video.shutil, "disk_usage", side_effect=disk_space)):
            job = self.start(plan)
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("空き容量", job["error"])
        self.assertEqual(set(self.output_dir.iterdir()), {self.sibling})

    def test_cleanup_preserves_unexpected_file_in_owned_output_temp(self):
        plan = self.plan()
        publish = video._publish_no_replace
        external = []

        def foreign_file(partial, target):
            marker = partial.parent / "keep-external.txt"
            marker.write_bytes(b"external file")
            external.append(marker)
            return publish(partial, target)

        with patch.object(video, "_publish_no_replace", side_effect=foreign_file):
            job = self.start(plan)
        self.assertEqual(job["status"], "done", job)
        self.assertTrue(plan.target.is_file())
        self.assertEqual(external[0].read_bytes(), b"external file")
        self.assertFalse((external[0].parent / "partial.mp4").exists())
        self.manager.close()
        self.assertEqual(external[0].read_bytes(), b"external file")

    def test_cancel_removes_only_its_partial_output(self):
        plan = self.plan(mode="compatible")
        actual_popen = subprocess.Popen
        started = threading.Event()

        def slow_conversion(command, *args, **kwargs):
            command = list(command)
            if Path(command[0]).name.lower().startswith("ffmpeg") and "-i" in command:
                command.insert(command.index("-i"), "-re")
                process = actual_popen(command, *args, **kwargs)
                started.set()
                return process
            return actual_popen(command, *args, **kwargs)

        with patch.object(video.subprocess, "Popen", side_effect=slow_conversion):
            identifier = self.manager.start(self.owner, plan.id, operation_id=uuid4().hex)
            self.assertTrue(started.wait(5), "conversion did not start")
            self.manager.cancel(self.owner)
            job = self.wait(identifier)
        self.assertEqual(job["status"], "cancelled", job)
        self.assertFalse(plan.target.exists())
        self.assertEqual(list(self.output_dir.iterdir()), [self.sibling])

    def test_output_filename_rejects_paths_and_reserved_names_and_has_mp4_suffix(self):
        source = self.prepare()
        for name in ("../input.mkv", str(self.root / "outside.mp4"), "CON.mp4", "AUX"):
            with self.subTest(name=name), self.assertRaises(MediaError):
                self.plan(source, name=name)
        self.assertEqual(self.plan(source, name="input.mkv").target.suffix, ".mp4")

    def test_empty_corrupt_and_disguised_uploads_are_rejected(self):
        for name, payload in (("bad.mkv", b"broken"), ("empty.mkv", b""), ("not-mkv.mp4", self.before)):
            with self.subTest(name=name), self.assertRaises(MediaError):
                self.manager.prepare_upload(self.owner, name, payload)


if __name__ == "__main__":
    unittest.main()
