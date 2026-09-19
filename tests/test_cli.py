"""The `qa-screens` command as used in CI and by MCP clients."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from qa_screens import __version__

BROKEN_CSS = "body{margin:0;font-family:serif} .hero{height:420px} main{padding:90px}"


def cli(*args, cwd=None, timeout=180):
    return subprocess.run([sys.executable, "-m", "qa_screens", *args], cwd=cwd, env=dict(os.environ),
                          capture_output=True, text=True, timeout=timeout)


def test_version():
    r = cli("--version")
    assert r.returncode == 0 and r.stdout.strip() == __version__


async def test_run_exits_zero_when_everything_passes(project, golden):
    r = cli("run", cwd=project.root)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("PASS") == 4
    assert "4/4 passed, 0 failed, 0 errors" in r.stdout
    assert (project.runtime / "reports" / "latest.json").exists()


async def test_run_exits_one_on_regressions_and_names_them(project, golden):
    project.app.set_css(BROKEN_CSS)
    r = cli("run", "about", "home", cwd=project.root)
    assert r.returncode == 1
    assert "FAIL" in r.stdout and "about" in r.stdout and "home" in r.stdout and "contact" not in r.stdout


async def test_run_exits_one_on_errors(project, golden):
    import shutil

    shutil.rmtree(project.app.root / "contact")
    r = cli("run", cwd=project.root)
    assert r.returncode == 1 and "ERROR" in r.stdout and "HTTP 404" in r.stdout


async def test_run_json_output_is_machine_readable(project, golden):
    r = cli("run", "--json", "--viewport", "mobile", cwd=project.root)
    data = json.loads(r.stdout)
    assert [p["page"] for p in data["pages"]] == ["home-mobile"] and data["passed"] == 1


async def test_run_tolerance_flags(project, golden):
    project.app.set_css(BROKEN_CSS)
    assert cli("run", "about", cwd=project.root).returncode == 1
    r = cli("run", "about", "--threshold", "0", "--max-changed-ratio", "1", cwd=project.root)
    assert r.returncode == 0, r.stdout


async def test_run_against_another_root_and_base_url(project, golden, other_app, tmp_path):
    r = cli("run", "--root", str(project.root), "--base-url", other_app.url, "home", cwd=tmp_path)
    assert r.returncode == 0 and other_app.seen("/home/")


def test_run_without_references_is_a_usage_error(project):
    r = cli("run", cwd=project.root)
    assert r.returncode == 2 and "error: There are no reference screenshots" in r.stderr
    assert "update_reference" in r.stderr  # tells you how to get some


async def test_broken_config_warns_but_still_runs(project, golden, monkeypatch):
    (project.root / ".qa-screens.json").write_text("{oops")
    monkeypatch.setenv("QA_SCREENS_BASE_URL", project.app.url)
    r = cli("run", "home", cwd=project.root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "warning: .qa-screens.json ignored, using defaults" in r.stderr


def test_reports_command_shows_and_flushes(project, github, isolated_state):
    pending = isolated_state / "reports" / "pending"
    r = cli("reports", cwd=project.root)
    assert r.returncode == 0 and "mode=on pending=0" in r.stdout
    # A crash from an earlier run left a report behind.
    crash = "from qa_screens import reporting\nreporting.install_crash_handlers()\nraise LookupError('left behind')\n"
    env = {**os.environ, "QA_SCREENS_ERROR_REPORTING": "local"}
    subprocess.run([sys.executable, "-c", crash], env=env, capture_output=True)
    assert len(list(pending.glob("*.json"))) == 1
    r = cli("reports", "--flush", cwd=project.root)
    assert "https://github.com/astuanax/qa-screens/issues/1" in r.stdout and "pending=0" in r.stdout
    assert "LookupError" in github.issues[0]["title"]


async def test_server_speaks_mcp_over_stdio(project):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "qa_screens", "serve"],
                                   cwd=str(project.root), env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = {t.name: t for t in (await s.list_tools()).tools}
        expected = {"qa_config", "run_qa", "qa_page", "capture", "compare_images", "capture_set", "compare_sets",
                    "ab_compare", "update_reference", "sync_figma", "auth_profiles", "auth_update_profile",
                    "auth_browser_login", "auth_oauth_login", "auth_import_storage_state", "auth_check",
                    "auth_delete_profile", "error_reports"}
        assert expected == set(tools)
        assert all(len(t.description or "") > 40 for t in tools.values()), "every tool needs a real description"
        res = await s.call_tool("qa_config", {})
        assert json.loads(res.content[0].text)["base_url"] == project.app.url
        res = await s.call_tool("capture", {"url": project.app.url + "/home/"})
        assert not res.is_error and res.content[1].type == "image"
