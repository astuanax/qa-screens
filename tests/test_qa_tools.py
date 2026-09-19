"""Visual QA as a developer's agent uses it: through the MCP tools."""
from __future__ import annotations

import json
import shutil

import cv2
import numpy as np
import pytest

from conftest import closed_port_url

BROKEN_CSS = "body{margin:0;font-family:serif} .hero{height:420px} main{padding:90px}"


async def make_reference(qa, project, *pages):
    await qa.ok("capture_set", label="ref-tmp", pages=list(pages))
    project.refs.mkdir(exist_ok=True)
    for p in pages:
        shutil.copy(project.runtime / "sets" / "ref-tmp" / f"{p}.png", project.refs / f"{p}.png")


def pending_reports(state):
    d = state / "reports" / "pending"
    return list(d.glob("*.json")) if d.exists() else []


def png_size(path):
    img = cv2.imread(str(path))
    return img.shape[1], img.shape[0]


# --------------------------------------------------------------------------- orientation

async def test_qa_config_lists_references_with_their_urls(qa, project, golden):
    project.configure(route_template="/site/{name}/", routes={"home": "/", "contact": "https://elsewhere.test/c"})
    cfg = (await qa.ok("qa_config")).data
    refs = {r["name"]: r for r in cfg["references"]}
    assert refs["home"]["url"] == project.app.url + "/"
    assert refs["home-mobile"] == {"name": "home-mobile", "url": project.app.url + "/", "viewport": "mobile"}
    assert refs["about"]["url"] == project.app.url + "/site/about/"
    assert refs["contact"]["url"] == "https://elsewhere.test/c"
    assert cfg["base_url"] == project.app.url


async def test_qa_config_without_config_file_uses_defaults(qa, project):
    (project.root / ".qa-screens.json").unlink()
    cfg = (await qa.ok("qa_config")).data
    assert cfg["base_url"] == "http://localhost:8080"
    assert "not present" in cfg["config_file"]
    assert cfg["references"] == []


# --------------------------------------------------------------------------- run_qa

async def test_unchanged_site_passes_and_writes_a_report(qa, project, golden):
    r = await qa.ok("run_qa")
    s = r.data
    assert (s["total"], s["passed"], s["failed"], s["errors"]) == (4, 4, 0, 0)
    assert all(p["score"] > 0.99 for p in s["pages"])
    assert r.images == []
    latest = json.loads((project.runtime / "reports" / "latest.json").read_text())
    assert latest["run_id"] == s["run_id"]
    assert json.loads(open(s["report_path"]).read())["passed"] == 4


async def test_css_regression_fails_with_regions_and_previews(qa, project, golden):
    project.app.set_css(BROKEN_CSS)
    r = await qa.ok("run_qa", max_images=2)
    s = r.data
    assert s["failed"] == 4
    worst = s["pages"][0]
    assert worst["status"] == "FAIL" and worst["score"] < worst["threshold"]
    assert worst["regions"] and {"x", "y", "width", "height"} <= set(worst["regions"][0])
    assert worst["size_mismatch"]  # the taller hero made the page longer
    assert len(r.images) == 2 and all(i.mime_type == "image/png" for i in r.images)


async def test_failures_are_listed_before_passes(qa, project, golden):
    project.app.write_page("about", color="#000", body="<h1 style='font-size:200px'>NEW</h1>")
    pages = (await qa.ok("run_qa")).data["pages"]
    assert pages[0]["page"] == "about" and pages[0]["status"] == "FAIL"
    assert [p["status"] for p in pages[1:]] == ["PASS"] * 3


async def test_viewport_and_page_filters(qa, project, golden):
    mobile = (await qa.ok("run_qa", viewport="mobile")).data
    assert [p["page"] for p in mobile["pages"]] == ["home-mobile"]
    desktop = (await qa.ok("run_qa", viewport="desktop")).data
    assert sorted(p["page"] for p in desktop["pages"]) == ["about", "contact", "home"]
    subset = (await qa.ok("run_qa", pages=["about"])).data
    assert [p["page"] for p in subset["pages"]] == ["about"]
    assert "viewport" in await qa.error("run_qa", viewport="tablet")


async def test_tolerances_can_be_set_per_call_and_by_environment(qa, project, golden, monkeypatch):
    project.app.set_css(BROKEN_CSS)
    # Both checks must be relaxed to accept a changed page: SSIM alone is not enough.
    assert (await qa.ok("run_qa", pages=["about"], threshold=0.0)).data["pages"][0]["status"] == "FAIL"
    lenient = (await qa.ok("run_qa", pages=["about"], threshold=0.0, max_changed_ratio=1.0)).data
    assert lenient["pages"][0]["status"] == "PASS"
    monkeypatch.setenv("QA_SCREENS_THRESHOLD", "0.01")
    monkeypatch.setenv("QA_SCREENS_MAX_CHANGED_RATIO", "0.5")
    row = (await qa.ok("run_qa", pages=["about"])).data["pages"][0]
    assert (row["threshold"], row["max_changed_ratio"]) == (0.01, 0.5)


async def test_recoloured_header_is_caught_even_though_ssim_barely_moves(qa, project, golden):
    """A colour regression on a small part of the page: SSIM stays ~0.99, the colour check fails it."""
    project.app.write_page("about", color="#2a6")  # reference header is #26a
    r = (await qa.ok("qa_page", page="about")).data
    assert r["score"] > r["threshold"]  # SSIM alone would have passed it
    assert r["status"] == "FAIL" and r["changed_ratio"] > r["max_changed_ratio"]
    top = r["regions"][0]
    assert top["y"] < 20 and top["height"] >= 120 and top["width"] > 1400  # the header band


async def test_small_text_edit_is_left_to_ssim(qa, project, golden):
    """Copy tweaks don't trip the colour-area check; they're judged by SSIM and still located."""
    project.app.write_page("about", color="#26a", body="<p>Lorem ipsum dolor sit amet, consectetur adipiscing elit!</p>"
                           + "<p>Lorem ipsum dolor sit amet, consectetur adipiscing elit.</p>" * 29)
    r = (await qa.ok("qa_page", page="about")).data
    assert r["changed_ratio"] < r["max_changed_ratio"]
    assert r["regions"]  # but the change is still pointed out


async def test_no_references_is_an_error_not_a_pass(qa, project):
    msg = await qa.error("run_qa")
    assert "no reference screenshots" in msg and "screenshots" in msg


async def test_missing_page_is_an_error_not_a_design_failure(qa, project, golden):
    shutil.rmtree(project.app.root / "contact")
    r = await qa.ok("run_qa")
    rows = {p["page"]: p for p in r.data["pages"]}
    assert rows["contact"]["status"] == "ERROR" and "HTTP 404" in rows["contact"]["error"]
    assert rows["home"]["status"] == rows["about"]["status"] == "PASS"  # one bad page doesn't sink the run
    assert r.data["errors"] == 1 and r.images == []


async def test_unreachable_site_reports_errors_per_page(qa, project, golden):
    r = await qa.ok("run_qa", base_url=closed_port_url(), pages=["home"])
    assert r.data["pages"][0]["status"] == "ERROR"
    assert "Nothing is running" in r.data["pages"][0]["error"]
    assert pending_reports(project.root.parent / "state") == []  # environment problem, not a bug


async def test_fix_loop_fail_then_fix_then_pass(qa, project, golden):
    """The core refactoring loop: break -> detect -> fix -> verify, in one server session."""
    assert (await qa.ok("qa_page", page="about")).data["status"] == "PASS"
    project.app.set_css(BROKEN_CSS)
    assert (await qa.ok("qa_page", page="about")).data["status"] == "FAIL"  # no stale cached CSS
    from conftest import CSS

    project.app.set_css(CSS)
    assert (await qa.ok("qa_page", page="about")).data["status"] == "PASS"


# --------------------------------------------------------------------------- qa_page

async def test_qa_page_returns_one_preview_only_on_failure(qa, project, golden):
    passed = await qa.ok("qa_page", page="home")
    assert passed.images == [] and "preview_path" not in passed.data
    project.app.write_page("home", color="#f00")
    failed = await qa.ok("qa_page", page="home")
    assert failed.data["status"] == "FAIL" and len(failed.images) == 1
    w, h = png_size(failed.data["preview_path"])
    assert w <= 1500 and h <= 1500  # sized for a vision model


async def test_missing_reference_is_a_clear_user_error(qa, project, golden):
    msg = await qa.error("qa_page", page="pricing")
    assert "Reference screenshot not found for 'pricing'" in msg
    assert pending_reports(project.root.parent / "state") == []


async def test_url_override_for_one_call(qa, project, golden):
    r = (await qa.ok("qa_page", page="home", url=project.app.url + "/about/")).data
    assert r["url"] == project.app.url + "/about/"
    assert r["status"] == "FAIL"  # about's header colour differs from home's reference


async def test_mobile_pages_use_a_mobile_viewport(qa, project, golden):
    r = (await qa.ok("qa_page", page="home-mobile")).data
    assert r["live_size"][0] == 390 * 2  # retina by default
    project.configure(mobile_device_scale_factor=1)
    await make_reference(qa, project, "about-mobile")
    assert (await qa.ok("qa_page", page="about-mobile")).data["live_size"][0] == 390


async def test_mobile_uses_mobile_user_agent(qa, project, golden):
    await qa.ok("qa_page", page="home-mobile")
    ua = project.app.last("/home/")["headers"]["User-Agent"]
    assert "iPhone" in ua


async def test_width_mismatch_is_explained(qa, project, golden):
    project.configure(mobile_device_scale_factor=1)  # references were captured at 2x
    r = (await qa.ok("qa_page", page="home-mobile")).data
    assert "Width differs" in r["warning"] and "mobile_device_scale_factor" in r["warning"]


async def test_align_pad_catches_content_added_below_the_fold(qa, project, golden):
    project.app.write_page("about", color="#26a", extra="<div style='height:600px;background:#000'></div>")
    crop = (await qa.ok("qa_page", page="about", align="crop")).data
    pad = (await qa.ok("qa_page", page="about", align="pad")).data
    assert crop["status"] == "PASS"
    assert pad["status"] == "FAIL" and pad["size_mismatch"]
    assert "stretch" in await qa.error("qa_page", page="about", align="stretch")


async def test_mask_selectors_hide_dynamic_content(qa, project):
    # A banner that alternates black/white on every load (think: carousel, clock, ads).
    project.app.write_page("promo", extra=(
        "<div id=ad style='height:600px'>SALE</div><script>"
        "const n=+(localStorage.n||0);localStorage.n=n+1;"
        "ad.style.background=n%2?'#000':'#fff'</script>"))
    await make_reference(qa, project, "promo")
    assert (await qa.ok("qa_page", page="promo")).data["status"] == "FAIL"
    project.configure(mask_selectors=["#ad"])
    await make_reference(qa, project, "promo")
    for _ in range(2):
        assert (await qa.ok("qa_page", page="promo")).data["status"] == "PASS"


async def test_redirect_to_login_is_flagged(qa, project):
    await make_reference(qa, project, "home")
    shutil.copy(project.refs / "home.png", project.refs / "private.png")
    r = (await qa.ok("qa_page", page="private")).data
    assert "Redirected" in r["warning"] and "/login" in r["warning"]


async def test_corrupt_reference_is_a_user_error(qa, project, golden):
    (project.refs / "home.png").write_bytes(b"not a png")
    assert "Not a readable image" in await qa.error("qa_page", page="home")
    assert pending_reports(project.root.parent / "state") == []


async def test_jpeg_references_pass_despite_compression_artifacts(qa, project, golden):
    img = cv2.imread(str(project.refs / "about.png"))
    cv2.imwrite(str(project.refs / "about.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 75])
    (project.refs / "about.png").unlink()
    assert (await qa.ok("qa_page", page="about")).data["status"] == "PASS"


# --------------------------------------------------------------------------- capture

async def test_capture_any_url(qa, project):
    r = await qa.ok("capture", url=project.app.url + "/about/", name="about-shot")
    assert r.data["status"] == 200 and len(r.images) == 1
    saved = project.runtime / "captures" / "about-shot.png"
    assert saved.exists() and png_size(saved)[0] == 1440


async def test_capture_single_element(qa, project):
    r = await qa.ok("capture", url=project.app.url + "/home/", name="hero", clip_selector=".hero")
    assert png_size(project.runtime / "captures" / "hero.png") == (1440, 140)
    assert "not found" in await qa.error("capture", url=project.app.url + "/home/", clip_selector=".nope")


async def test_capture_viewport_and_full_page(qa, project):
    await qa.ok("capture", url=project.app.url + "/home/", name="m", viewport="mobile")
    assert png_size(project.runtime / "captures" / "m.png")[0] == 780
    await qa.ok("capture", url=project.app.url + "/home/", name="fold", full_page=False)
    assert png_size(project.runtime / "captures" / "fold.png") == (1440, 900)
    assert "viewport" in await qa.error("capture", url=project.app.url, viewport="watch")


async def test_capture_waits_for_selector(qa, project):
    project.app.write_page("late", extra="<script>setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<p id=ready>ok</p>'),300)</script>")
    await qa.ok("capture", url=project.app.url + "/late/", wait_for_selector="#ready")
    assert "did not appear" in await qa.error("capture", url=project.app.url + "/home/", wait_for_selector="#never")


async def test_capture_name_cannot_escape_the_runtime_dir(qa, project):
    await qa.ok("capture", url=project.app.url + "/home/", name="../../../escape", return_image=False)
    assert (project.runtime / "captures" / "escape.png").exists()
    assert not (project.root.parent / "escape.png").exists()


# --------------------------------------------------------------------------- compare_images

def _draw(path, h=400, box=None):
    im = np.full((h, 300, 3), 255, np.uint8)
    cv2.rectangle(im, (10, 10), (290, 60), (40, 120, 40), -1)
    if box:
        cv2.rectangle(im, box[:2], box[2:], (0, 0, 0), -1)
    cv2.imwrite(str(path), im)


async def test_compare_two_image_files(qa, project):
    _draw(project.root / "a.png")
    _draw(project.root / "b.png", box=(100, 200, 180, 280))
    same = await qa.ok("compare_images", image_a="a.png", image_b="a.png")
    assert same.data["status"] == "PASS" and same.data["score"] == pytest.approx(1.0)
    diff = await qa.ok("compare_images", image_a="a.png", image_b=str(project.root / "b.png"))
    assert diff.data["status"] == "FAIL" and len(diff.images) == 1
    region = diff.data["regions"][0]
    assert region["x"] <= 100 and region["x"] + region["width"] >= 180


async def test_compare_images_input_errors(qa, project):
    _draw(project.root / "a.png")
    (project.root / "notes.txt").write_text("hello")
    assert "not found" in await qa.error("compare_images", image_a="a.png", image_b="missing.png")
    assert "Not a readable image" in await qa.error("compare_images", image_a="a.png", image_b="notes.txt")


async def test_very_long_pages_are_compared_without_running_out_of_memory(qa, project):
    tall = np.full((32000, 1440, 3), 255, np.uint8)
    cv2.rectangle(tall, (100, 30000), (800, 31000), (0, 0, 0), -1)
    cv2.imwrite(str(project.root / "tall_a.png"), tall)
    cv2.rectangle(tall, (100, 30000), (800, 31000), (255, 255, 255), -1)
    cv2.imwrite(str(project.root / "tall_b.png"), tall)
    r = (await qa.ok("compare_images", image_a="tall_a.png", image_b="tall_b.png")).data
    assert r["downscaled"] >= 2
    top = r["regions"][0]
    assert 29000 < top["y"] < 30500  # coordinates are reported at full resolution


# --------------------------------------------------------------------------- before/after

async def test_refactor_that_changes_nothing_is_proven_identical(qa, project):
    await qa.ok("capture_set", label="before", pages=["home", "about", "home-mobile"])
    # "refactor": different markup, same rendering
    (project.app.root / "about" / "index.html").write_text(
        (project.app.root / "about" / "index.html").read_text().replace("<main>", "<main data-refactored='1'>"))
    await qa.ok("capture_set", label="after", pages=["home", "about", "home-mobile"])
    r = await qa.ok("compare_sets")
    assert r.data["identical"] is True and r.data["compared"] == 3 and r.images == []


async def test_refactor_that_changes_one_page_is_pinpointed(qa, project):
    await qa.ok("capture_set", label="before", pages=["home", "about"])
    project.app.write_page("about", body="<h1 style='font-size:120px'>moved</h1>")
    await qa.ok("capture_set", label="after", pages=["home", "about"])
    r = await qa.ok("compare_sets", before="before", after="after")
    assert r.data["identical"] is False
    assert [c["page"] for c in r.data["changed"]] == ["about"]
    assert len(r.images) == 1


async def test_pages_missing_on_one_side_are_listed(qa, project):
    await qa.ok("capture_set", label="before", pages=["home", "about"])
    await qa.ok("capture_set", label="after", pages=["home", "contact"])
    r = (await qa.ok("compare_sets")).data
    assert r["only_before"] == ["about"] and r["only_after"] == ["contact"]


async def test_compare_sets_accepts_directories(qa, project):
    await qa.ok("capture_set", label="x", pages=["home"])
    d = str(project.runtime / "sets" / "x")
    assert (await qa.ok("compare_sets", before=d, after=d)).data["identical"]


async def test_capture_set_reports_failures_and_bad_input(qa, project):
    shutil.rmtree(project.app.root / "contact")
    r = (await qa.ok("capture_set", label="b", pages=["home", "contact", "ghost"])).data
    assert r["captured"] == 3 or r["warnings"]  # 404s still produce a capture, flagged as a warning
    assert {w["page"] for w in r["warnings"]} == {"contact", "ghost"}
    assert "label" in await qa.error("capture_set", label="../evil", pages=["home"])
    assert "not found" in await qa.error("compare_sets", before="nope", after="b")
    assert "No pages" in await qa.error("capture_set", label="empty")


async def test_ab_compare_between_two_running_servers(qa, project, other_app):
    same = (await qa.ok("ab_compare", base_url_a=project.app.url, base_url_b=other_app.url,
                        pages=["home", "about"])).data
    assert same["identical"] is True
    other_app.set_css(BROKEN_CSS)
    r = await qa.ok("ab_compare", base_url_a=project.app.url, base_url_b=other_app.url, pages=["home", "about"])
    assert sorted(c["page"] for c in r.data["changed"]) == ["about", "home"]
    assert len(r.images) == 2


# --------------------------------------------------------------------------- references

async def test_update_reference_requires_explicit_confirmation(qa, project, golden):
    before = (project.refs / "home.png").read_bytes()
    project.app.write_page("home", color="#f00")
    await qa.ok("qa_page", page="home")
    assert "confirm" in await qa.error("update_reference", page="home")
    assert (project.refs / "home.png").read_bytes() == before


async def test_accepting_a_new_design_updates_the_reference_with_backup(qa, project, golden):
    before = (project.refs / "home.png").read_bytes()
    project.app.write_page("home", color="#f00")
    assert (await qa.ok("qa_page", page="home")).data["status"] == "FAIL"
    await qa.ok("update_reference", page="home", confirm=True)
    assert (await qa.ok("qa_page", page="home")).data["status"] == "PASS"
    backups = list((project.runtime / "reference_backups").glob("home_*.png"))
    assert len(backups) == 1 and backups[0].read_bytes() == before


async def test_update_reference_captures_the_page_when_needed(qa, project):
    r = await qa.ok("update_reference", page="about", confirm=True)
    assert (project.refs / "about.png").exists() and len(r.images) == 1
    assert (await qa.ok("qa_page", page="about")).data["status"] == "PASS"


async def test_error_pages_are_never_saved_as_references(qa, project):
    msg = await qa.error("update_reference", page="does-not-exist", confirm=True)
    assert "HTTP 404" in msg and not (project.refs / "does-not-exist.png").exists()
    assert "plain name" in await qa.error("update_reference", page="../../etc/x", confirm=True)


async def test_sync_figma_downloads_frames_as_references(qa, project, figma, monkeypatch):
    assert "FIGMA_TOKEN" in await qa.error("sync_figma", file_key="FILE1")
    monkeypatch.setenv("FIGMA_TOKEN", "figma-secret")
    r = (await qa.ok("sync_figma", file_key="FILE1")).data
    assert sorted(r["synced"]) == ["about_us", "home"]
    assert (project.refs / "home.png").exists() and (project.refs / "about_us.png").exists()
    api = [s for s in figma if s["path"].startswith("/v1/")]
    cdn = [s for s in figma if s["path"].startswith("/cdn/")]
    assert all(s["token"] == "figma-secret" for s in api)
    assert cdn and all(s["token"] is None for s in cdn)  # the token never goes to the image CDN


async def test_sync_single_figma_frame(qa, project, figma, monkeypatch):
    monkeypatch.setenv("FIGMA_TOKEN", "t")
    assert (await qa.ok("sync_figma", file_key="FILE1", frame="home")).data["synced"] == ["home"]
    msg = await qa.error("sync_figma", file_key="FILE1", frame="pricing")
    assert "not found" in msg and "home" in msg
    assert "not accessible" in await qa.error("sync_figma", file_key="NOPE")
