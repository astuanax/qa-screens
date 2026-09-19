"""Test doubles for the outside world a qa-screens user deals with.

- `app`: a small web app (static pages, login form + session cookie, protected
  page, basic auth, an OAuth provider with PKCE) that records what the browser
  actually sent, so tests assert on observable effects.
- `github`: a fake GitHub REST API that records issues and comments.
- `figma`: a fake Figma API.
- `qa`: an in-process MCP client connected to the qa-screens server, the same
  interface an AI agent uses.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width, initial-scale=1">
<link rel=stylesheet href=/style.css></head><body>
<header class=hero style="background:{color}"></header><main><h1>{name}</h1>{body}</main></body></html>"""
LOREM = "<p>Lorem ipsum dolor sit amet, consectetur adipiscing elit.</p>" * 30  # taller than the viewport
CSS = "body{margin:0;font-family:sans-serif} .hero{height:140px} main{padding:40px}"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets its own state dir, and never talks to the real GitHub."""
    state = tmp_path / "state"
    monkeypatch.setenv("QA_SCREENS_STATE_DIR", str(state))
    monkeypatch.setenv("QA_SCREENS_ERROR_REPORTING", "local")
    monkeypatch.setenv("QA_SCREENS_GITHUB_API", "http://127.0.0.1:9")  # unroutable unless a test sets `github`
    for var in ("QA_SCREENS_GITHUB_TOKEN", "GITHUB_TOKEN", "QA_SCREENS_ROOT", "QA_SCREENS_BASE_URL",
                "QA_SCREENS_ISSUE_REPO", "FIGMA_TOKEN", "QA_SCREENS_PROFILE", "QA_SCREENS_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    # `gh auth token` must not leak the developer's real token into tests.
    monkeypatch.setenv("PATH", os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                                                if not (Path(p) / "gh").exists()))
    return state


# --------------------------------------------------------------------------- fake app

@dataclass
class App:
    url: str
    root: Path
    requests: list[dict] = field(default_factory=list)
    storage_reports: list[dict] = field(default_factory=list)
    token_requests: list[dict] = field(default_factory=list)
    issued: list[str] = field(default_factory=list)
    token_ttl: int = 3600
    client_secret: str = "app-secret"
    users: dict = field(default_factory=lambda: {"ada": "lovelace"})
    deny_authorize: bool = False
    _challenges: dict = field(default_factory=dict)
    _sessions: set = field(default_factory=set)
    _refresh: set = field(default_factory=set)

    def seen(self, path: str) -> list[dict]:
        return [r for r in self.requests if r["path"].split("?")[0] == path]

    def last(self, path: str) -> dict:
        hits = self.seen(path)
        assert hits, f"app never received {path}; got {[r['path'] for r in self.requests]}"
        return hits[-1]

    def write_page(self, name: str, color: str = "#2a6", body: str = LOREM, extra: str = "") -> None:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(PAGE.format(name=name, color=color, body=body) + extra)

    def set_css(self, css: str) -> None:
        (self.root / "style.css").write_text(css)


def _handler(app: App):
    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(app.root), **kw)

        def log_message(self, *a):
            pass

        def _record(self, body: bytes = b""):
            app.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                 "cookies": self.headers.get("Cookie") or "", "body": body.decode(errors="replace")})

        def _send(self, status: int, body: str = "", ctype: str = "text/html", headers: dict | None = None):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, obj: dict):
            self._send(status, json.dumps(obj), "application/json")

        def _session(self) -> bool:
            m = re.search(r"session=([\w-]+)", self.headers.get("Cookie") or "")
            return bool(m and m.group(1) in app._sessions)

        def do_GET(self):
            self._record()
            path, _, query = self.path.partition("?")
            q = parse_qs(query)
            if path == "/echo":
                # Reports what the page can see (storage, JS cookies) back to the server.
                return self._send(200, "<html><body><h1>echo</h1><script>"
                                  "fetch('/storage-report',{method:'POST',body:JSON.stringify({"
                                  "local:{...localStorage},session:{...sessionStorage},cookie:document.cookie})});"
                                  "</script></body></html>")
            if path == "/login":
                return self._send(200, "<form method=post action=/login><input name=username>"
                                       "<input type=password name=password><button type=submit>Sign in</button></form>")
            if path in ("/dashboard", "/dashboard/"):
                if not self._session():
                    return self._send(302, headers={"Location": "/login"})
                return self._send(200, "<h1>Dashboard</h1><script>sessionStorage.setItem('csrf','c-123')</script>")
            if path in ("/private", "/private/"):
                if not self._session():
                    return self._send(302, headers={"Location": "/login"})
                return self._send(200, "<h1>Private</h1>")
            if path == "/basic/":
                expected = "Basic " + base64.b64encode(b"qa:pw").decode()
                if self.headers.get("Authorization") != expected:
                    return self._send(401, "no", headers={"WWW-Authenticate": 'Basic realm="t"'})
                return self._send(200, "<h1>basic ok</h1>")
            if path == "/authorize":
                redirect, state = q["redirect_uri"][0], q["state"][0]
                if app.deny_authorize:
                    target = redirect + "?" + urlencode({"error": "access_denied", "error_description": "user said no", "state": state})
                else:
                    code = secrets.token_hex(8)
                    app._challenges[code] = (q.get("code_challenge", [""])[0], q.get("code_challenge_method", [""])[0])
                    target = redirect + "?" + urlencode({"code": code, "state": state})
                return self._send(200, f"<script>location.href={json.dumps(target)}</script>")
            super().do_GET()

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._record(body)
            path = self.path.split("?")[0]
            if path == "/storage-report":
                app.storage_reports.append(json.loads(body))
                return self._send(204)
            if path == "/login":
                form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                if app.users.get(form.get("username")) == form.get("password"):
                    sid = secrets.token_hex(8)
                    app._sessions.add(sid)
                    return self._send(302, headers={"Location": "/dashboard/", "Set-Cookie": f"session={sid}; Path=/"})
                return self._send(200, "<p>Wrong password</p><form method=post action=/login><input name=username>"
                                       "<input type=password name=password><button type=submit>Sign in</button></form>")
            if path == "/token":
                return self._token(body)
            self._send(404)

        def _token(self, body: bytes):
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                form["client_id"], _, form["client_secret"] = base64.b64decode(auth[6:]).decode().partition(":")
            app.token_requests.append(form)
            grant = form.get("grant_type")
            if grant == "client_credentials" and form.get("client_secret") != app.client_secret:
                return self._json(401, {"error": "invalid_client", "error_description": "client authentication failed"})
            if grant == "password" and app.users.get(form.get("username")) != form.get("password"):
                return self._json(400, {"error": "invalid_grant", "error_description": "bad user credentials"})
            if grant == "refresh_token" and form.get("refresh_token") not in app._refresh:
                return self._json(400, {"error": "invalid_grant", "error_description": "unknown refresh token"})
            if grant == "authorization_code":
                challenge, method = app._challenges.pop(form.get("code"), (None, None))
                verifier = form.get("code_verifier", "")
                computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if challenge is None or method != "S256" or computed != challenge:
                    return self._json(400, {"error": "invalid_grant", "error_description": "PKCE verification failed"})
            if grant not in ("client_credentials", "password", "refresh_token", "authorization_code"):
                return self._json(400, {"error": "unsupported_grant_type"})
            token = f"at-{len(app.issued) + 1}"
            app.issued.append(token)
            resp = {"access_token": token, "token_type": "bearer", "expires_in": app.token_ttl}
            if grant != "client_credentials":
                rt = f"rt-{len(app.issued)}"
                app._refresh.add(rt)
                resp["refresh_token"] = rt
            self._json(200, resp)

    return H


def _start(handler, host="127.0.0.1"):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://{host}:{httpd.server_address[1]}"


def _make_app(root: Path, host="127.0.0.1"):
    root.mkdir(parents=True, exist_ok=True)
    app = App(url="", root=root)
    httpd, app.url = _start(_handler(app), host)
    app.set_css(CSS)
    for name, color in (("home", "#2a6"), ("about", "#26a"), ("contact", "#a62")):
        app.write_page(name, color)
    return app, httpd


@pytest.fixture
def app(tmp_path):
    a, httpd = _make_app(tmp_path / "site")
    yield a
    httpd.shutdown()


@pytest.fixture
def other_app(tmp_path):
    """A second app on a different host (cookies are per host, not per port)."""
    a, httpd = _make_app(tmp_path / "other-site", host="localhost")
    yield a
    httpd.shutdown()


# Back-compat names used by the older tests.
@pytest.fixture
def site(app):
    return app.root, app.url


@pytest.fixture
def second_server(other_app):
    return other_app.url


def closed_port_url() -> str:
    """A URL where nothing listens (not port 9: Chromium refuses 'unsafe' ports)."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------- fake GitHub

@dataclass
class GitHub:
    url: str
    issues: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    fail_with: int | None = None


@pytest.fixture
def github(monkeypatch):
    gh = GitHub(url="")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _common(self, body=b""):
            gh.requests.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                                "body": body.decode(errors="replace")})
            if gh.fail_with:
                self._reply(gh.fail_with, {"message": "unavailable"})
                return False
            return True

        def do_GET(self):
            if not self._common():
                return
            q = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            m = re.search(r"qa-screens-fingerprint:(\w+)", q)
            hits = [i for i in gh.issues if i["state"] == "open" and m and f"qa-screens-fingerprint:{m.group(1)}" in i["body"]]
            self._reply(200, {"total_count": len(hits), "items": hits})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not self._common(body):
                return
            payload = json.loads(body)
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues(?:/(\d+)/comments)?", self.path)
            if not m:
                return self._reply(404, {})
            if m.group(2):
                gh.comments.append({"repo": m.group(1), "number": int(m.group(2)), **payload})
                return self._reply(201, {"id": len(gh.comments)})
            number = len(gh.issues) + 1
            issue = {"repo": m.group(1), "number": number, "state": "open",
                     "html_url": f"https://github.com/{m.group(1)}/issues/{number}", **payload}
            gh.issues.append(issue)
            self._reply(201, issue)

    httpd, gh.url = _start(H)
    monkeypatch.setenv("QA_SCREENS_GITHUB_API", gh.url)
    monkeypatch.setenv("QA_SCREENS_GITHUB_TOKEN", "gh-test-token")
    monkeypatch.setenv("QA_SCREENS_ERROR_REPORTING", "on")
    yield gh
    httpd.shutdown()


# --------------------------------------------------------------------------- fake Figma

@pytest.fixture
def figma(monkeypatch, tmp_path):
    """Figma file with two frames; image URLs point back at this server (like Figma's CDN)."""
    import cv2
    import numpy as np

    img = np.full((200, 300, 3), 255, np.uint8)
    cv2.rectangle(img, (10, 10), (290, 60), (40, 120, 40), -1)
    png = cv2.imencode(".png", img)[1].tobytes()
    seen: list[dict] = []
    base = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append({"path": self.path, "token": self.headers.get("X-Figma-Token")})
            if self.path.startswith("/v1/files/FILE1"):
                body = {"document": {"type": "DOCUMENT", "children": [{"type": "CANVAS", "children": [
                    {"type": "FRAME", "name": "home", "id": "1:1"},
                    {"type": "FRAME", "name": "about us", "id": "1:2"}]}]}}
            elif self.path.startswith("/v1/images/FILE1"):
                ids = parse_qs(urlsplit(self.path).query)["ids"][0].split(",")
                body = {"images": {i: base["url"] + f"/cdn/{i.replace(':', '_')}.png" for i in ids}}
            elif self.path.startswith("/cdn/"):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd, base["url"] = _start(H)
    monkeypatch.setenv("QA_SCREENS_FIGMA_API", base["url"] + "/v1/")
    yield seen
    httpd.shutdown()


# --------------------------------------------------------------------------- project + MCP client

@dataclass
class Project:
    root: Path
    app: App

    @property
    def refs(self) -> Path:
        return self.root / "screenshots"

    @property
    def runtime(self) -> Path:
        return self.root / ".qa-screens" / "runtime"

    def configure(self, **cfg) -> None:
        data = {"base_url": self.app.url, "route_template": "/{name}/", **cfg}
        (self.root / ".qa-screens.json").write_text(json.dumps(data))


@pytest.fixture
def project(tmp_path, app, monkeypatch):
    """A project dir (the MCP server's cwd) pointing at the fake app, with no references yet."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    p = Project(root=root, app=app)
    p.configure()
    return p


@dataclass
class ToolResult:
    is_error: bool
    text: str
    images: list

    @property
    def data(self):
        return json.loads(self.text)


class QA:
    """Calls qa-screens tools through a real MCP client session."""

    def __init__(self, client):
        self.client = client

    async def __call__(self, tool: str, **args) -> ToolResult:
        res = await self.client.call_tool(tool, args)
        texts = [c.text for c in res.content if c.type == "text"]
        images = [c for c in res.content if c.type == "image"]
        return ToolResult(bool(res.is_error), texts[0] if texts else "", images)

    async def ok(self, tool: str, **args) -> ToolResult:
        r = await self(tool, **args)
        assert not r.is_error, f"{tool} failed: {r.text}"
        return r

    async def error(self, tool: str, **args) -> str:
        r = await self(tool, **args)
        assert r.is_error, f"{tool} unexpectedly succeeded: {r.text[:500]}"
        return r.text


@pytest.fixture
async def qa(project):
    from mcp import Client

    import qa_screens.server as server

    # Fresh browser/profile singletons so each test sees its own state dir.
    server._bm = None
    server._profiles = None

    # pytest-asyncio runs fixture setup and teardown in different tasks, but anyio cancel
    # scopes must be exited by the task that entered them: own the session in one task.
    ready, done, holder = asyncio.Event(), asyncio.Event(), {}

    async def session():
        async with Client(server.mcp) as client:
            holder["client"] = client
            ready.set()
            await done.wait()

    task = asyncio.create_task(session())
    waiter = asyncio.create_task(ready.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()  # surface the connection error
    yield QA(holder["client"])
    done.set()
    await task
    if server._bm is not None:
        await server._bm.stop()
    server._bm = None
    server._profiles = None


@pytest.fixture
async def golden(qa, project):
    """Reference screenshots captured from the unchanged app (desktop + one mobile page)."""
    await qa.ok("capture_set", label="golden", pages=["home", "about", "contact", "home-mobile"])
    project.refs.mkdir(exist_ok=True)
    for f in (project.runtime / "sets" / "golden").glob("*.png"):
        shutil.copy(f, project.refs / f.name)
    return project.refs
