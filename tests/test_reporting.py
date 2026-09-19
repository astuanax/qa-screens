import json
import os
import subprocess
import sys

import httpx

from qa_screens import reporting
from qa_screens.errors import QAUserError


def _boom():
    raise RuntimeError("kaboom token=abc123secret Authorization: Bearer eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sig")


def test_scrub_redacts_secrets():
    s = reporting.scrub(
        "Bearer abcdefghijklmnop password=hunter2 https://u:p@host/x?code=123&state=9 ghp_" + "a" * 36
    )
    for secret in ("abcdefghijklmnop", "hunter2", "u:p@", "code=123", "ghp_aaaa"):
        assert secret not in s
    assert reporting.scrub_args({"client_secret": "x", "url": "https://h/p?t=1"}) == {
        "client_secret": "[REDACTED]", "url": "'https://h/p?[REDACTED_QUERY]'"}


def test_user_errors_are_not_reported(isolated_state):
    assert reporting.record_exception(QAUserError("missing reference")) is None


def test_significant_error_written_scrubbed_and_stable(isolated_state):
    paths = []
    for _ in range(2):
        try:
            _boom()
        except RuntimeError as e:
            paths.append(reporting.record_exception(e, context={"tool": "t"}))
    reports = [json.loads(p.read_text()) for p in paths]
    assert reports[0]["fingerprint"] == reports[1]["fingerprint"]
    text = json.dumps(reports[0])
    assert "abc123secret" not in text and "eyJhbGci" not in text


def test_flush_posts_issue_then_dedupes(isolated_state, monkeypatch):
    monkeypatch.setenv("QA_SCREENS_ERROR_REPORTING", "on")
    monkeypatch.setenv("QA_SCREENS_GITHUB_TOKEN", "test-token")
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path == "/search/issues":
            return httpx.Response(200, json={"total_count": 0, "items": []})
        body = json.loads(request.content)
        assert "qa-screens-fingerprint:" in body["body"]
        return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/1"})

    monkeypatch.setattr(reporting, "_transport", httpx.MockTransport(handler))
    for _ in range(2):
        try:
            _boom()
        except RuntimeError as e:
            reporting.record_exception(e, post=False)
    assert reporting.flush_pending() == ["https://github.com/o/r/issues/1"]
    assert [c for c in calls if c[0] == "POST"] == [("POST", "/repos/astuanax/qa-screens/issues")]
    assert not list((isolated_state / "reports" / "pending").glob("*.json"))


def test_post_failure_keeps_report_pending(isolated_state, monkeypatch):
    monkeypatch.setenv("QA_SCREENS_ERROR_REPORTING", "on")
    monkeypatch.setenv("QA_SCREENS_GITHUB_TOKEN", "t")
    monkeypatch.setattr(reporting, "_transport", httpx.MockTransport(lambda r: httpx.Response(503)))
    try:
        _boom()
    except RuntimeError as e:
        reporting.record_exception(e, post=False)
    assert reporting.flush_pending() == []
    assert len(list((isolated_state / "reports" / "pending").glob("*.json"))) == 1


def test_uncaught_crash_is_logged_and_picked_up_on_next_start(isolated_state):
    env = {**os.environ, "QA_SCREENS_STATE_DIR": str(isolated_state), "QA_SCREENS_ERROR_REPORTING": "local"}
    code = "from qa_screens import reporting; reporting.install_crash_handlers(); raise ValueError('fatal boom')"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode != 0
    pending = list((isolated_state / "reports" / "pending").glob("fatal-*.json"))
    assert len(pending) == 1 and json.loads(pending[0].read_text())["exc_type"] == "ValueError"


def test_hard_crash_dump_becomes_report(isolated_state):
    env = {**os.environ, "QA_SCREENS_STATE_DIR": str(isolated_state), "QA_SCREENS_ERROR_REPORTING": "local"}
    code = "import faulthandler; from qa_screens import reporting; reporting.install_crash_handlers(); faulthandler._sigsegv()"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
    assert r.returncode != 0
    assert reporting.collect_crashes() == 1
    crash = next((isolated_state / "reports" / "pending").glob("crash-*.json"))
    assert "Segmentation fault" in json.loads(crash.read_text())["traceback"]
    assert not list((isolated_state / "reports" / "faulthandler").glob("*.log"))


def test_clean_exit_leaves_no_crash_dump(isolated_state):
    env = {**os.environ, "QA_SCREENS_STATE_DIR": str(isolated_state), "QA_SCREENS_ERROR_REPORTING": "local"}
    subprocess.run([sys.executable, "-c", "from qa_screens import reporting; reporting.install_crash_handlers()"], env=env, check=True)
    assert not list((isolated_state / "reports" / "faulthandler").glob("*.log"))
