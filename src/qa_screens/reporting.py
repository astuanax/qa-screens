"""Automatic error reporting to GitHub issues.

Every significant error (an unexpected exception, never a QAUserError) is first
written to `<state>/reports/pending/*.json`, then posted as a GitHub issue in a
background thread. Only when posting succeeds is the file moved to `reported/`.
That makes posting crash-safe: whatever is still pending — including crash files
written by the process-level hooks or faulthandler — is retried on next start-up.

Configuration (environment):
  QA_SCREENS_ERROR_REPORTING  on (default) | local (write files, never post) | off
  QA_SCREENS_ISSUE_REPO       owner/repo to post to (default: astuanax/qa-screens)
  QA_SCREENS_GITHUB_TOKEN     token with issues:write; falls back to GITHUB_TOKEN,
                              then `gh auth token`. No token -> stays pending.

Reports are scrubbed: bearer tokens, JWTs, cookies, passwords, secrets in
key=value pairs and URL query strings / credentials are redacted before posting.
"""
from __future__ import annotations

import atexit
import faulthandler
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import __version__
from .config import state_dir
from .errors import QAUserError

logger = logging.getLogger("qa_screens.reporting")

DEFAULT_REPO = "astuanax/qa-screens"
GITHUB_API = "https://api.github.com"
_transport: httpx.BaseTransport | None = None  # injectable for tests
DEDUPE_WINDOW_S = 24 * 3600
MAX_POSTS_PER_DAY = 10

_SECRET_KEY_RE = r"(?:pass(?:word|wd)?|secret|token|api[_-]?key|auth(?:orization)?|cookie|session|client_secret|refresh_token|access_token|id_token|code_verifier)"
_SCRUBBERS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), "[REDACTED_JWT]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{30,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)([\"']?" + _SECRET_KEY_RE + r"[\"']?\s*[:=]\s*)([\"']?)[^\s\"',;&}]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(https?://[^\s?#\"']+)\?[^\s#\"']+"), r"\1?[REDACTED_QUERY]"),
    (re.compile(re.escape(os.path.expanduser("~"))), "~"),
]


def scrub(text: str) -> str:
    for pattern, repl in _SCRUBBERS:
        text = pattern.sub(repl, text)
    return text


def scrub_args(args: dict | None) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if re.search(_SECRET_KEY_RE, k, re.I) or k in ("cookies", "headers", "local_storage", "session_storage"):
            out[k] = "[REDACTED]"
        else:
            out[k] = scrub(repr(v))[:300]
    return out


def _dirs() -> tuple[Path, Path, Path]:
    base = state_dir() / "reports"
    pending, reported, crash = base / "pending", base / "reported", base / "faulthandler"
    for d in (pending, reported, crash):
        d.mkdir(parents=True, exist_ok=True)
    return pending, reported, crash


def mode() -> str:
    return os.environ.get("QA_SCREENS_ERROR_REPORTING", "on").strip().lower()


def is_significant(exc: BaseException) -> bool:
    return not isinstance(exc, (QAUserError, KeyboardInterrupt, SystemExit, GeneratorExit))


def fingerprint(exc_type: str, tb_text: str) -> str:
    """Stable across runs: exception type + our own frames (file:function), no line numbers/values."""
    frames = re.findall(r'File "([^"]+)", line \d+, in (\S+)', tb_text)
    ours = [f"{Path(f).name}:{fn}" for f, fn in frames if "qa_screens" in f] or [
        f"{Path(f).name}:{fn}" for f, fn in frames[-3:]
    ]
    return hashlib.sha1(f"{exc_type}|{'>'.join(ours)}".encode()).hexdigest()[:12]


def _environment() -> dict:
    env = {"qa_screens": __version__, "python": platform.python_version(), "platform": platform.platform()}
    try:
        from importlib.metadata import version

        env["playwright"] = version("playwright")
        env["mcp"] = version("mcp")
    except Exception:
        pass
    return env


def build_report(kind: str, exc_type: str, message: str, tb_text: str, context: dict | None = None) -> dict:
    return {
        "kind": kind,  # "error" | "crash" | "fatal"
        "exc_type": exc_type,
        "message": scrub(message)[:2000],
        "traceback": scrub(tb_text)[-12000:],
        "fingerprint": fingerprint(exc_type, tb_text),
        "context": context or {},
        "environment": _environment(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }


def write_pending(report: dict) -> Path:
    pending, _, _ = _dirs()
    path = pending / f"{report['kind']}-{int(time.time() * 1000)}-{report['pid']}-{report['fingerprint']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def record_exception(exc: BaseException, kind: str = "error", context: dict | None = None, post: bool = True) -> Path | None:
    """Persist a significant exception and (optionally) post it in the background."""
    if mode() == "off" or not is_significant(exc):
        return None
    try:
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        path = write_pending(build_report(kind, type(exc).__name__, str(exc), tb_text, context))
        if post:
            threading.Thread(target=flush_pending, name="qa-screens-report", daemon=True).start()
        return path
    except Exception:  # the reporter must never take the server down
        logger.exception("Failed to record error report")
        return None


# --------------------------------------------------------------------------- GitHub

def _token() -> str | None:
    tok = os.environ.get("QA_SCREENS_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    if shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return None


def _ledger_path() -> Path:
    return state_dir() / "reports" / "ledger.json"


def _load_ledger() -> dict:
    try:
        return json.loads(_ledger_path().read_text(encoding="utf-8"))
    except Exception:
        return {"fingerprints": {}, "posts": []}


def _save_ledger(ledger: dict) -> None:
    ledger["posts"] = [t for t in ledger.get("posts", []) if t > time.time() - 86400]
    _ledger_path().write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def _issue_body(r: dict) -> str:
    ctx = "\n".join(f"- **{k}**: `{v}`" for k, v in r.get("context", {}).items()) or "_none_"
    env = "\n".join(f"- {k}: `{v}`" for k, v in r["environment"].items())
    return (
        f"Automatically reported by qa-screens ({r['kind']}).\n\n"
        f"**{r['exc_type']}**: {r['message']}\n\n"
        f"### Context\n{ctx}\n\n### Environment\n{env}\n\n"
        f"### Traceback\n```\n{r['traceback']}\n```\n\n"
        f"<!-- qa-screens-fingerprint:{r['fingerprint']} -->\n"
        f"fingerprint: `{r['fingerprint']}` · first seen {r['timestamp']}"
    )


def _post(client: httpx.Client, repo: str, r: dict) -> str:
    """Create an issue, or comment on the open one with the same fingerprint. Returns its URL."""
    fp = r["fingerprint"]
    search = client.get(
        "/search/issues", params={"q": f'repo:{repo} is:issue is:open "qa-screens-fingerprint:{fp}" in:body'}
    )
    if search.status_code == 200 and search.json().get("total_count"):
        issue = search.json()["items"][0]
        resp = client.post(
            f"/repos/{repo}/issues/{issue['number']}/comments",
            json={"body": f"Seen again ({r['kind']}) at {r['timestamp']} on qa-screens {r['environment']['qa_screens']}.\n\n```\n{r['traceback'][-3000:]}\n```"},
        )
        resp.raise_for_status()
        return issue["html_url"]
    title = f"[auto-report] {r['exc_type']}: {r['message'].splitlines()[0][:90] if r['message'] else r['kind']}"
    resp = client.post(
        f"/repos/{repo}/issues", json={"title": title, "body": _issue_body(r), "labels": ["auto-report", "bug"]}
    )
    resp.raise_for_status()
    return resp.json()["html_url"]


_flush_lock = threading.Lock()


def flush_pending() -> list[str]:
    """Post every pending report. Safe to call concurrently and repeatedly."""
    if mode() != "on":
        return []
    with _flush_lock:
        pending, reported, _ = _dirs()
        files = sorted(pending.glob("*.json"))
        if not files:
            return []
        token = _token()
        if not token:
            logger.warning("%d error report(s) pending: no GitHub token (set QA_SCREENS_GITHUB_TOKEN)", len(files))
            return []
        repo = os.environ.get("QA_SCREENS_ISSUE_REPO", DEFAULT_REPO)
        ledger = _load_ledger()
        urls: list[str] = []
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        with httpx.Client(base_url=GITHUB_API, headers=headers, timeout=20, transport=_transport) as client:
            for f in files:
                try:
                    r = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    f.rename(reported / (f.name + ".unreadable"))
                    continue
                fp, now = r.get("fingerprint", "?"), time.time()
                last = ledger["fingerprints"].get(fp, {}).get("posted_at", 0)
                if now - last < DEDUPE_WINDOW_S:
                    ledger["fingerprints"][fp]["suppressed"] = ledger["fingerprints"][fp].get("suppressed", 0) + 1
                    f.rename(reported / f.name)
                    continue
                if len([t for t in ledger.get("posts", []) if t > now - 86400]) >= MAX_POSTS_PER_DAY:
                    logger.warning("Daily error-report limit reached; keeping %d report(s) pending", len(files))
                    break
                try:
                    url = _post(client, repo, r)
                except Exception as e:
                    logger.warning("Posting error report failed (will retry on next start): %s", scrub(str(e)))
                    break
                ledger["fingerprints"][fp] = {"posted_at": now, "url": url}
                ledger.setdefault("posts", []).append(now)
                r["issue_url"] = url
                (reported / f.name).write_text(json.dumps(r, indent=2), encoding="utf-8")
                f.unlink()
                urls.append(url)
                logger.info("Reported %s to %s", fp, url)
        _save_ledger(ledger)
        return urls


# --------------------------------------------------------------------------- crash handling

_fault_file = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def collect_crashes() -> int:
    """Turn faulthandler dumps left by dead processes into pending reports."""
    _, _, crash = _dirs()
    n = 0
    for f in crash.glob("faulthandler-*.log"):
        try:
            pid = int(f.stem.split("-")[1])
        except (IndexError, ValueError):
            continue
        if pid == os.getpid() or _pid_alive(pid):
            continue
        text = f.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            first = text.splitlines()[0]
            report = build_report("crash", "FatalError", first, text, {"source": "faulthandler", "pid": pid})
            report["pid"] = pid
            write_pending(report)
            n += 1
        f.unlink(missing_ok=True)
    return n


def install_crash_handlers() -> None:
    """Log uncaught exceptions and hard crashes (segfaults, aborts) to crash files."""
    global _fault_file
    if mode() == "off":
        return
    _, _, crash = _dirs()
    fault_path = crash / f"faulthandler-{os.getpid()}.log"
    _fault_file = open(fault_path, "w", encoding="utf-8")  # noqa: SIM115 - must stay open for the process lifetime
    faulthandler.enable(file=_fault_file, all_threads=True)

    def _cleanup():
        # A clean exit leaves an empty file; remove it so it isn't mistaken for a crash.
        try:
            faulthandler.disable()
            _fault_file.close()
            if fault_path.stat().st_size == 0:
                fault_path.unlink()
        except Exception:
            pass

    atexit.register(_cleanup)

    prev_hook = sys.excepthook

    def _excepthook(exc_type, exc, tb):
        if exc is not None and is_significant(exc):
            record_exception(exc.with_traceback(tb), kind="fatal", context={"source": "sys.excepthook"}, post=False)
        prev_hook(exc_type, exc, tb)

    sys.excepthook = _excepthook

    def _thread_hook(args):
        if args.exc_value is not None and is_significant(args.exc_value):
            record_exception(args.exc_value, kind="fatal", context={"source": "thread", "thread": getattr(args.thread, "name", "?")}, post=False)

    threading.excepthook = _thread_hook


def asyncio_exception_handler(loop, context: dict) -> None:
    exc = context.get("exception")
    if exc is not None and is_significant(exc):
        record_exception(exc, kind="error", context={"source": "asyncio", "message": str(context.get("message", ""))})
    loop.default_exception_handler(context)


def startup_scan(background: bool = True) -> None:
    """On start-up: convert crash dumps from previous runs into reports and post all pending ones."""
    if mode() == "off":
        return

    def _run():
        try:
            n = collect_crashes()
            if n:
                logger.info("Found %d crash dump(s) from a previous run", n)
            flush_pending()
        except Exception:
            logger.exception("Start-up crash scan failed")

    if background:
        threading.Thread(target=_run, name="qa-screens-startup-scan", daemon=True).start()
    else:
        _run()
