"""OCR/PDF isolation process; no Streamlit or personal record-store access."""

from __future__ import annotations

from contextlib import closing
import math
import os
from pathlib import Path
import sys
import threading
import time
import warnings

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_core.ocr_service import OCRError, transform
from app_core.transcription import atomic_json, read_json, file_lock

MAX_PIXELS = 40_000_000
PROFILES = {
    "standard": {"dpi": 200, "max_side": 2400},
    "accurate": {"dpi": 320, "max_side": 3200},
}


def profile_options(profile="standard"):
    if not isinstance(profile, str) or profile not in PROFILES:
        raise OCRError("読み取り品質を標準または高精度から選び直してください。")
    return PROFILES[profile]


def image_open(path):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as incoming:
            if incoming.format not in {"JPEG", "PNG"}:
                raise OCRError("JPEG・PNGの画像を選択してください。")
            if incoming.width * incoming.height > MAX_PIXELS:
                raise OCRError("画像が大きすぎます。4000万画素以内に縮小してください。")
            image = ImageOps.exif_transpose(incoming)
            if image.mode == "RGBA" or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                rgba.close()
                image.close()
                return background
            if image.mode == "RGB":
                return image
            converted = image.convert("RGB")
            image.close()
            return converted


def pdf_open(path):
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(path)
    except Exception as exc:
        raise OCRError(
            "PDFを開けません。パスワード保護・破損がないPDFを指定してください。"
        ) from exc


def inspect(request):
    path = Path(request["path"])
    suffix = request["suffix"]
    if suffix == ".pdf":
        with path.open("rb") as source:
            signature = source.read(1024)
        if not signature.lstrip().startswith(b"%PDF-"):
            raise OCRError("ファイルの内容がPDFではありません。")
        with closing(pdf_open(path)) as document:
            pages = len(document)
            if not 0 < pages <= 10000:
                raise OCRError("1〜10000ページのPDFを指定してください。")
            return {"kind": "pdf", "pages": pages}
    with closing(image_open(path)) as image:
        return {"kind": "image", "pages": 1, "width": image.width, "height": image.height}


def render(path, suffix, number, rotation=0, crop=None, *, profile="standard"):
    settings = transform({"rotation": rotation, "crop": crop or [0, 0, 1, 1]})
    options = profile_options(profile)
    x1, y1, x2, y2 = settings["crop"]
    if suffix == ".pdf":
        with closing(pdf_open(path)) as document:
            if not 1 <= number <= len(document):
                raise OCRError("ページ番号を確認してください。")
            with closing(document[number - 1]) as page:
                width, height = page.get_size()
                if not all(math.isfinite(v) and 0 < v <= 10_000_000 for v in (width, height)):
                    raise OCRError("ページの大きさを取得できません。")
                if rotation in (90, 270):
                    width, height = height, width
                # Render only the selected area. Shrinking the entire page
                # first permanently discards small glyphs before a crop.
                selected_w, selected_h = width * (x2 - x1), height * (y2 - y1)
                scale = min(options["dpi"] / 72, options["max_side"] / max(selected_w, selected_h))
                original_size = (math.ceil(width * scale), math.ceil(height * scale))
                cuts = (x1 * width, (1 - y2) * height, (1 - x2) * width, y1 * height)
                with closing(page.render(scale=scale, rotation=rotation, crop=cuts)) as bitmap:
                    image = bitmap.to_pil().convert("RGB")
    else:
        image = image_open(path)
        if rotation:
            rotated = image.rotate(-rotation, expand=True)
            image.close()
            image = rotated
        original_size = image.size
        if settings["crop"] != [0.0, 0.0, 1.0, 1.0]:
            cropped = image.crop((round(x1 * image.width), round(y1 * image.height),
                                  round(x2 * image.width), round(y2 * image.height)))
            image.close()
            image = cropped
    from PIL import Image

    image.thumbnail((options["max_side"], options["max_side"]), Image.Resampling.LANCZOS)
    if min(image.size) < 16:
        image.close()
        raise OCRError("読み取り範囲が小さすぎます。範囲を広げてください。")
    return image, original_size


def preview(request):
    image, _ = render(
        request["path"], request["suffix"], request["page"], request["rotation"]
    )
    try:
        image.thumbnail((1200, 1200))
        image.save(request["image"], "PNG")
        return {"width": image.width, "height": image.height}
    finally:
        image.close()


def _rect(block):
    points = block["box"]
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def _normalized_box(rect, bbox, rotation):
    left, bottom, right, top = bbox
    width, height = right - left, top - bottom
    if width <= 0 or height <= 0:
        raise OCRError("PDFの表示領域を確認できません。画像としてOCRをお試しください。")
    x1, y1, x2, y2 = rect
    points = [(x1, y2), (x2, y2), (x2, y1), (x1, y1)]
    result = []
    for x, y in points:
        x, y = (x - left) / width, (top - y) / height
        if rotation == 90:
            x, y = 1 - y, x
        elif rotation == 180:
            x, y = 1 - x, 1 - y
        elif rotation == 270:
            x, y = y, 1 - x
        result.append([max(0.0, min(1.0, x)), max(0.0, min(1.0, y))])
    return result


def native_content(path, number):
    """Return native text and safe image areas in displayed-page coordinates.

    Form XObjects may contain transformed nested images. If their coordinates
    cannot be established cheaply, OCR the page as a fallback and merge it
    with native text rather than guessing a nested image's location.
    """
    with closing(pdf_open(path)) as document, closing(document[number - 1]) as page:
        bbox, rotation = page.get_bbox(), page.get_rotation()
        if rotation not in (0, 90, 180, 270) or not all(math.isfinite(v) for v in bbox):
            return [], [(0.0, 0.0, 1.0, 1.0)]
        with closing(page.get_textpage()) as textpage:
            count = textpage.count_rects()
            if count > 10000:
                raise OCRError(
                    "ページの文字領域が多すぎます。画像としてOCRをお試しください。"
                )
            blocks = []
            for index in range(count):
                rect = textpage.get_rect(index)
                left, bottom, right, top = rect
                left, bottom = max(left, bbox[0]), max(bottom, bbox[1])
                right, top = min(right, bbox[2]), min(top, bbox[3])
                if right <= left or top <= bottom:
                    continue
                rect = (left, bottom, right, top)
                text = textpage.get_text_bounded(
                    left=left, bottom=bottom, right=right, top=top
                ).strip()
                if not text:
                    continue
                block = {
                        "original_text": text.replace("\r\n", "\n"),
                        "score": None,
                        "box": _normalized_box(rect, bbox, rotation),
                    }
                a, b, c, d = _rect(block)
                if c > a and d > b:
                    blocks.append(block)
        from pypdfium2 import raw as pdfium_c
        regions, fallback = [], False
        try:
            for index, obj in enumerate(page.get_objects(max_depth=4)):
                if index >= 10000:
                    fallback = True
                    break
                if obj.type == pdfium_c.FPDF_PAGEOBJ_FORM:
                    # Text-only Forms are common in generated PDFs and do not
                    # need OCR. Unvisited deeper Forms may still hide images.
                    if obj.level >= 3:
                        fallback = True
                elif obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
                    if obj.level:
                        fallback = True
                        continue
                    bounds = obj.get_bounds()
                    if not all(math.isfinite(v) for v in bounds):
                        fallback = True
                        continue
                    points = _normalized_box(bounds, bbox, rotation)
                    region = _rect({"box": points})
                    if region[2] > region[0] and region[3] > region[1]:
                        regions.append(region)
                    if len(regions) > 32:
                        fallback = True
                        break
        except Exception:
            # Failure to enumerate an image must not suppress its text.
            fallback = True
        width, height = page.get_size()
        padded = []
        for x1, y1, x2, y2 in regions:
            # Tiny icons must not make an otherwise valid mixed page fail the
            # minimum crop size. Expand their context, bounded by the page.
            min_width, min_height = min(1.0, max(0.011, 6 / width)), min(1.0, max(0.011, 6 / height))
            dx, dy = max(0.0, min_width - (x2 - x1)), max(0.0, min_height - (y2 - y1))
            left, top = max(0.0, x1 - dx / 2), max(0.0, y1 - dy / 2)
            right, bottom = min(1.0, x2 + dx / 2), min(1.0, y2 + dy / 2)
            if right - left < min_width:
                left, right = max(0.0, right - min_width), min(1.0, left + min_width)
            if bottom - top < min_height:
                top, bottom = max(0.0, bottom - min_height), min(1.0, top + min_height)
            padded.append((left, top, right, bottom))
        return blocks, [(0.0, 0.0, 1.0, 1.0)] if fallback else merge_regions(padded)


def merge_regions(regions):
    """Coalesce overlapping image objects to avoid duplicate OCR passes."""
    result = []
    for region in sorted(regions):
        pending = tuple(region)
        changed = True
        while changed:
            changed = False
            for i, other in enumerate(result):
                if (min(pending[2], other[2]) > max(pending[0], other[0])
                        and min(pending[3], other[3]) > max(pending[1], other[1])):
                    pending = (min(pending[0], other[0]), min(pending[1], other[1]),
                               max(pending[2], other[2]), max(pending[3], other[3]))
                    result.pop(i)
                    changed = True
                    break
        result.append(pending)
    return sorted(result, key=lambda rect: (rect[1], rect[0]))


def _intersection(a, b):
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _covered(candidate, existing):
    rect = _rect(candidate)
    area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    if area <= 0:
        return True
    # Native text wins where a text layer already describes this location.
    return sum(_intersection(rect, _rect(block)) for block in existing) >= area * 0.8


def merge_blocks(native, recognized):
    """Insert image text near same-column native neighbors, preserving native order."""
    additions = []
    for block in recognized:
        if not _covered(block, [*native, *additions]):
            additions.append(block)
    if not native:
        return additions
    buckets = {}
    for block in additions:
        x1, y1, x2, y2 = _rect(block)
        above, below, global_above, global_below = [], [], [], []
        for i, existing in enumerate(native):
            a, b, c, d = _rect(existing)
            if d <= y1 + 0.005:
                global_above.append((y1 - d, i + 1))
            elif b >= y2 - 0.005:
                global_below.append((b - y2, i))
            overlap = min(x2, c) - max(x1, a)
            if overlap <= 0 or overlap < min(x2 - x1, c - a) * 0.2:
                continue
            if d <= y1 + 0.005:
                above.append((y1 - d, i + 1))
            elif b >= y2 - 0.005:
                below.append((b - y2, i))
        if not above and not below:
            neighbors = global_above + global_below
            anchor = min(neighbors)[1] if neighbors else len(native)
        else:
            anchor = min(above)[1] if above else min(below)[1]
        buckets.setdefault(anchor, []).append(block)
    result = []
    for i in range(len(native) + 1):
        result.extend(sorted(buckets.get(i, []), key=lambda block: (_rect(block)[1], _rect(block)[0])))
        if i < len(native):
            result.append(native[i])
    return result


def _mapped_blocks(blocks, region):
    x1, y1, x2, y2 = region
    return [{**block, "box": [[x1 + x * (x2 - x1), y1 + y * (y2 - y1)]
                               for x, y in block["box"]]} for block in blocks]


def _mask_native(image, native, region):
    from PIL import ImageDraw

    x1, y1, x2, y2 = region
    masked = image.copy()
    draw = ImageDraw.Draw(masked)
    for block in native:
        a, b, c, d = _rect(block)
        if _intersection((a, b, c, d), region) <= 0:
            continue
        box = (max(0, (a - x1) / (x2 - x1) * image.width),
               max(0, (b - y1) / (y2 - y1) * image.height),
               min(image.width, (c - x1) / (x2 - x1) * image.width),
               min(image.height, (d - y1) / (y2 - y1) * image.height))
        draw.rectangle(box, fill="white")
    return masked


def make_engine(root, profile="standard"):
    from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

    options = profile_options(profile)
    model = ModelType.MEDIUM if profile == "accurate" else ModelType.SMALL

    return RapidOCR(
        params={
            "Global.model_root_dir": str(Path(root) / "tools/ocr-models"),
            "Global.text_score": 0.0,
            "Global.log_level": "warning",
            "Global.max_side_len": options["max_side"],
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.ocr_version": OCRVersion.PPOCRV6,
            # Keep page-wide detection practical on CPU. Accurate mode raises
            # image resolution and uses the medium recognizer for glyphs;
            # it does not require the much slower medium detector weights.
            "Det.model_type": ModelType.SMALL,
            "Det.limit_type": "max",
            "Det.limit_side_len": options["max_side"],
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_type": model,
            "EngineConfig.onnxruntime.use_cuda": False,
            "EngineConfig.onnxruntime.intra_op_num_threads": 4,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
    )


def recognize(engine, image):
    import numpy as np

    # RapidOCR uses OpenCV's BGR convention for ndarray input.
    output = engine(np.asarray(image)[:, :, ::-1].copy())
    if not output.txts:
        return []
    blocks = []
    for text, score, box in zip(output.txts, output.scores, output.boxes):
        if not text.strip():
            continue
        score = float(score)
        if not math.isfinite(score):
            score = 0.0
        blocks.append(
            {
                "original_text": str(text),
                "score": max(0.0, min(1.0, score)),
                "box": [
                    [
                        max(0.0, min(1.0, float(x) / image.width)),
                        max(0.0, min(1.0, float(y) / image.height)),
                    ]
                    for x, y in box
                ],
            }
        )
    return blocks


class OCRCancelled(Exception):
    pass


def _check_cancel(folder):
    if (folder / "cancel").exists():
        raise OCRCancelled()


def read_page(request, number, folder, profile, get_engine, update):
    settings = request["transforms"][str(number)]
    _check_cancel(folder)
    update(status="reading", message=f"{number}ページを準備しています")
    image, original_size = render(request["path"], Path(request["path"]).suffix.lower(), number,
                                  profile=profile, **settings)
    try:
        blocks, regions = [], []
        native = (request["strategy"] == "auto" and Path(request["path"]).suffix.lower() == ".pdf"
                  and settings == {"rotation": 0, "crop": [0.0, 0.0, 1.0, 1.0]})
        if native:
            blocks, regions = native_content(request["path"], number)
        method = "pdf_text" if blocks else "ocr"
        if blocks and regions:
            additions = []
            for region in regions:
                _check_cancel(folder)
                part, _ = render(request["path"], ".pdf", number, crop=list(region), profile=profile)
                try:
                    with closing(_mask_native(part, blocks, region)) as masked:
                        current_engine = get_engine()
                        _check_cancel(folder)
                        update(status="reading", message=f"{number}ページの画像内の文字を読み取っています")
                        additions.extend(_mapped_blocks(recognize(current_engine, masked), region))
                finally:
                    part.close()
            blocks = merge_blocks(blocks, additions)
            method = "pdf_mixed"
        elif not blocks:
            current_engine = get_engine()
            _check_cancel(folder)
            update(status="reading", message=f"{number}ページを読み取っています")
            blocks = recognize(current_engine, image)
        _check_cancel(folder)
        for index, block in enumerate(blocks):
            block["id"] = f"p{number}b{index}"
        page = {"number": number, "method": method, "transform": settings,
                "width": image.width, "height": image.height, "rotated_full_size": list(original_size), "blocks": blocks}
        image.thumbnail((1200, 1200))
        image.save(folder / f"page-{number}.png", "PNG")
        _check_cancel(folder)
        atomic_json(folder / f"page-{number}.json", page)
        return page
    finally:
        image.close()


def run(request_path):
    request_path = Path(request_path).resolve()
    folder = request_path.parent
    request = read_json(request_path)
    if folder.name != request["id"]:
        raise OCRError("読み取りの保存先が不正です。")
    state = read_json(folder / "state.json")
    state_lock = threading.Lock()
    stop = threading.Event()
    started = time.monotonic()

    def update(**values):
        with state_lock:
            state.update(
                values, elapsed=time.monotonic() - started, heartbeat=time.time()
            )
            atomic_json(folder / "state.json", state)

    def watch():
        while not stop.wait(0.2):
            if (folder / "cancel").exists() or time.monotonic() - started > 12 * 3600:
                try:
                    update(
                        status="cancelled", message="読み取りを中止しました", error=""
                    )
                finally:
                    os._exit(0)

    def heartbeat():
        while not stop.wait(2):
            update()

    with file_lock(folder / ".worker.lock", blocking=False):
        threading.Thread(target=watch, daemon=True).start()
        threading.Thread(target=heartbeat, daemon=True).start()
        try:
            inspect(
                {
                    "path": request["path"],
                    "suffix": Path(request["path"]).suffix.lower(),
                }
            )
            engine = None
            profile = request.get("profile", "standard")
            profile_options(profile)

            def get_engine():
                nonlocal engine
                if engine is None:
                    update(status="loading", message="認識モデルを準備しています（初回はダウンロード）")
                    engine = make_engine(request["root"], profile)
                return engine

            completed = []
            for number in request["pages"]:
                read_page(request, number, folder, profile, get_engine, update)
                completed.append(number)
                update(
                    completed_pages=list(completed),
                    progress=len(completed) / len(request["pages"]),
                )
                if (
                    sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
                    > 2 * 1024**3
                ):
                    raise OCRError(
                        "この履歴が2GBを超えました。ページ数を分けて再実行してください。完了したページは保持しています。"
                    )
            stop.set()
            update(status="done", progress=1.0, message="読み取りが完了しました")
        except OCRCancelled:
            stop.set()
            update(status="cancelled", message="読み取りを中止しました", error="")
        except Exception as exc:
            import traceback

            traceback.print_exc()
            stop.set()
            update(
                status="failed",
                message="読み取りを完了できませんでした",
                error=f"{type(exc).__name__}: {str(exc)[:1200]}",
            )
        finally:
            stop.set()


if __name__ == "__main__":
    action, path = sys.argv[1:3]
    if action == "run":
        run(path)
    else:
        request = read_json(path)
        try:
            result = inspect(request) if action == "inspect" else preview(request)
            atomic_json(request["output"], result)
        except Exception as exc:
            atomic_json(request["output"], {"error": str(exc)[:1000]})
            raise SystemExit(1)
