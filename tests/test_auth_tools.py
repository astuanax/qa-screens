"""Testing apps behind a login: auth profiles as a developer sets them up.

Every assertion is about what the app actually received (headers, cookies, what
the page could read from storage), never about internal profile structure.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import os
import stat
import urllib.parse
import urllib.request

SECRET = "tok-SUPER-secret-42"


async def visit(qa, url, profile=None, **kw):
    args = {"url": url, "return_image": False, **kw}
    if profile:
        args["profile"] = profile
    return (await qa.ok("capture", **args)).data


def storage_seen(app):
    assert app.storage_reports, "the /echo page never reported its storage"
    return app.storage_reports[-1]


def all_text(*results) -> str:
    return " ".join(r if isinstance(r, str) else r.text for r in results)


# --------------------------------------------------------------------------- tokens & placement

async def test_bearer_token_is_sent_to_the_app(qa, project):
    await qa.ok("auth_update_profile", name="app", access_token=SECRET)
    await visit(qa, project.app.url + "/echo", "app")
    assert project.app.last("/echo")["headers"]["Authorization"] == f"Bearer {SECRET}"


async def test_credentials_never_reach_other_origins(qa, project, other_app):
    await qa.ok("auth_update_profile", name="app", access_token=SECRET, headers={"X-Tenant": "acme"},
                apply_token_as=["header", "local_storage:jwt", "cookie:at"])
    await visit(qa, project.app.url + "/echo", "app")
    await visit(qa, other_app.url + "/echo", "app")
    leaked = other_app.last("/echo")
    assert "Authorization" not in leaked["headers"] and "X-Tenant" not in leaked["headers"]
    assert SECRET not in leaked["cookies"]
    assert storage_seen(other_app)["local"] == {}


async def test_token_can_be_placed_where_the_app_expects_it(qa, project):
    await qa.ok("auth_update_profile", name="spa", access_token=SECRET, apply_token_as=[
        "header:X-Api-Key", "local_storage:jwt", "session_storage:auth", "local_storage_json:oidc", "cookie:at"])
    await visit(qa, project.app.url + "/echo", "spa")
    req = project.app.last("/echo")
    assert req["headers"]["X-Api-Key"] == SECRET and "Authorization" not in req["headers"]
    assert f"at={SECRET}" in req["cookies"]
    seen = storage_seen(project.app)
    assert seen["local"]["jwt"] == SECRET
    assert seen["session"]["auth"] == SECRET
    assert json.loads(seen["local"]["oidc"])["access_token"] == SECRET


async def test_token_is_scoped_to_the_base_url_by_default(qa, project, other_app):
    await qa.ok("auth_update_profile", name="app", access_token=SECRET)  # no origins given
    await visit(qa, project.app.url + "/echo", "app")
    assert project.app.last("/echo")["headers"]["Authorization"] == f"Bearer {SECRET}"
    # Capturing a foreign URL with the profile must not hand it the token.
    await visit(qa, other_app.url + "/echo", "app")
    assert "Authorization" not in other_app.last("/echo")["headers"]
    await qa.ok("auth_check", name="app", url=other_app.url + "/echo")
    assert "Authorization" not in other_app.last("/echo")["headers"]


async def test_explicit_origins(qa, project, other_app):
    await qa.ok("auth_update_profile", name="api", access_token=SECRET, origins=[other_app.url])
    await visit(qa, project.app.url + "/echo", "api")
    await visit(qa, other_app.url + "/echo", "api")
    assert "Authorization" not in project.app.last("/echo")["headers"]
    assert other_app.last("/echo")["headers"]["Authorization"] == f"Bearer {SECRET}"


# --------------------------------------------------------------------------- secrets hygiene

async def test_secrets_can_come_from_the_environment(qa, project, monkeypatch):
    monkeypatch.setenv("APP_TOKEN", SECRET)
    saved = await qa.ok("auth_update_profile", name="app", access_token="env:APP_TOKEN")
    await visit(qa, project.app.url + "/echo", "app")
    assert project.app.last("/echo")["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in saved.text


async def test_missing_environment_secret_is_a_clear_error(qa, project):
    await qa.ok("auth_update_profile", name="app", access_token="env:NOT_SET_ANYWHERE")
    msg = await qa.error("capture", url=project.app.url + "/echo", profile="app")
    assert "NOT_SET_ANYWHERE" in msg and "not set" in msg


async def test_secrets_are_never_echoed_back(qa, project):
    r1 = await qa.ok("auth_update_profile", name="app", access_token=SECRET, refresh_token="rt-" + SECRET,
                     headers={"X-Api-Key": "hdr-" + SECRET}, cookies=[{"name": "sid", "value": "ck-" + SECRET}],
                     http_credentials={"username": "u", "password": "pw-" + SECRET},
                     local_storage={project.app.url: {"k": "ls-" + SECRET}})
    r2 = await qa.ok("auth_profiles")
    r3 = await qa.ok("auth_profiles", name="app")
    assert SECRET not in all_text(r1, r2, r3)
    summary = r3.data
    assert summary["token"] is True and summary["headers"] == ["X-Api-Key"]
    assert summary["cookies"] == [f"sid@None"] and summary["http_credentials"] is True
    assert summary["local_storage_keys"] == {project.app.url: ["k"]}


async def test_profiles_are_stored_privately_outside_the_project(qa, project, isolated_state):
    await qa.ok("auth_update_profile", name="app", access_token=SECRET)
    files = list((isolated_state / "profiles").glob("app*.json"))
    assert files and all(stat.S_IMODE(os.stat(f).st_mode) == 0o600 for f in files)
    assert not any(SECRET in p.read_text(errors="ignore") for p in project.root.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- editing profiles

async def test_updates_merge_and_clear_removes(qa, project):
    await qa.ok("auth_update_profile", name="app", headers={"X-A": "1"}, cookies=[{"name": "c", "value": "old"}])
    await qa.ok("auth_update_profile", name="app", headers={"X-B": "2"}, cookies=[{"name": "c", "value": "new"}],
                access_token=SECRET)
    await visit(qa, project.app.url + "/echo", "app")
    req = project.app.last("/echo")
    assert req["headers"]["X-A"] == "1" and req["headers"]["X-B"] == "2"
    assert "c=new" in req["cookies"] and "c=old" not in req["cookies"]

    await qa.ok("auth_update_profile", name="app", clear=["token", "headers"])
    await visit(qa, project.app.url + "/echo", "app")
    req = project.app.last("/echo")
    assert "Authorization" not in req["headers"] and "X-A" not in req["headers"]
    assert "c=new" in req["cookies"]


async def test_explicit_web_storage(qa, project):
    await qa.ok("auth_update_profile", name="app", local_storage={project.app.url: {"theme": "dark"}},
                session_storage={project.app.url: {"tab": "2"}})
    await visit(qa, project.app.url + "/echo", "app")
    seen = storage_seen(project.app)
    assert seen["local"]["theme"] == "dark" and seen["session"]["tab"] == "2"


async def test_http_basic_auth(qa, project):
    assert (await visit(qa, project.app.url + "/basic/"))["status"] == 401
    await qa.ok("auth_update_profile", name="basic", http_credentials={"username": "qa", "password": "pw"})
    assert (await visit(qa, project.app.url + "/basic/", "basic"))["status"] == 200


async def test_profile_input_validation(qa, project):
    assert "Invalid profile name" in await qa.error("auth_update_profile", name="../x", access_token="t")
    assert "absolute URL" in await qa.error("auth_update_profile", name="p", origins=["example.com"])
    await qa.ok("auth_update_profile", name="p", access_token="t", apply_token_as=["carrier_pigeon"])
    assert "carrier_pigeon" in await qa.error("capture", url=project.app.url + "/echo", profile="p")
    await qa.ok("auth_update_profile", name="p2", access_token="t", apply_token_as=["local_storage"])
    assert "needs a key" in await qa.error("capture", url=project.app.url + "/echo", profile="p2")


async def test_unknown_and_deleted_profiles(qa, project):
    assert "does not exist" in await qa.error("capture", url=project.app.url, profile="ghost")
    await qa.ok("auth_update_profile", name="tmp", access_token=SECRET)
    assert (await qa.ok("auth_delete_profile", name="tmp")).data["deleted"] is True
    assert (await qa.ok("auth_delete_profile", name="tmp")).data["deleted"] is False
    assert "does not exist" in await qa.error("capture", url=project.app.url, profile="tmp")
    assert (await qa.ok("auth_profiles")).data == []


async def test_default_profile_from_config(qa, project):
    await qa.ok("auth_update_profile", name="app", access_token=SECRET)
    project.configure(default_profile="app")
    await visit(qa, project.app.url + "/echo")
    assert project.app.last("/echo")["headers"]["Authorization"] == f"Bearer {SECRET}"


# --------------------------------------------------------------------------- OAuth

def oauth_cfg(app, **kw):
    return {"token_url": app.url + "/token", "client_id": "qa-bot", **kw}


async def test_client_credentials_token_is_fetched_and_used(qa, project):
    r = await qa.ok("auth_update_profile", name="m2m", oauth=oauth_cfg(
        project.app, grant_type="client_credentials", client_secret="app-secret", scope="read"))
    assert r.data["token_fetched"] is True
    assert project.app.token_requests[-1]["scope"] == "read"
    await visit(qa, project.app.url + "/echo", "m2m")
    assert project.app.last("/echo")["headers"]["Authorization"] == "Bearer at-1"


async def test_client_secret_via_http_basic(qa, project):
    await qa.ok("auth_update_profile", name="m2m", oauth=oauth_cfg(
        project.app, grant_type="client_credentials", client_secret="app-secret", client_auth="basic"))
    req = project.app.last("/token")
    assert req["headers"]["Authorization"].startswith("Basic ") and "client_secret" not in req["body"]


async def test_wrong_client_secret_is_reported_to_the_user_not_as_a_bug(qa, project, isolated_state):
    msg = await qa.error("auth_update_profile", name="m2m", oauth=oauth_cfg(
        project.app, grant_type="client_credentials", client_secret="wrong"))
    assert "client authentication failed" in msg and "401" in msg
    assert not list((isolated_state / "reports" / "pending").glob("*.json"))


async def test_expired_token_is_renewed_during_a_long_session(qa, project):
    project.app.token_ttl = 61  # valid for ~1s once the 60s safety margin is applied
    await qa.ok("auth_update_profile", name="m2m", oauth=oauth_cfg(
        project.app, grant_type="client_credentials", client_secret="app-secret"))
    await visit(qa, project.app.url + "/echo", "m2m")
    assert project.app.last("/echo")["headers"]["Authorization"] == "Bearer at-1"
    await asyncio.sleep(1.5)
    await visit(qa, project.app.url + "/echo", "m2m")
    assert project.app.last("/echo")["headers"]["Authorization"] == "Bearer at-2"
    assert (await qa.ok("auth_profiles", name="m2m")).data["refreshable"] is True


async def test_password_grant_then_refresh_token(qa, project, monkeypatch):
    monkeypatch.setenv("QA_PASSWORD", "lovelace")
    project.app.token_ttl = 61
    await qa.ok("auth_update_profile", name="user", oauth=oauth_cfg(
        project.app, grant_type="password", username="ada", password="env:QA_PASSWORD"))
    assert project.app.token_requests[-1]["grant_type"] == "password"
    await asyncio.sleep(1.5)
    await visit(qa, project.app.url + "/echo", "user")
    assert project.app.token_requests[-1] == {**project.app.token_requests[-1], "grant_type": "refresh_token", "refresh_token": "rt-1"}
    assert project.app.last("/echo")["headers"]["Authorization"] == "Bearer at-2"


async def test_rejected_refresh_token_falls_back_to_the_grant(qa, project):
    project.app.token_ttl = 61
    await qa.ok("auth_update_profile", name="user", oauth=oauth_cfg(
        project.app, grant_type="password", username="ada", password="lovelace"))
    project.app._refresh.clear()  # the IdP revoked all refresh tokens
    await asyncio.sleep(1.5)
    await visit(qa, project.app.url + "/echo", "user")
    assert [r["grant_type"] for r in project.app.token_requests] == ["password", "refresh_token", "password"]
    assert project.app.last("/echo")["headers"]["Authorization"] == "Bearer at-2"


async def test_bad_user_password_is_a_clear_error(qa, project):
    msg = await qa.error("auth_update_profile", name="user", oauth=oauth_cfg(
        project.app, grant_type="password", username="ada", password="wrong"))
    assert "bad user credentials" in msg


def auth_code_cfg(app):
    return oauth_cfg(app, grant_type="authorization_code", authorize_url=app.url + "/authorize",
                     redirect_uri="http://app.invalid/callback", scope="openid")


async def test_authorization_code_with_pkce(qa, project):
    await qa.ok("auth_update_profile", name="user", oauth=auth_code_cfg(project.app))
    r = await qa.ok("auth_oauth_login", name="user", headed=False, timeout_s=20)
    assert r.data["token"] is True and r.data["refreshable"] is True
    exchange = project.app.token_requests[-1]
    assert exchange["grant_type"] == "authorization_code" and exchange["redirect_uri"] == "http://app.invalid/callback"
    authorize = urllib.parse.parse_qs(urllib.parse.urlsplit(project.app.last("/authorize")["path"]).query)
    assert authorize["code_challenge_method"] == ["S256"] and authorize["scope"] == ["openid"]
    await visit(qa, project.app.url + "/echo", "user")
    assert project.app.last("/echo")["headers"]["Authorization"] == "Bearer at-1"


async def test_authorization_denied_by_the_user(qa, project):
    project.app.deny_authorize = True
    await qa.ok("auth_update_profile", name="user", oauth=auth_code_cfg(project.app))
    assert "user said no" in await qa.error("auth_oauth_login", name="user", headed=False, timeout_s=20)


async def test_authorization_code_misconfiguration(qa, project):
    await qa.ok("auth_update_profile", name="user", oauth={"grant_type": "authorization_code", "token_url": "x"})
    assert "authorize_url" in await qa.error("auth_oauth_login", name="user", headed=False)
    await qa.ok("auth_update_profile", name="m2m", oauth=oauth_cfg(
        project.app, grant_type="client_credentials", client_secret="app-secret"))
    assert "client_credentials" in await qa.error("auth_oauth_login", name="m2m", headed=False)


# --------------------------------------------------------------------------- browser login / sessions

async def test_browser_login_saves_the_session(qa, project, monkeypatch):
    monkeypatch.setenv("QA_USER", "ada")
    monkeypatch.setenv("QA_PASS", "lovelace")
    assert "Redirected" in (await visit(qa, project.app.url + "/private/")).get("warning", "")
    r = await qa.ok("auth_browser_login", name="web", login_url=project.app.url + "/login", headed=False,
                    username="env:QA_USER", password="env:QA_PASS", timeout_s=15)
    assert r.data["landed_on"] == project.app.url and r.data["saved"]["storage_state"] is True
    meta = await visit(qa, project.app.url + "/private/", "web")
    assert meta["status"] == 200 and "warning" not in meta
    # sessionStorage written by the app after login is restored too
    await visit(qa, project.app.url + "/echo", "web")
    assert storage_seen(project.app)["session"]["csrf"] == "c-123"
    assert "lovelace" not in r.text


async def test_browser_login_success_conditions(qa, project):
    await qa.ok("auth_browser_login", name="a", login_url=project.app.url + "/login", headed=False,
                username="ada", password="lovelace", success_url_pattern=r"/dashboard/$", timeout_s=15)
    await qa.ok("auth_browser_login", name="b", login_url=project.app.url + "/login", headed=False,
                username="ada", password="lovelace", success_selector="h1:has-text('Dashboard')", timeout_s=15)
    for name in ("a", "b"):
        assert (await visit(qa, project.app.url + "/private/", name))["status"] == 200


async def test_failed_browser_login_times_out_with_a_clear_message(qa, project):
    msg = await qa.error("auth_browser_login", name="web", login_url=project.app.url + "/login", headed=False,
                         username="ada", password="wrong", success_url_pattern="/dashboard/", timeout_s=3)
    assert "Login not completed within 3s" in msg
    assert "does not exist" in await qa.error("auth_profiles", name="web")  # nothing half-saved


async def test_interactive_login_needs_a_display(qa, project, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert "needs a display" in await qa.error("auth_browser_login", name="web", login_url=project.app.url + "/login")


async def test_import_playwright_storage_state(qa, project):
    # Log in the way a Playwright test setup would, and hand the resulting state over.
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(project.app.url + "/login", data=urllib.parse.urlencode({"username": "ada", "password": "lovelace"}).encode())
    sid = next(c.value for c in jar if c.name == "session")
    state = {"cookies": [{"name": "session", "value": sid, "domain": "127.0.0.1", "path": "/", "expires": -1,
                          "httpOnly": False, "secure": False, "sameSite": "Lax"}], "origins": []}
    (project.root / "state.json").write_text(json.dumps(state))
    await qa.ok("auth_import_storage_state", name="pw", path="state.json")
    assert (await visit(qa, project.app.url + "/private/", "pw"))["status"] == 200
    (project.root / "bad.json").write_text('{"hello": 1}')
    assert "storage state" in await qa.error("auth_import_storage_state", name="x", path="bad.json")
    assert "Cannot read" in await qa.error("auth_import_storage_state", name="x", path="missing.json")


async def test_auth_check_tells_logged_in_from_logged_out(qa, project):
    await qa.ok("auth_update_profile", name="anon", headers={"X-Nothing": "1"})
    out = await qa.ok("auth_check", name="anon", url=project.app.url + "/private/")
    assert out.data["authenticated_guess"] is False and "/login" in out.data["final_url"] and len(out.images) == 1
    await qa.ok("auth_browser_login", name="web", login_url=project.app.url + "/login", headed=False,
                username="ada", password="lovelace", timeout_s=15)
    ok = await qa.ok("auth_check", name="web", url=project.app.url + "/private/")
    assert ok.data["authenticated_guess"] is True and ok.data["status"] == 200


async def test_visual_qa_of_a_page_behind_login(qa, project):
    """End to end: log in once, then run the normal QA loop on a protected page."""
    await qa.ok("auth_browser_login", name="web", login_url=project.app.url + "/login", headed=False,
                username="ada", password="lovelace", timeout_s=15)
    await qa.ok("capture_set", label="golden", pages=["private"], profile="web")
    project.refs.mkdir()
    (project.refs / "private.png").write_bytes((project.runtime / "sets" / "golden" / "private.png").read_bytes())
    logged_in = (await qa.ok("run_qa", profile="web")).data["pages"][0]
    assert logged_in["status"] == "PASS" and "warning" not in logged_in
    anonymous = (await qa.ok("run_qa")).data["pages"][0]
    assert "Redirected" in anonymous["warning"]
