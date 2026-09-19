import json

import cv2
import numpy as np
import pytest

from qa_screens.config import load_config
from qa_screens.diff import compute_diff, make_preview
from qa_screens.errors import QAUserError


def test_route_mapping_monorepo_style(tmp_path):
    (tmp_path / ".qa-screens.json").write_text(json.dumps({
        "route_template": "/nl-be/{name}/", "routes": {"home": "/", "nl-be": "/nl-be/"}, "base_url": "http://x:8080",
    }))
    cfg = load_config(tmp_path)
    assert cfg.url_for("docenten") == "http://x:8080/nl-be/docenten/"
    assert cfg.url_for("docenten-mobile") == "http://x:8080/nl-be/docenten/"
    assert cfg.url_for("nl-be-mobile") == "http://x:8080/nl-be/"
    assert cfg.url_for("home") == "http://x:8080/"
    assert cfg.is_mobile("faq-mobile") and not cfg.is_mobile("faq")


def test_unknown_config_key_rejected(tmp_path):
    (tmp_path / ".qa-screens.json").write_text('{"treshold": 0.9}')
    with pytest.raises(QAUserError, match="treshold"):
        load_config(tmp_path)


def _img(path, h=600, w=400, box=None):
    im = np.full((h, w, 3), 255, np.uint8)
    cv2.rectangle(im, (20, 20), (380, 80), (40, 120, 40), -1)
    if box:
        x, y, bw, bh = box
        cv2.rectangle(im, (x, y), (x + bw, y + bh), (0, 0, 0), -1)
    cv2.imwrite(str(path), im)
    return str(path)


def test_identical_images_score_one(tmp_path):
    a = _img(tmp_path / "a.png")
    r = compute_diff(a, a, str(tmp_path / "d.png"), str(tmp_path / "m.png"))
    assert r["score"] == pytest.approx(1.0)
    assert r["regions"] == []


def test_changed_region_is_located(tmp_path):
    a = _img(tmp_path / "a.png")
    b = _img(tmp_path / "b.png", box=(200, 300, 60, 60))
    r = compute_diff(a, b, str(tmp_path / "d.png"))
    assert r["score"] < 0.99
    top = r["regions"][0]
    assert top["x"] <= 200 <= top["x"] + top["width"]
    assert top["y"] <= 300 <= top["y"] + top["height"]
    preview = make_preview(a, b, r["regions"], str(tmp_path / "p.png"))
    assert cv2.imread(preview) is not None


def test_align_modes_on_height_change(tmp_path):
    a = _img(tmp_path / "a.png", h=600)
    b = _img(tmp_path / "b.png", h=700, box=(20, 620, 360, 60))  # content in the extra height
    assert compute_diff(a, b, align="crop")["score"] == pytest.approx(1.0)
    assert compute_diff(a, b, align="pad")["score"] < 0.99
    assert compute_diff(a, b, align="pad")["size_mismatch"]
    with pytest.raises(QAUserError):
        compute_diff(a, b, align="stretch")
