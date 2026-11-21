"""Actual PDF/image subprocesses and deterministic cancellation/error injection."""

from pathlib import Path
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

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


def make_layout_pdf(path, *, size=(600, 800), rotation=0, cropbox=None, image=False, tiny=False, form=False):
    width, height = size
    page_extra = f" /Rotate {rotation}".encode()
    if cropbox:
        page_extra += (" /CropBox [" + " ".join(map(str, cropbox)) + "]").encode()
    resources = b" /Font << /F1 4 0 R >>"
    text = b"BT /F1 24 Tf 150 700 Td (Native Header) Tj ET"
    if image:
        resources += b" /XObject << /Im1 6 0 R >>" if not form else b" /XObject << /Fm1 7 0 R >>"
        matrix = b"0.5 0 0 0.5 400 200" if tiny else b"400 0 0 200 50 300"
        text += b"\nq " + matrix + (b" cm /Fm1 Do Q" if form else b" cm /Im1 Do Q")
    elif form:
        resources += b" /XObject << /Fm1 6 0 R >>"
        form_text = text
        text = b"/Fm1 Do"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}]".encode() + page_extra
               + b" /Resources <<" + resources + b" >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream"]
    if image:
        raw = zlib.compress(bytes([180, 180, 180] * (16 * 8)))
        objects.append(b"<< /Type /XObject /Subtype /Image /Width 16 /Height 8 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length "
                       + str(len(raw)).encode() + b" >>\nstream\n" + raw + b"\nendstream")
        if form:
            content = b"q 1 0 0 1 0 0 cm /Im1 Do Q"
            objects.append(b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << /XObject << /Im1 6 0 R >> >> /Length "
                           + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    elif form:
        objects.append(f"<< /Type /XObject /Subtype /Form /BBox [0 0 {width} {height}]".encode()
                       + b" /Resources << /Font << /F1 4 0 R >> >> /Length " + str(len(form_text)).encode()
                       + b" >>\nstream\n" + form_text + b"\nendstream")
    data, offsets = b"%PDF-1.4\n", [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    data += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    path.write_bytes(data)


FAKE = """
import os,time
from types import SimpleNamespace
from enum import Enum
class ModelType(Enum):
    SMALL='small'
    MEDIUM='medium'
class OCRVersion(Enum):
    PPOCRV6='PP-OCRv6'
class EngineType(Enum):
    ONNXRUNTIME='onnxruntime'
class RapidOCR:
    def __init__(self, **kwargs):
        self.calls=0
        if os.environ.get("PROLOAG_OCR_TEST_MODE")=="LOAD": time.sleep(10)
        if os.environ.get("PROLOAG_OCR_TEST_MODE")=="FAIL_LOAD": raise RuntimeError("Engine must not load")
    def __call__(self,image):
        self.calls+=1;mode=os.environ.get("PROLOAG_OCR_TEST_MODE")
        if self.calls>1 and mode=="SLOW":time.sleep(10)
        if self.calls>1 and mode=="FAIL_SECOND":raise RuntimeError("Injected page recognition failure")
        return SimpleNamespace(txts=("日本語 English",),scores=(.7,),boxes=([ [0,0],[100,0],[100,100],[0,100] ],))
"""


class OCRProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Path(os.environ.get("PROLOAG_OCR_TEST_DEPS") or ocr.dependency_dir(
            Path(ocr.__file__).resolve().parents[1]
        )).resolve()
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

        # Use the prepared optional runtime read-only, not thousands of copied
        # or hard-linked dependency files for every synthetic test case.
        inherited = os.environ.get("PYTHONPATH", "")
        environment = patch.dict(os.environ, {"PYTHONPATH": os.pathsep.join([str(self.engine), inherited])})
        environment.start()
        self.addCleanup(environment.stop)
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

    def test_mixed_pdf_keeps_native_header_and_reads_only_image_region(self):
        make_layout_pdf(self.pdf, image=True)
        before = self.pdf.read_bytes()
        source = self.manager.prepare("a" * 32, path=str(self.pdf))
        result = self.wait(self.store.start(source, [1]))
        self.assertEqual(result["state"]["status"], "done")
        page = result["pages"][0]
        self.assertEqual(page["method"], "pdf_mixed")
        self.assertEqual(sum("Native Header" in b["text"] for b in page["blocks"]), 1)
        recognized = next(b for b in page["blocks"] if b["text"] == "日本語 English")
        self.assertTrue(all(0.08 <= point[0] <= 0.76 and 0.37 <= point[1] <= 0.64 for point in recognized["box"]))
        self.assertEqual(self.pdf.read_bytes(), before)

    def test_tiny_decoration_and_nested_form_do_not_fail_native_page(self):
        for params in ({"tiny": True}, {"form": True}):
            with self.subTest(params=params):
                make_layout_pdf(self.pdf, image=True, **params)
                source = self.manager.prepare("a" * 32, path=str(self.pdf))
                result = self.wait(self.store.start(source, [1]))
                self.assertEqual(result["state"]["status"], "done", result["state"])
                self.assertEqual(result["pages"][0]["method"], "pdf_mixed")
                self.assertIn("Native Header", " ".join(b["text"] for b in result["pages"][0]["blocks"]))

    def test_text_only_form_extracts_without_loading_ocr_engine(self):
        make_layout_pdf(self.pdf, form=True)
        source = self.manager.prepare("a" * 32, path=str(self.pdf))
        with patch.dict(os.environ, {"PROLOAG_OCR_TEST_MODE": "FAIL_LOAD"}):
            job = self.store.start(source, [1])
        result = self.wait(job)
        self.assertEqual(result["state"]["status"], "done", result["state"])
        self.assertEqual(result["pages"][0]["method"], "pdf_text")
        self.assertIn("Native Header", " ".join(b["text"] for b in result["pages"][0]["blocks"]))

    def test_pdf_crop_is_rendered_at_region_resolution_without_large_page_allocation(self):
        from app_core import ocr_worker as worker
        make_layout_pdf(self.pdf, size=(6000, 8000))
        image, full = worker.render(self.pdf, ".pdf", 1, crop=[0, 0, 0.1, 0.1])
        self.assertGreaterEqual(image.width, 1600)
        self.assertLessEqual(max(image.size), 2400)
        self.assertGreater(full[1], 20000)
        image.close()
        image, _ = worker.render(self.pdf, ".pdf", 1, crop=[0, 0, 0.1, 0.1], profile="accurate")
        self.assertGreaterEqual(image.width, 2300)
        self.assertLessEqual(max(image.size), 3200)
        image.close()

    def test_intrinsic_pdf_rotation_and_cropbox_native_box_matches_visible_text(self):
        from app_core import ocr_worker as worker
        make_layout_pdf(self.pdf, size=(800, 1000), rotation=90, cropbox=(100, 100, 700, 900))
        native, regions = worker.native_content(self.pdf, 1)
        self.assertFalse(regions)
        self.assertEqual(len(native), 1)
        image, _ = worker.render(self.pdf, ".pdf", 1)
        x1, y1, x2, y2 = worker._rect(native[0])
        patch_image = image.crop((int(x1 * image.width), int(y1 * image.height),
                                  int(x2 * image.width) + 1, int(y2 * image.height) + 1)).convert("L")
        self.assertLess(patch_image.getextrema()[0], 100)
        patch_image.close()
        image.close()

    def test_mixed_cancel_between_regions_does_not_commit_incomplete_page(self):
        from app_core import ocr_worker as worker
        make_layout_pdf(self.pdf, image=True)
        request = {"path": str(self.pdf), "strategy": "auto", "transforms": {"1": ocr.transform()}}
        native = {"original_text": "Native Header", "score": None, "box": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.2], [0.1, 0.2]]}
        def recognize(_engine, _image):
            (self.root / "cancel").touch()
            return []
        with patch.object(worker, "native_content", return_value=([native], [(0.1, 0.3, 0.4, 0.5), (0.5, 0.6, 0.9, 0.9)])), \
             patch.object(worker, "recognize", side_effect=recognize) as recognition:
            with self.assertRaises(worker.OCRCancelled):
                worker.read_page(request, 1, self.root, "standard", lambda: object(), lambda **kw: None)
        self.assertEqual(recognition.call_count, 1)
        self.assertFalse((self.root / "page-1.json").exists())

    def test_rotated_mixed_cropbox_image_region_matches_visible_image(self):
        from app_core import ocr_worker as worker
        make_layout_pdf(self.pdf, size=(800, 1000), rotation=90,
                        cropbox=(100, 100, 700, 900), image=True)
        native, regions = worker.native_content(self.pdf, 1)
        self.assertEqual(len(native), 1)
        self.assertEqual(len(regions), 1)
        image, _ = worker.render(self.pdf, ".pdf", 1, crop=list(regions[0]))
        center = image.getpixel((image.width // 2, image.height // 2))
        self.assertTrue(all(abs(v - 180) <= 2 for v in center), (regions, center))
        image.close()


if __name__ == "__main__":
    unittest.main()
