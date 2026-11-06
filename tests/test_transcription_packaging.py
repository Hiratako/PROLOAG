import unittest
import test_portable_release_safety as fixtures


@unittest.skipUnless(fixtures.POWERSHELL, "PowerShell is required")
class TranscriptionPackagingTests(unittest.TestCase):
    def test_public_setup_ships_without_audio_history_engine_or_models(self):
        fixture = fixtures.PortableReleaseSafetyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        public = [
            "requirements-transcription.txt",
            "docs/transcription.md",
            "app_core/transcription.py",
            "app_core/transcription_worker.py",
            "app_core/setup_transcription.py",
            "app_core/transcription_ui.py",
            "pages/21_音声・動画の文字起こし.py",
        ]
        private = [
            "transcription_data/jobs/abcdef/audio.wav",
            "transcription_data/jobs/abcdef/segments.jsonl",
            "transcription_data/jobs/abcdef/edits.json",
            "tools/asr-models/model.bin",
            ".deps-asr311/faster_whisper/__init__.py",
        ]
        for name in public + private:
            fixture.write(name, "synthetic")
        fixture.test_copy_app_allows_only_code_stylesheet_and_streamlit_config()
        app = fixture.root / "release/synthetic_demo/app"
        for name in public:
            self.assertTrue((app / name).is_file(), name)
        for name in private:
            self.assertFalse((app / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
