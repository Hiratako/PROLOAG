import unittest
import test_portable_release_safety as fixtures


@unittest.skipUnless(fixtures.POWERSHELL, "PowerShell is required")
class MediaPackagingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PortableReleaseSafetyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_optional_bundle_copies_only_the_pair_and_license(self):
        for name in (
            "bin/ffmpeg.exe",
            "bin/ffprobe.exe",
            "LICENSE",
            "private.json",
            "bin/token.json",
        ):
            self.fixture.write(
                "ffmpeg-source/" + name,
                "synthetic tool" if name.endswith(".exe") else "synthetic",
            )
        variables = self.fixture.variables()
        body = (
            variables
            + f"$FFmpegDirectory = {fixtures.ps_string(self.fixture.root / 'ffmpeg-source')}; Assert-ReleaseTargets; Copy-MediaTools"
        )
        result = self.fixture.run_functions(fixtures.BUILD_SCRIPT, body)
        self.fixture.assert_success(result)
        app = self.fixture.root / "release/synthetic_demo/app"
        self.assertEqual(
            {p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file()},
            {
                "tools/ffmpeg/bin/ffmpeg.exe",
                "tools/ffmpeg/bin/ffprobe.exe",
                "tools/ffmpeg/LICENSE",
            },
        )

    def test_missing_license_fails_before_any_copy_and_default_is_optional(self):
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            self.fixture.write("ffmpeg-source/" + name)
        body = (
            self.fixture.variables()
            + "$FFmpegDirectory = ''; Assert-ReleaseTargets; Copy-MediaTools"
        )
        self.fixture.assert_success(
            self.fixture.run_functions(fixtures.BUILD_SCRIPT, body)
        )
        body = (
            self.fixture.variables()
            + f"$FFmpegDirectory = {fixtures.ps_string(self.fixture.root / 'ffmpeg-source')}; Assert-ReleaseTargets; Copy-MediaTools"
        )
        result = self.fixture.run_functions(fixtures.BUILD_SCRIPT, body)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(
            (self.fixture.root / "release/synthetic_demo/app/tools").exists()
        )
