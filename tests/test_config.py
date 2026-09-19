"""Project configuration as a developer writes it (.qa-screens.json + environment)."""
from __future__ import annotations

import shutil


async def test_environment_overrides_the_config_file(qa, project, monkeypatch, other_app):
    monkeypatch.setenv("QA_SCREENS_BASE_URL", other_app.url)
    monkeypatch.setenv("QA_SCREENS_ROUTE_TEMPLATE", "/x/{name}")
    monkeypatch.setenv("QA_SCREENS_THRESHOLD", "0.5")
    cfg = (await qa.ok("qa_config")).data
    assert (cfg["base_url"], cfg["route_template"], cfg["threshold"]) == (other_app.url, "/x/{name}", 0.5)


async def test_qa_screens_root_points_the_server_at_another_project(qa, project, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / ".qa-screens.json").write_text('{"base_url": "http://other.test"}')
    monkeypatch.setenv("QA_SCREENS_ROOT", str(other))
    cfg = (await qa.ok("qa_config")).data
    assert cfg["root"] == str(other) and cfg["base_url"] == "http://other.test"


async def test_references_can_live_outside_the_project(qa, project, golden, tmp_path):
    shared = tmp_path / "design-refs"
    shutil.copytree(project.refs, shared)
    shutil.rmtree(project.refs)
    project.configure(references_dir=str(shared))
    r = (await qa.ok("run_qa")).data
    assert r["passed"] == 4


async def test_custom_mobile_suffix(qa, project):
    project.configure(mobile_suffix="_m")
    await qa.ok("capture_set", label="s", pages=["home_m", "home"])
    project.refs.mkdir()
    for n in ("home_m", "home"):
        shutil.copy(project.runtime / "sets" / "s" / f"{n}.png", project.refs / f"{n}.png")
    refs = {r["name"]: r for r in (await qa.ok("qa_config")).data["references"]}
    assert refs["home_m"]["viewport"] == "mobile" and refs["home_m"]["url"] == project.app.url + "/home/"
    r = (await qa.ok("qa_page", page="home_m")).data
    assert r["status"] == "PASS" and r["live_size"][0] == 780


async def test_custom_desktop_viewport(qa, project):
    project.configure(desktop_viewport={"width": 1024, "height": 700})
    await qa.ok("capture", url=project.app.url + "/home/", name="narrow", full_page=False, return_image=False)
    import cv2

    img = cv2.imread(str(project.runtime / "captures" / "narrow.png"))
    assert (img.shape[1], img.shape[0]) == (1024, 700)


async def test_runtime_output_location(qa, project, golden):
    project.configure(runtime_dir="build/qa")
    r = (await qa.ok("run_qa", pages=["home"])).data
    assert r["report_path"].startswith(str(project.root / "build" / "qa"))
