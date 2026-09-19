"""SSIM image comparison with heatmaps, changed-region boxes and AI-sized previews."""
from __future__ import annotations

import logging
import os

import cv2
import numpy as np
from PIL import Image, ImageDraw
from skimage.metrics import structural_similarity as ssim

from .errors import QAUserError

logger = logging.getLogger("qa_screens.diff")

# Full-page captures can be hundreds of megapixels; beyond this we decode at
# reduced resolution (1/2, 1/4, 1/8) so SSIM never OOMs the process.
MAX_PIXELS = 40_000_000
_REDUCED = {1: cv2.IMREAD_COLOR, 2: cv2.IMREAD_REDUCED_COLOR_2, 4: cv2.IMREAD_REDUCED_COLOR_4, 8: cv2.IMREAD_REDUCED_COLOR_8}


def image_size(path: str) -> tuple[int, int]:
    Image.MAX_IMAGE_PIXELS = None  # size read only; no decompression
    with Image.open(path) as im:
        return im.size


def _load(path: str, factor: int) -> np.ndarray:
    img = cv2.imread(path, _REDUCED[factor])
    if img is None:
        raise QAUserError(f"Could not read image {path}")
    return img


def _align(a: np.ndarray, b: np.ndarray, mode: str) -> tuple[np.ndarray, np.ndarray]:
    if a.shape == b.shape:
        return a, b
    if mode == "resize":  # legacy behaviour: stretch live onto reference
        return a, cv2.resize(b, (a.shape[1], a.shape[0]))
    if mode == "crop":
        h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
        return a[:h, :w], b[:h, :w]
    if mode == "pad":  # height changes count as differences
        h, w = max(a.shape[0], b.shape[0]), max(a.shape[1], b.shape[1])

        def pad(x):
            return cv2.copyMakeBorder(x, 0, h - x.shape[0], 0, w - x.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255))

        return pad(a), pad(b)
    raise QAUserError(f"Unknown align mode '{mode}' (resize | crop | pad)")


def compute_diff(ref_path: str, live_path: str, diff_path: str | None = None, mask_path: str | None = None,
                 align: str = "resize", max_regions: int = 10) -> dict:
    """SSIM between two images; writes a red-overlay heatmap and a binary mask if paths are given."""
    ref_size, live_size = image_size(ref_path), image_size(live_path)
    biggest = max(ref_size[0] * ref_size[1], live_size[0] * live_size[1])
    factor = next((f for f in (1, 2, 4, 8) if biggest / (f * f) <= MAX_PIXELS), 8)

    ref, live = _align(_load(ref_path, factor), _load(live_path, factor), align)
    ref_gray, live_gray = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), cv2.cvtColor(live, cv2.COLOR_BGR2GRAY)
    if min(ref_gray.shape) < 7:
        raise QAUserError("Images are too small to compare (min 7x7 px)")
    score, diff_map = ssim(ref_gray, live_gray, full=True, data_range=255)

    diff_inv = 255 - np.clip(diff_map * 255, 0, 255).astype("uint8")
    _, mask = cv2.threshold(diff_inv, 30, 255, cv2.THRESH_BINARY)

    # Group changed pixels into boxes (in original-resolution coordinates).
    blobs = cv2.dilate(mask, np.ones((15, 15), np.uint8), iterations=2)
    contours, _ = cv2.findContours(blobs, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = sorted((cv2.boundingRect(c) for c in contours), key=lambda r: r[2] * r[3], reverse=True)
    regions = [{"x": x * factor, "y": y * factor, "width": w * factor, "height": h * factor} for x, y, w, h in boxes[:max_regions]]

    if diff_path:
        os.makedirs(os.path.dirname(diff_path) or ".", exist_ok=True)
        heat = ref.copy()
        overlay = heat.copy()
        overlay[mask == 255] = (0, 0, 255)
        heat = cv2.addWeighted(overlay, 0.6, heat, 0.4, 0)
        for r in boxes[:max_regions]:
            x, y, w, h = r
            cv2.rectangle(heat, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.imwrite(diff_path, heat)
    if mask_path:
        cv2.imwrite(mask_path, mask)

    result = {
        "score": round(float(score), 5),
        "reference_size": list(ref_size),
        "live_size": list(live_size),
        "size_mismatch": ref_size != live_size,
        "changed_ratio": round(float(np.count_nonzero(mask)) / mask.size, 5),
        "regions": regions,
        "align": align,
    }
    if factor > 1:
        result["downscaled"] = factor
    if diff_path:
        result["diff_path"] = diff_path
    logger.info("SSIM %s vs %s = %.4f", os.path.basename(ref_path), os.path.basename(live_path), score)
    return result


def make_preview(ref_path: str, live_path: str, regions: list[dict], out_path: str,
                 max_width: int = 1500, max_height: int = 1500, pad: int = 80,
                 labels: tuple[str, str] = ("REFERENCE", "LIVE")) -> str:
    """Side-by-side crop (left | right) around the changed regions, sized for a vision model."""
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(ref_path) as a_full, Image.open(live_path) as b_full:
        a_full, b_full = a_full.convert("RGB"), b_full.convert("RGB")
        if regions:
            x0 = max(min(r["x"] for r in regions) - pad, 0)
            y0 = max(min(r["y"] for r in regions) - pad, 0)
            x1 = max(r["x"] + r["width"] for r in regions) + pad
            y1 = max(r["y"] + r["height"] for r in regions) + pad
        else:
            x0, y0, x1, y1 = 0, 0, a_full.width, min(a_full.height, 2 * a_full.width)
        # Keep the preview readable: cap crop height at ~2.5x its width.
        y1 = min(y1, y0 + int(2.5 * max(x1 - x0, 400)))
        box = (x0, y0, min(x1, max(a_full.width, b_full.width)), y1)
        a, b = a_full.crop(box), b_full.crop(box)
    gap = 12
    canvas = Image.new("RGB", (a.width + b.width + gap, max(a.height, b.height)), (255, 0, 255))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (a.width + gap, 0))
    canvas.thumbnail((max_width, max_height - 28))
    labelled = Image.new("RGB", (canvas.width, canvas.height + 28), (30, 30, 30))
    labelled.paste(canvas, (0, 28))
    draw = ImageDraw.Draw(labelled)
    half = canvas.width * a.width // (a.width + b.width + gap)
    draw.text((8, 7), f"{labels[0]}  (y {y0}-{y1}px)", fill=(255, 255, 255))
    draw.text((half + 8, 7), labels[1], fill=(255, 255, 255))
    canvas = labelled
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    canvas.save(out_path, "PNG", optimize=True)
    return out_path
