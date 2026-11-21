"""OCR precision policy, crop geometry and mixed-page merge invariants."""
from enum import Enum
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from app_core import ocr_worker as worker


def block(text, rect, score=None):
    a, b, c, d = rect
    return {"original_text": text, "score": score, "box": [[a, b], [c, b], [c, d], [a, d]]}


class OCRWorkerQualityTests(unittest.TestCase):
    def test_profile_keeps_cpu_small_detection_and_uses_medium_recognition_when_accurate(self):
        class ModelType(Enum):
            SMALL = "small"
            MEDIUM = "medium"

        class EngineType(Enum):
            ONNXRUNTIME = "onnxruntime"

        class OCRVersion(Enum):
            PPOCRV6 = "PP-OCRv6"

        fake = SimpleNamespace(ModelType=ModelType, EngineType=EngineType, OCRVersion=OCRVersion,
                               RapidOCR=lambda **values: values["params"])
        with patch.dict(sys.modules, {"rapidocr": fake}):
            normal = worker.make_engine(Path("unused"))
            accurate = worker.make_engine(Path("unused"), "accurate")
        self.assertEqual(normal["Rec.model_type"], ModelType.SMALL)
        self.assertEqual(normal["Det.model_type"], ModelType.SMALL)
        self.assertEqual(accurate["Det.model_type"], ModelType.SMALL)
        self.assertEqual(accurate["Rec.model_type"], ModelType.MEDIUM)
        self.assertEqual(accurate["Global.max_side_len"], 3200)
        self.assertEqual(accurate["Det.limit_type"], "max")
        self.assertEqual(accurate["Det.limit_side_len"], 3200)
        self.assertFalse(accurate["EngineConfig.onnxruntime.use_cuda"])
        for invalid in ("best", "cuda", None, {}):
            with self.subTest(invalid=invalid), self.assertRaises(worker.OCRError):
                worker.profile_options(invalid)

    def test_crop_first_image_keeps_region_pixels_and_source_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="proloag_ocr_crop_") as folder:
            path = Path(folder) / "large.png"
            Image.new("RGB", (4000, 3000), "white").save(path)
            original = path.read_bytes()
            image, full = worker.render(path, ".png", 1, crop=[0, 0, 0.25, 0.25])
            self.assertEqual(image.size, (1000, 750))
            self.assertEqual(full, (4000, 3000))
            image.close()
            self.assertEqual(path.read_bytes(), original)

    def test_accurate_full_image_is_bounded_and_rotation_crop_coordinates_agree(self):
        with tempfile.TemporaryDirectory(prefix="proloag_ocr_bounds_") as folder:
            path = Path(folder) / "source.png"
            Image.new("RGB", (4000, 3000), "white").save(path)
            image, full = worker.render(path, ".png", 1, profile="accurate")
            self.assertEqual(image.size, (3200, 2400))
            image.close()
            image, full = worker.render(path, ".png", 1, rotation=90, crop=[0, 0, 0.5, 1])
            self.assertEqual(full, (3000, 4000))
            self.assertEqual(image.size, (900, 2400))
            image.close()

    def test_native_pdf_coordinates_account_for_cropbox_and_intrinsic_rotation(self):
        rect, bbox = (150, 700, 250, 750), (100, 100, 700, 900)
        unrotated = worker._normalized_box(rect, bbox, 0)
        self.assertAlmostEqual(unrotated[0][0], 50 / 600)
        self.assertAlmostEqual(unrotated[0][1], 150 / 800)
        for rotation in (0, 90, 180, 270):
            points = worker._normalized_box(rect, bbox, rotation)
            self.assertTrue(all(0 <= v <= 1 for point in points for v in point))
            if rotation == 90:
                self.assertAlmostEqual(points[0][0], 1 - 150 / 800)
                self.assertAlmostEqual(points[0][1], 50 / 600)

    def test_mixed_merge_keeps_native_sequence_and_inserts_same_column_image_text(self):
        left_top = block("native left top", (0.1, 0.1, 0.4, 0.15))
        left_bottom = block("native left bottom", (0.1, 0.6, 0.4, 0.65))
        right_top = block("native right top", (0.6, 0.1, 0.9, 0.15))
        image_text = block("image text", (0.1, 0.3, 0.4, 0.35), 0.8)
        result = worker.merge_blocks([left_top, left_bottom, right_top], [image_text])
        self.assertEqual([b["original_text"] for b in result],
                         ["native left top", "image text", "native left bottom", "native right top"])
        self.assertIs(result[0], left_top)
        self.assertIs(result[2], left_bottom)

    def test_mixed_merge_native_wins_same_location_but_repeated_text_elsewhere_survives(self):
        native = block("Header", (0.1, 0.1, 0.6, 0.2))
        duplicate = block("Hcader", (0.11, 0.11, 0.59, 0.19), 0.4)
        separate = block("Header", (0.1, 0.5, 0.6, 0.6), 0.8)
        result = worker.merge_blocks([native], [duplicate, separate])
        self.assertEqual(result, [native, separate])

    def test_mixed_merge_image_body_precedes_native_footer_without_column_overlap(self):
        footer = block("PAGE 1", (0.02, 0.9, 0.12, 0.95))
        body = block("image body", (0.2, 0.3, 0.8, 0.5), 0.9)
        self.assertEqual(worker.merge_blocks([footer], [body]), [body, footer])

    def test_mixed_merge_without_column_overlap_uses_closest_vertical_anchor(self):
        later_column = block("later column top", (0.8, 0.1, 0.95, 0.15))
        closer = block("nearby native caption", (0.02, 0.6, 0.12, 0.65))
        body = block("image body", (0.2, 0.5, 0.7, 0.55), 0.9)
        # Native PDF extraction order can differ from geometric order. Retain
        # its sequence, and insert next to the nearest available vertical box.
        self.assertEqual(worker.merge_blocks([closer, later_column], [body]),
                         [body, closer, later_column])

    def test_region_mapping_and_mask_do_not_change_original_preview(self):
        image = Image.new("RGB", (200, 200), "black")
        native = block("native", (0.25, 0.25, 0.5, 0.5))
        masked = worker._mask_native(image, [native], (0.25, 0.25, 0.75, 0.75))
        self.assertEqual(image.getpixel((20, 20)), (0, 0, 0))
        self.assertEqual(masked.getpixel((20, 20)), (255, 255, 255))
        self.assertEqual(masked.getpixel((180, 180)), (0, 0, 0))
        mapped = worker._mapped_blocks([block("region", (0, 0, 1, 1), 0.9)], (0.25, 0.25, 0.75, 0.75))
        self.assertEqual(worker._rect(mapped[0]), (0.25, 0.25, 0.75, 0.75))
        image.close()
        masked.close()

    def test_overlapping_images_coalesce_transitively_without_merging_separate_images(self):
        regions = [(0, 0, 0.3, 0.3), (0.2, 0.2, 0.5, 0.5), (0.4, 0.4, 0.7, 0.7), (0.8, 0.8, 1, 1)]
        self.assertEqual(worker.merge_regions(regions), [(0, 0, 0.7, 0.7), (0.8, 0.8, 1, 1)])


if __name__ == "__main__":
    unittest.main()
