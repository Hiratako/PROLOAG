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
MAX_SIDE = 2400


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
                background = Image.new("RGBA", rgba.size, "white")
                image = Image.alpha_composite(background, rgba)
            return image.convert("RGB")


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
        if not path.open("rb").read(1024).lstrip().startswith(b"%PDF-"):
            raise OCRError("ファイルの内容がPDFではありません。")
        with closing(pdf_open(path)) as document:
            pages = len(document)
            if not 0 < pages <= 10000:
                raise OCRError("1〜10000ページのPDFを指定してください。")
            return {"kind": "pdf", "pages": pages}
    image = image_open(path)
    return {"kind": "image", "pages": 1, "width": image.width, "height": image.height}


def render(path, suffix, number, rotation=0, crop=None):
    settings = transform({"rotation": rotation, "crop": crop or [0, 0, 1, 1]})
    if suffix == ".pdf":
        with closing(pdf_open(path)) as document:
            if not 1 <= number <= len(document):
                raise OCRError("ページ番号を確認してください。")
            with closing(document[number - 1]) as page:
                width, height = page.get_size()
                if not width > 0 or not height > 0:
                    raise OCRError("ページの大きさを取得できません。")
                scale = min(200 / 72, MAX_SIDE / max(width, height))
                with closing(page.render(scale=scale)) as bitmap:
                    image = bitmap.to_pil().convert("RGB")
    else:
        image = image_open(path)
    from PIL import Image

    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    image = image.rotate(-rotation, expand=True)
    original_size = image.size
    x1, y1, x2, y2 = settings["crop"]
    box = (
        round(x1 * image.width),
        round(y1 * image.height),
        round(x2 * image.width),
        round(y2 * image.height),
    )
    image = image.crop(box)
    if min(image.size) < 16:
        raise OCRError("読み取り範囲が小さすぎます。範囲を広げてください。")
    return image, original_size


def preview(request):
    image, _ = render(
        request["path"], request["suffix"], request["page"], request["rotation"]
    )
    image.thumbnail((1200, 1200))
    image.save(request["image"], "PNG")
    return {"width": image.width, "height": image.height}


def native_blocks(path, number):
    with closing(pdf_open(path)) as document, closing(document[number - 1]) as page:
        # PDF intrinsic rotations have different text coordinate conventions.
        if page.get_rotation():
            return []
        width, height = page.get_size()
        with closing(page.get_textpage()) as textpage:
            count = textpage.count_rects()
            if count > 10000:
                raise OCRError(
                    "ページの文字領域が多すぎます。画像としてOCRをお試しください。"
                )
            blocks = []
            for index in range(count):
                left, bottom, right, top = textpage.get_rect(index)
                text = textpage.get_text_bounded(
                    left=left, bottom=bottom, right=right, top=top
                ).strip()
                if not text:
                    continue
                blocks.append(
                    {
                        "original_text": text.replace("\r\n", "\n"),
                        "score": None,
                        "box": [
                            [left / width, 1 - top / height],
                            [right / width, 1 - top / height],
                            [right / width, 1 - bottom / height],
                            [left / width, 1 - bottom / height],
                        ],
                    }
                )
            return blocks


def make_engine(root):
    from rapidocr import RapidOCR

    return RapidOCR(
        params={
            "Global.model_root_dir": str(Path(root) / "tools/ocr-models"),
            "Global.text_score": 0.0,
            "Global.log_level": "warning",
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
            completed = []
            for number in request["pages"]:
                settings = request["transforms"][str(number)]
                update(status="reading", message=f"{number}ページを準備しています")
                image, original_size = render(
                    request["path"],
                    Path(request["path"]).suffix.lower(),
                    number,
                    **settings,
                )
                blocks = []
                native = (
                    request["strategy"] == "auto"
                    and Path(request["path"]).suffix == ".pdf"
                    and settings == {"rotation": 0, "crop": [0.0, 0.0, 1.0, 1.0]}
                )
                if native:
                    blocks = native_blocks(request["path"], number)
                method = "pdf_text" if blocks else "ocr"
                if not blocks:
                    if engine is None:
                        update(
                            status="loading",
                            message="認識モデルを準備しています（初回はダウンロード）",
                        )
                        engine = make_engine(request["root"])
                    update(
                        status="reading", message=f"{number}ページを読み取っています"
                    )
                    blocks = recognize(engine, image)
                for index, block in enumerate(blocks):
                    block["id"] = f"p{number}b{index}"
                page = {
                    "number": number,
                    "method": method,
                    "transform": settings,
                    "width": image.width,
                    "height": image.height,
                    "rotated_full_size": list(original_size),
                    "blocks": blocks,
                }
                preview_image = image.copy()
                preview_image.thumbnail((1200, 1200))
                preview_image.save(folder / f"page-{number}.png", "PNG")
                atomic_json(folder / f"page-{number}.json", page)
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
