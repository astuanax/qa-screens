"""The first experience: a new user, no or broken config, nothing set up yet.

Rule: configuration problems never make a tool unusable. Bad settings fall back to
defaults and come back as `config_warnings` the agent can act on.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from conftest import closed_port_url


def write_config(project, text: str):
    (project.root / ".qa-screens.json").write_text(text)


# --------------------------------------------------------------------------- config can't break anything

async def test_broken_json_falls_back_to_defaults(qa, project, golden, monkeypatch):
    write_config(project, '{"base_url": "http://x", oops}')
    monkeypatch.setenv("QA_SCREENS_BASE_URL", project.app.url)
    cfg = (await qa.ok("qa_config")).data
    assert any("ignored, using defaults" in w for w in cfg["config_warnings"])
    r = (await qa.ok("run_qa")).data
    assert r["passed"] == 4 and r["config_warnings"]


async def test_typo_in_a_key_is_ignored_with_a_suggestion(qa, project, golden):
    write_config(project, json.dumps({"base_url": project.app.url, "treshold": 0.5}))
    r = (await qa.ok("run_qa", pages=["home"])).data
    assert r["pages"][0]["threshold"] == 0.9
    assert any("'treshold'" in w and "did you mean 'threshold'" in w for w in r["config_warnings"])


async def test_bad_values_keep_defaults_and_the_rest_still_applies(qa, project, golden):
    write_config(project, json.dumps({
        "base_url": project.app.url,          # good: must still apply
        "threshold": "high",
        "desktop_viewport": "big",
        "wait_until": "idle",
        "route_template": "/{page}/",
        "mask_selectors": ".ad",
        "concurrency": 0,
        "mobile_device_scale_factor": 2,       # good
    }))
    r = await qa.ok("run_qa")
    assert r.data["passed"] == 4
    warnings = " | ".join(r.data["config_warnings"])
    for key in ("threshold", "desktop_viewport", "wait_until", "route_template", "mask_selectors", "concurrency"):
        assert f"'{key}'" in warnings
    assert "mobile_device_scale_factor" not in warnings


async def test_numbers_written_as_strings_are_accepted(qa, project, golden):
    write_config(project, json.dumps({"base_url": project.app.url, "threshold": "0.95", "concurrency": "2"}))
    r = (await qa.ok("run_qa", pages=["home"])).data
    assert r["pages"][0]["threshold"] == 0.95 and "config_warnings" not in r


async def test_config_that_is_not_an_object(qa, project, monkeypatch):
    write_config(project, '["base_url"]')
    cfg = (await qa.ok("qa_config")).data
    assert any("JSON object" in w for w in cfg["config_warnings"])


async def test_bad_environment_values_are_warnings_too(qa, project, golden, monkeypatch):
    monkeypatch.setenv("QA_SCREENS_THRESHOLD", "strict")
    r = (await qa.ok("run_qa", pages=["home"])).data
    assert r["pages"][0]["status"] == "PASS"
    assert any("QA_SCREENS_THRESHOLD" in w for w in r["config_warnings"])


async def test_errors_explain_the_config_problem_behind_them(qa, project, golden):
    # The references live in screenshots/, but a typo means the setting never applied.
    shutil.move(project.refs, project.root / "design")
    write_config(project, json.dumps({"base_url": project.app.url, "reference_dir": "design"}))
    msg = await qa.error("run_qa")
    assert "did you mean 'references_dir'" in msg
    assert "update_reference" in msg and "sync_figma" in msg  # and how to get references at all


async def test_read_only_project_dir_still_works(qa, project, golden):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    shutil.rmtree(project.root / ".qa-screens")  # a checkout where nothing has run yet
    project.root.chmod(0o555)
    try:
        r = (await qa.ok("run_qa", pages=["home"])).data
        assert r["pages"][0]["status"] == "PASS"
        assert any("not writable" in w for w in r["config_warnings"])
        assert not str(r["report_path"]).startswith(str(project.root))
    finally:
        project.root.chmod(0o755)


# --------------------------------------------------------------------------- nothing set up yet

async def test_zero_config_first_baseline(qa, project, monkeypatch):
    """No config file, no references: the agent can create a baseline and start checking."""
    (project.root / ".qa-screens.json").unlink()
    monkeypatch.setenv("QA_SCREENS_BASE_URL", project.app.url)
    msg = await qa.error("run_qa")
    assert "update_reference" in msg
    for page in ("home", "home-mobile"):
        made = await qa.ok("update_reference", page=page, confirm=True)
        assert len(made.images) == 1
    r = (await qa.ok("run_qa")).data
    assert (r["passed"], r["total"]) == (2, 2)


async def test_site_not_running_says_what_to_do(qa, project, golden):
    down = closed_port_url()
    r = (await qa.ok("run_qa", base_url=down, pages=["home"])).data
    err = r["pages"][0]["error"]
    assert f"Nothing is running at {down}" in err and "base_url" in err
    assert "Nothing is running" in await qa.error("capture", url=down + "/")


async def test_app_that_never_goes_network_idle_is_still_captured(qa, project):
    project.app.write_page("live", extra="<script>setInterval(()=>fetch('/style.css?'+Date.now()),100)</script>")
    project.configure(navigation_timeout_ms=2000)
    r = await qa.ok("capture", url=project.app.url + "/live/", name="live")
    assert r.data["status"] == 200 and "never went idle" in r.data["warning"]
    assert (project.runtime / "captures" / "live.png").exists()


async def test_chromium_download_failure_is_actionable(qa, project, tmp_path, monkeypatch, isolated_state):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))
    monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", closed_port_url())
    msg = await qa.error("capture", url=project.app.url + "/home/")
    assert "Could not download Chromium" in msg and "playwright install chromium" in msg
    assert not list(isolated_state.glob("reports/pending/*.json"))  # the user's network, not our bug


# --------------------------------------------------------------------------- the real first run

@pytest.mark.first_run
async def test_first_run_from_nothing(app, tmp_path):
    """Fresh machine: no Chromium, no config, server started the way `claude mcp add` does.

    Opt-in (downloads Chromium): pytest -m first_run
    """
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    project = tmp_path / "new-project"
    project.mkdir()
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "fresh-browsers"),
           "QA_SCREENS_BASE_URL": app.url}
    params = StdioServerParameters(command=sys.executable, args=["-m", "qa_screens"], cwd=str(project), env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w, read_timeout_seconds=600) as s:
        await s.initialize()
        cfg = json.loads((await s.call_tool("qa_config", {})).content[0].text)
        assert "config_warnings" not in cfg
        res = await s.call_tool("run_qa", {})
        assert res.is_error and "update_reference" in res.content[0].text
        res = await s.call_tool("update_reference", {"page": "home", "confirm": True})
        assert not res.is_error, res.content[0].text
        res = await s.call_tool("run_qa", {})
        assert not res.is_error and json.loads(res.content[0].text)["passed"] == 1
    assert list((tmp_path / "fresh-browsers").iterdir()), "Chromium was installed on demand"
