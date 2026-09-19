"""Automatic error reporting, observed from GitHub's side.

Bugs are simulated by fault injection (a function inside the server raises
unexpectedly); crashes by real processes that die. Everything else is observed
through the tools and the fake GitHub API.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from conftest import closed_port_url

SECRET = "tok-SUPER-secret-42"


def wait_for(predicate, timeout=15.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def inject_bug(monkeypatch):
    """Make the page-comparison step blow up like a real bug would."""
    import qa_screens.runner as runner

    def _inject(exc: BaseException):
        def boom(*a, **kw):
            raise type(exc)(*exc.args)  # a fresh exception each call, like a real bug
        monkeypatch.setattr(runner, "qa_page", boom)
    return _inject


def pending(state):
    d = state / "reports" / "pending"
    return sorted(d.glob("*.json")) if d.exists() else []


# --------------------------------------------------------------------------- what gets reported

async def test_unexpected_error_becomes_a_github_issue(qa, project, golden, github, inject_bug):
    inject_bug(ZeroDivisionError("division by zero in layout scorer"))
    msg = await qa.error("qa_page", page="home")
    assert "ZeroDivisionError" in msg and "error report was filed" in msg
    issue = wait_for(lambda: github.issues and github.issues[0], what="issue")
    assert issue["repo"] == "astuanax/qa-screens"
    assert issue["title"].startswith("[auto-report] ZeroDivisionError: division by zero")
    assert set(issue["labels"]) == {"auto-report", "bug"}
    body = issue["body"]
    assert "qa_page" in body and "Traceback" in body and "qa-screens-fingerprint:" in body
    assert "qa_screens" in body and "python" in body
    assert all(r["auth"] == "Bearer gh-test-token" for r in github.requests)


async def test_reports_contain_no_secrets(qa, project, golden, github, inject_bug):
    inject_bug(RuntimeError(f"upstream said: Authorization: Bearer {SECRET} password={SECRET} "
                            f"at https://user:hunter2@idp.example/cb?code=AUTHCODE&state=xyz "
                            f"in {os.path.expanduser('~')}/projects/client-x"))
    await qa.error("qa_page", page="home", url=f"https://app.example/p?session={SECRET}")
    issue = wait_for(lambda: github.issues and github.issues[0], what="issue")
    text = issue["title"] + issue["body"]
    for leaked in (SECRET, "hunter2", "AUTHCODE", os.path.expanduser("~") + "/"):
        assert leaked not in text, f"{leaked!r} leaked into the issue"
    assert "[REDACTED" in text


async def test_user_errors_are_never_reported(qa, project, golden, github, monkeypatch):
    import shutil

    await qa.error("qa_page", page="does-not-exist")                       # missing reference
    await qa.error("run_qa", viewport="tablet")                             # bad argument
    await qa.error("capture", url=closed_port_url() + "/")                    # site down
    await qa.error("auth_update_profile", name="x", oauth={                 # bad credentials
        "grant_type": "client_credentials", "token_url": project.app.url + "/token",
        "client_id": "c", "client_secret": "wrong"})
    shutil.rmtree(project.app.root / "about")
    await qa.ok("run_qa")                                                   # a 404 page
    time.sleep(1)
    assert github.issues == [] and github.requests == []


async def test_bugs_inside_a_batch_are_reported_without_stopping_the_run(qa, project, golden, github, monkeypatch):
    import qa_screens.runner as runner

    real = runner.compute_diff

    def flaky(ref, live, *a, **kw):
        if "about" in ref:
            raise KeyError("bbox")
        return real(ref, live, *a, **kw)

    monkeypatch.setattr(runner, "compute_diff", flaky)
    r = (await qa.ok("run_qa", viewport="desktop")).data
    rows = {p["page"]: p for p in r["pages"]}
    assert rows["about"]["status"] == "ERROR" and "reported automatically" in rows["about"]["error"]
    assert rows["home"]["status"] == rows["contact"]["status"] == "PASS"
    issue = wait_for(lambda: github.issues and github.issues[0], what="issue")
    assert "KeyError" in issue["title"] and "about" in issue["body"]


# --------------------------------------------------------------------------- noise control

async def test_the_same_bug_is_filed_once(qa, project, golden, github, inject_bug):
    inject_bug(ValueError("same bug"))
    for _ in range(3):
        await qa.error("qa_page", page="home")
        time.sleep(0.3)
    wait_for(lambda: github.issues, what="issue")
    time.sleep(1)
    assert len(github.issues) == 1 and github.comments == []


async def test_a_bug_already_open_on_github_gets_a_comment_instead(qa, project, golden, github, inject_bug,
                                                                   monkeypatch, tmp_path):
    inject_bug(ValueError("known bug"))
    await qa.error("qa_page", page="home")
    wait_for(lambda: github.issues, what="first issue")
    # Another developer's machine (fresh state) hits the same bug.
    monkeypatch.setenv("QA_SCREENS_STATE_DIR", str(tmp_path / "colleague"))
    await qa.error("qa_page", page="home")
    comment = wait_for(lambda: github.comments and github.comments[0], what="comment")
    assert comment["number"] == 1 and "Seen again" in comment["body"]
    assert len(github.issues) == 1


async def test_at_most_ten_issues_per_day(qa, project, golden, github, inject_bug, monkeypatch):
    monkeypatch.setenv("QA_SCREENS_ERROR_REPORTING", "local")  # collect first, post in one go
    for i in range(12):
        inject_bug(type(f"DistinctBug{i}", (Exception,), {})(f"bug {i}"))
        await qa.error("qa_page", page="home")
    monkeypatch.setenv("QA_SCREENS_ERROR_REPORTING", "on")
    status = (await qa.ok("error_reports", flush=True)).data
    assert len(github.issues) == 10 and len(status["posted_now"]) == 10
    assert len(status["pending"]) == 2  # kept for tomorrow, not dropped


async def test_issue_repo_is_configurable(qa, project, golden, github, inject_bug, monkeypatch):
    monkeypatch.setenv("QA_SCREENS_ISSUE_REPO", "acme/qa-screens-fork")
    inject_bug(ValueError("fork bug"))
    await qa.error("qa_page", page="home")
    assert wait_for(lambda: github.issues, what="issue")[0]["repo"] == "acme/qa-screens-fork"


# --------------------------------------------------------------------------- delivery guarantees

async def test_report_survives_github_outage_and_is_sent_later(qa, project, golden, github, inject_bug, isolated_state):
    github.fail_with = 503
    inject_bug(ValueError("while github is down"))
    await qa.error("qa_page", page="home")
    wait_for(lambda: github.requests, what="attempt")
    time.sleep(0.5)
    status = (await qa.ok("error_reports")).data
    assert len(status["pending"]) == 1 and github.issues == []

    github.fail_with = None
    status = (await qa.ok("error_reports", flush=True)).data
    assert len(status["posted_now"]) == 1 and status["pending"] == []
    assert len(github.issues) == 1


async def test_without_a_github_token_reports_wait(qa, project, golden, github, inject_bug, monkeypatch):
    monkeypatch.delenv("QA_SCREENS_GITHUB_TOKEN")
    inject_bug(ValueError("no token"))
    await qa.error("qa_page", page="home")
    time.sleep(0.5)
    status = (await qa.ok("error_reports", flush=True)).data
    assert status["github_token_available"] is False and len(status["pending"]) == 1
    assert github.requests == []
    monkeypatch.setenv("GITHUB_TOKEN", "fallback-token")  # the generic variable works too
    assert len((await qa.ok("error_reports", flush=True)).data["posted_now"]) == 1
    assert github.requests[-1]["auth"] == "Bearer fallback-token"


@pytest.mark.parametrize("mode,written", [("local", 1), ("off", 0)])
async def test_reporting_modes(qa, project, golden, github, inject_bug, monkeypatch, isolated_state, mode, written):
    monkeypatch.setenv("QA_SCREENS_ERROR_REPORTING", mode)
    inject_bug(ValueError("mode test"))
    msg = await qa.error("qa_page", page="home")
    assert ("report was filed" in msg) == bool(written)
    time.sleep(0.5)
    assert len(pending(isolated_state)) == written
    assert github.requests == []
    assert (await qa.ok("error_reports")).data["mode"] == mode


# --------------------------------------------------------------------------- crashes

def _env(**extra):
    return {**os.environ, **extra}


def _start_server_and_list_tools(cwd):
    """Start the real server (as an MCP client would) and do one request."""
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    p = subprocess.Popen([sys.executable, "-m", "qa_screens"], cwd=cwd, env=_env(), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    p.stdin.write(json.dumps(init) + "\n")
    p.stdin.flush()
    first = p.stdout.readline()
    return p, first


def test_crash_while_offline_is_posted_on_next_start(project, github, isolated_state):
    github.fail_with = 503
    crash = ("import qa_screens.runner as r\n"
             "def boom(*a, **k): raise MemoryError('capture buffer exhausted')\n"
             "r.qa_batch = boom\n"
             "from qa_screens.cli import main\n"
             "main(['run'])\n")
    (project.refs).mkdir()
    (project.refs / "home.png").write_bytes(b"")
    res = subprocess.run([sys.executable, "-c", crash], cwd=project.root, env=_env(), capture_output=True, text=True)
    assert res.returncode != 0 and "MemoryError" in res.stderr
    assert github.issues == [] and len(pending(isolated_state)) == 1

    github.fail_with = None
    server, first = _start_server_and_list_tools(project.root)
    try:
        assert '"result"' in first  # the server answers while the scan runs in the background
        issue = wait_for(lambda: github.issues and github.issues[0], what="crash issue")
        assert "MemoryError" in issue["title"]
    finally:
        server.kill()


def test_hard_crash_is_logged_to_a_file_and_posted_on_next_start(project, github):
    # The real server segfaults one second after starting (e.g. a native library bug).
    segv = ("import faulthandler, threading\n"
            "from qa_screens.cli import main\n"
            "threading.Timer(1.0, faulthandler._sigsegv).start()\n"
            "main(['serve'])\n")
    p = subprocess.Popen([sys.executable, "-c", segv], cwd=project.root, env=_env(), stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert p.wait(timeout=30) != 0

    server, _ = _start_server_and_list_tools(project.root)
    try:
        issue = wait_for(lambda: github.issues and github.issues[0], what="segfault issue")
        assert "Fatal Python error" in issue["title"] or "Segmentation fault" in issue["body"]
        assert "crash" in issue["body"]
    finally:
        server.kill()


def test_clean_shutdown_is_not_a_crash(project, github):
    server, first = _start_server_and_list_tools(project.root)
    assert '"result"' in first
    server.stdin.close()  # how MCP clients end a session
    assert server.wait(timeout=20) == 0
    server, _ = _start_server_and_list_tools(project.root)
    time.sleep(2)
    server.kill()
    assert github.issues == []
