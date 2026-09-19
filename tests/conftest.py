import http.server
import json
import threading
from functools import partial
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets its own state dir and never posts to GitHub."""
    state = tmp_path / "state"
    monkeypatch.setenv("QA_SCREENS_STATE_DIR", str(state))
    monkeypatch.setenv("QA_SCREENS_ERROR_REPORTING", "local")
    for var in ("QA_SCREENS_GITHUB_TOKEN", "GITHUB_TOKEN", "QA_SCREENS_ROOT", "QA_SCREENS_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return state


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/echo"):
            body = json.dumps({"authorization": self.headers.get("Authorization"), "cookie": self.headers.get("Cookie"),
                               "x_api_key": self.headers.get("X-Api-Key")})
            html = (f"<html><body><pre id=srv>{body}</pre><pre id=ls></pre><script>"
                    "document.getElementById('ls').textContent = JSON.stringify({local: {...localStorage}, session: {...sessionStorage}});"
                    "</script></body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        super().do_GET()


def _serve(directory: Path):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(directory)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture
def site(tmp_path):
    """A tiny static site with two pages, served on a random port."""
    root = tmp_path / "site"
    for name, color in (("home", "#2a6"), ("about", "#26a")):
        d = root / name
        d.mkdir(parents=True)
        (d / "index.html").write_text(
            f"<!doctype html><html><head><link rel=stylesheet href=/style.css></head><body>"
            f"<header style='background:{color};height:120px'></header><main><h1>{name}</h1>"
            + "<p>Lorem ipsum dolor sit amet.</p>" * 20
            + "</main></body></html>"
        )
    (root / "style.css").write_text("body{margin:0;font-family:sans-serif} main{padding:40px}")
    httpd, url = _serve(root)
    yield root, url
    httpd.shutdown()


@pytest.fixture
def second_server(tmp_path):
    d = tmp_path / "other"
    d.mkdir()
    httpd, url = _serve(d)
    # A different host, not just a different port: cookies are scoped per host.
    yield url.replace("127.0.0.1", "localhost")
    httpd.shutdown()
