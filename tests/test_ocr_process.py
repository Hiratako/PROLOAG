"""Actual PDF/image subprocesses and deterministic cancellation/error injection."""

from pathlib import Path
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image
from app_core import ocr_service as ocr


def make_pdf(path):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    text = b"BT /F1 24 Tf 50 700 Td (Hello OCR page one) Tj ET"
    objects.append(
        b"<< /Length "
        + str(len(text)).encode()
        + b" >>\nstream\n"
        + text
        + b"\nendstream"
    )
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources << /Font << /F1 4 0 R >> >> /Contents 7 0 R >>"
    )
    text = b"BT /F1 24 Tf 50 700 Td (Hello OCR page two) Tj ET"
    objects.append(
        b"<< /Length "
        + str(len(text)).encode()
        + b" >>\nstream\n"
        + text
        + b"\nendstream"
    )
    data = b"%PDF-1.4\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets[1:]:
        data += f"{offset:010} 00000 n \n".encode()
    data += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    path.write_bytes(data)


FAKE = """
import os,time
from types import SimpleNamespace
class RapidOCR:
    def __init__(self, **kwargs):
        self.calls=0
        if os.environ.get("PROLOAG_OCR_TEST_MODE")=="LOAD": time.sleep(10)
    def __call__(self,image):
        self.calls+=1;mode=os.environ.get("PROLOAG_OCR_TEST_MODE")
        if self.calls>1 and mode=="SLOW":time.sleep(10)
        if self.calls>1 and mode=="FAIL_SECOND":raise RuntimeError("Injected page recognition failure")
        return SimpleNamespace(txts=("日本語 English",),scores=(.7,),boxes=([ [0,0],[100,0],[100,100],[0,100] ],))
"""


class OCRProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = ocr.dependency_dir(
            Path(ocr.__file__).resolve().parents[1]
        ).resolve()
        if not (cls.engine / "rapidocr/__init__.py").is_file():
            raise RuntimeError(
                "OCR optional dependencies must be installed for these integration tests"
            )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pgocr_process_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.cleanup)
        code = self.root / "app_core"
        code.mkdir()
        (code / "__init__.py").write_text("")
        for name in (
            "ocr_service.py",
            "ocr_worker.py",
            "transcription.py",
            "media_conversion.py",
        ):
            shutil.copy2(Path(ocr.__file__).parent / name, code / name)

        def link_or_copy(source, target):
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)

        shutil.copytree(
            self.engine,
            ocr.dependency_dir(self.root),
            copy_function=link_or_copy,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (self.root / "rapidocr").mkdir()
        (self.root / "rapidocr/__init__.py").write_text(FAKE, encoding="utf-8")
        self.image = self.root / "sample.png"
        Image.new("RGB", (400, 300), "white").save(self.image)
        self.pdf = self.root / "sample.pdf"
        make_pdf(self.pdf)
        self.manager = ocr.OCRSources(self.root)
        self.store = ocr.OCRStore(self.root)

    def cleanup(self):
        if hasattr(self, "manager"):
            self.manager.close()
        if hasattr(self, "store"):
            for state in self.store.history():
                if state["status"] in ocr.ACTIVE:
                    self.store.cancel(state["id"])
            end = time.monotonic() + 10
            while time.monotonic() < end:
                active = False
                for state in self.store.history():
                    try:
                        with ocr.file_lock(
                            self.store.folder(state["id"]) / ".worker.lock",
                            blocking=False,
                        ):
                            pass
                    except OSError:
                        active = True
                if not active and not any(
                    s["status"] in ocr.ACTIVE for s in self.store.history()
                ):
                    break
                time.sleep(0.1)
        self.temp.cleanup()

    def wait(self, job, status=None, pages=None):
        end = time.monotonic() + 25
        while time.monotonic() < end:
            value = self.store.state(job)
            if pages is not None:
                ready = len(value["completed_pages"]) >= pages
            else:
                ready = (
                    value["status"] == status
                    if status
                    else value["status"] not in ocr.ACTIVE
                )
            if ready:
                return self.store.result(job)
            time.sleep(0.05)
        raise AssertionError(self.store.result(job))

    def test_pdf_native_text_extraction_and_page_range(self):
        source = self.manager.prepare("a" * 32, path=str(self.pdf))
        self.assertEqual(source.pages, 2)
        preview = self.manager.preview(source, 2, 0)
        self.assertTrue(preview.startswith(b"\x89PNG"))
        job = self.store.start(source, [2])
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "done")
        self.assertEqual(result["pages"][0]["method"], "pdf_text")
        self.assertIn(
            "page two", " ".join(b["text"] for b in result["pages"][0]["blocks"])
        )

    def test_uploaded_image_persists_after_input_clear(self):
        source = self.manager.prepare(
            "a" * 32, name="教材.png", buffer=self.image.read_bytes()
        )
        job = self.store.start(source, [1], strategy="ocr")
        self.manager.clear("a" * 32)
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "done")
        self.assertEqual(result["pages"][0]["blocks"][0]["text"], "日本語 English")

    def test_rotation_and_crop_produce_expected_working_dimensions(self):
        source = self.manager.prepare("a" * 32, path=str(self.image))
        job = self.store.start(
            source, [1], {"1": {"rotation": 90, "crop": [0, 0, 0.5, 1]}}, strategy="ocr"
        )
        result = self.wait(job)
        page = result["pages"][0]
        self.assertEqual((page["width"], page["height"]), (150, 400))
        self.assertTrue(
            all(0 <= v <= 1 for b in page["blocks"] for p in b["box"] for v in p)
        )

    def test_ocr_override_reads_pdf_as_image(self):
        source = self.manager.prepare("a" * 32, path=str(self.pdf))
        result = self.wait(self.store.start(source, [1], strategy="ocr"))
        self.assertEqual(result["pages"][0]["method"], "ocr")

    def test_cancel_retains_completed_page(self):
        source = self.manager.prepare("a" * 32, path=str(self.pdf))
        with patch.dict(os.environ, {"PROLOAG_OCR_TEST_MODE": "SLOW"}):
            job = self.store.start(source, [1, 2], strategy="ocr")
        self.wait(job, pages=1)
        self.store.cancel(job)
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "cancelled")
        self.assertEqual([p["number"] for p in result["pages"]], [1])

    def test_load_can_be_cancelled(self):
        source = self.manager.prepare("a" * 32, path=str(self.image))
        with patch.dict(os.environ, {"PROLOAG_OCR_TEST_MODE": "LOAD"}):
            job = self.store.start(source, [1], strategy="ocr")
        self.wait(job, status="loading")
        self.store.cancel(job)
        self.assertEqual(self.wait(job)["state"]["status"], "cancelled")

    def test_page_failure_retains_previous_completed_page(self):
        source = self.manager.prepare("a" * 32, path=str(self.pdf))
        with patch.dict(os.environ, {"PROLOAG_OCR_TEST_MODE": "FAIL_SECOND"}):
            job = self.store.start(source, [1, 2], strategy="ocr")
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "failed")
        self.assertEqual(len(result["pages"]), 1)
        self.assertIn("Injected page", result["state"]["error"])

    def test_invalid_source_does_not_replace_previous_input(self):
        source = self.manager.prepare("a" * 32, path=str(self.image))
        with self.assertRaises(ocr.OCRError):
            self.manager.prepare("a" * 32, name="bad.png", buffer=b"not an image")
        self.assertEqual(self.manager.snapshot("a" * 32), source)

    def test_moved_original_is_not_needed_after_job_start(self):
        source = self.manager.prepare("a" * 32, path=str(self.pdf))
        job = self.store.start(source, [1])
        self.pdf.unlink()
        self.assertEqual(self.wait(job)["state"]["status"], "done")

    def test_bad_pdf_and_missing_file_have_actionable_errors(self):
        with self.assertRaises(ocr.OCRError):
            self.manager.prepare("a" * 32, name="bad.pdf", buffer=b"wrong format")
        with self.assertRaises(ocr.OCRError):
            self.manager.prepare("a" * 32, path=str(self.root / "missing.png"))


if __name__ == "__main__":
    unittest.main()
