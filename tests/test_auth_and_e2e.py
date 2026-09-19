import json
import os
import shutil
import stat
import sys
import time

import httpx
import pytest

from qa_screens.auth import ProfileStore
from qa_screens.auth import oauth as oauth_mod
from qa_screens.browser import BrowserManager, capture_page
from qa_screens.config import load_config
from qa_screens.errors import AuthError
from qa_screens import runner


@pytest.fixture
async def bm():
    m = BrowserManager(ProfileStore())
    yield m
    await m.stop()


def _cfg(tmp_path, base_url, **extra):
    (tmp_path / ".qa-screens.json").write_text(json.dumps({"base_url": base_url, "route_template": "/{name}/", **extra}))
    return load_config(tmp_path)


async def _echo(bm, cfg, url, profile):
    ctx = await bm.context(cfg, False, profile, url)
    page = await ctx.new_page()
    await page.goto(url)
    srv = json.loads(await page.inner_text("#srv"))
    ls = json.loads(await page.inner_text("#ls"))
    await page.close()
    return srv, ls


def test_profile_files_are_private():
    store = ProfileStore()
    store.save({"name": "p", "headers": {"X": "1"}})
    assert stat.S_IMODE(os.stat(store._path("p")).st_mode) == 0o600
    with pytest.raises(Exception):
        store.get("../etc")


async def test_token_cookie_storage_applied_only_to_own_origin(tmp_path, site, second_server, bm, monkeypatch):
    _, url = site
    cfg = _cfg(tmp_path, url)
    monkeypatch.setenv("APP_TOKEN", "tok-123")
    ProfileStore().save({
        "name": "app",
        "origins": [url],
        "token": {"access_token": "env:APP_TOKEN", "token_type": "bearer"},
        "apply_token_as": ["header", "local_storage:jwt", "session_storage_json:auth"],
        "headers": {"X-Api-Key": "k1"},
        "cookies": [{"name": "sid", "value": "s1"}],
    })
    srv, ls = await _echo(bm, cfg, url + "/echo", "app")
    assert srv["authorization"] == "Bearer tok-123"
    assert srv["x_api_key"] == "k1"
    assert "sid=s1" in srv["cookie"]
    assert ls["local"]["jwt"] == "tok-123"
    assert json.loads(ls["session"]["auth"])["access_token"] == "tok-123"

    # Same context, other origin: no credentials leak.
    srv2, ls2 = await _echo(bm, cfg, second_server + "/echo", "app")
    assert srv2 == {"authorization": None, "cookie": None, "x_api_key": None}
    assert ls2 == {"local": {}, "session": {}}


async def test_client_credentials_fetch_and_auto_refresh(monkeypatch):
    seen = []

    def handler(request):
        form = dict(x.split("=") for x in request.content.decode().split("&"))
        seen.append(form["grant_type"])
        if form["grant_type"] == "client_credentials":
            assert form["client_secret"] == "s3cret"
            return httpx.Response(200, json={"access_token": f"at{len(seen)}", "expires_in": 3600})
        return httpx.Response(400, json={"error": "invalid_grant"})

    monkeypatch.setattr(oauth_mod, "_transport", httpx.MockTransport(handler))
    monkeypatch.setenv("CS", "s3cret")
    profile = {"name": "m2m", "oauth": {"grant_type": "client_credentials", "token_url": "https://idp/token",
                                        "client_id": "c", "client_secret": "env:CS"}}
    tok = await oauth_mod.fetch_token(profile)
    assert tok["access_token"] == "at1" and not oauth_mod.token_expired(tok)

    from qa_screens.auth import prepare_profile

    store = ProfileStore()
    profile["token"] = {**tok, "expires_at": time.time() - 10}
    store.save(profile)
    refreshed = await prepare_profile(store, "m2m")
    assert refreshed["token"]["access_token"] == "at2"
    assert store.get("m2m")["token"]["access_token"] == "at2"


async def test_token_error_is_user_error(monkeypatch):
    monkeypatch.setattr(oauth_mod, "_transport", httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": "invalid_client", "error_description": "bad secret"})))
    with pytest.raises(AuthError, match="bad secret"):
        await oauth_mod.request_token({"token_url": "https://idp/token"}, "client_credentials")


async def test_authorization_code_pkce_flow(site, bm, monkeypatch):
    """The 'IdP' page immediately redirects back with a code; we intercept and exchange it."""
    root, url = site
    (root / "authorize").mkdir()
    (root / "authorize" / "index.html").write_text(
        "<script>const p=new URLSearchParams(location.search);"
        "location.href=p.get('redirect_uri')+'?code=CODE42&state='+p.get('state');</script>")
    exchanged = {}

    def handler(request):
        exchanged.update(dict(x.split("=") for x in request.content.decode().split("&")))
        return httpx.Response(200, json={"access_token": "user-at", "refresh_token": "rt", "expires_in": 60})

    monkeypatch.setattr(oauth_mod, "_transport", httpx.MockTransport(handler))
    browser = await bm.browser()
    ctx = await browser.new_context()
    tok = await oauth_mod.authorization_code_flow(ctx, {
        "authorize_url": url + "/authorize/", "token_url": "https://idp/token", "client_id": "spa",
        "redirect_uri": "http://app.invalid/callback"}, timeout_s=20)
    await ctx.close()
    assert tok["access_token"] == "user-at" and tok["refresh_token"] == "rt"
    assert exchanged["code"] == "CODE42" and len(exchanged["code_verifier"]) >= 43


async def test_qa_run_pass_then_detect_regression(tmp_path, site, bm):
    root, url = site
    cfg = _cfg(tmp_path, url)
    cap = await runner.capture_set(bm, cfg, "golden", pages=["home", "about", "about-mobile"])
    assert cap["captured"] == 3
    cfg.references_path.mkdir()
    for f in (cfg.runtime_path / "sets" / "golden").glob("*.png"):
        shutil.copy(f, cfg.references_path / f.name)

    ok = await runner.qa_batch(bm, cfg)
    assert ok["passed"] == 3, ok

    (root / "style.css").write_text("body{margin:0;font-family:serif} main{padding:90px} header{height:400px!important}")
    bad = await runner.qa_batch(bm, cfg, pages=["about"])
    page = bad["pages"][0]
    assert page["status"] == "FAIL" and page["regions"] and os.path.exists(page["preview_path"])
    assert (cfg.runtime_path / "reports" / "latest.json").exists()

    missing = await runner.qa_batch(bm, cfg, pages=["nope"])
    assert missing["pages"][0]["status"] == "ERROR"


async def test_parity_sets(tmp_path, site, bm):
    root, url = site
    cfg = _cfg(tmp_path, url)
    await runner.capture_set(bm, cfg, "before", pages=["home", "about"])
    await runner.capture_set(bm, cfg, "after", pages=["home", "about"])
    assert runner.compare_sets(cfg, "before", "after")["identical"]
    (root / "home" / "index.html").write_text("<body><h1 style='font-size:80px'>changed</h1></body>")
    await runner.capture_set(bm, cfg, "after", pages=["home", "about"])
    r = runner.compare_sets(cfg, "before", "after")
    assert [c["page"] for c in r["changed"]] == ["home"]


async def test_trailing_slash_redirect_is_not_a_login_wall(tmp_path, site, bm):
    _, url = site
    cfg = _cfg(tmp_path, url)
    ctx = await bm.context(cfg, False, None)
    meta = await capture_page(ctx, url + "/home", str(tmp_path / "x.png"))  # server redirects to /home/
    assert meta["status"] == 200 and "warning" not in meta


async def test_mcp_server_over_stdio(tmp_path, site):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    _, url = site
    _cfg(tmp_path, url)
    params = StdioServerParameters(command=sys.executable, args=["-m", "qa_screens"], cwd=str(tmp_path),
                                   env={**os.environ, "QA_SCREENS_ERROR_REPORTING": "local"})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"run_qa", "qa_page", "capture", "compare_sets", "auth_update_profile", "auth_oauth_login", "error_reports"} <= names
        res = await s.call_tool("qa_config", {})
        assert json.loads(res.content[0].text)["base_url"] == url
        res = await s.call_tool("capture", {"url": url + "/home/", "name": "../../evil"})
        assert not res.is_error and res.content[1].type == "image"
        assert (tmp_path / ".qa-screens/runtime/captures/evil.png").exists()
        res = await s.call_tool("qa_page", {"page": "missing"})
        assert res.is_error and "not found" in res.content[0].text
