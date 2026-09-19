"""OAuth 2.0 token acquisition and refresh.

Supported grant types (profile["oauth"]["grant_type"]):
  client_credentials  machine-to-machine
  password            resource-owner password (legacy, still common on internal apps)
  refresh_token       refresh an existing token
  authorization_code  browser-driven with PKCE (S256); the redirect is intercepted
                      inside Playwright, so no local callback server is needed.

oauth config keys: token_url, client_id, client_secret, scope, audience,
username, password, authorize_url, redirect_uri, client_auth ("post" | "basic"),
extra_params ({...} added to the token request).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..errors import AuthError
from .profiles import resolve

logger = logging.getLogger("qa_screens.auth")

EXPIRY_SKEW_S = 60
_transport: httpx.AsyncBaseTransport | None = None  # injectable for tests


def token_expired(token: dict | None) -> bool:
    if not token or not token.get("access_token"):
        return True
    exp = token.get("expires_at")
    return bool(exp) and time.time() >= exp - EXPIRY_SKEW_S


async def request_token(oauth: dict, grant_type: str, **params) -> dict:
    cfg = resolve(oauth)
    if not cfg.get("token_url"):
        raise AuthError("oauth.token_url is required")
    data = {"grant_type": grant_type, **params}
    for key in ("scope", "audience"):
        if cfg.get(key) and key not in data:
            data[key] = cfg[key]
    data.update(cfg.get("extra_params") or {})
    headers = {"Accept": "application/json"}
    client_id, client_secret = cfg.get("client_id"), cfg.get("client_secret")
    if cfg.get("client_auth") == "basic" and client_id:
        raw = f"{client_id}:{client_secret or ''}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    else:
        if client_id:
            data["client_id"] = client_id
        if client_secret:
            data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=30, transport=_transport) as client:
        try:
            resp = await client.post(cfg["token_url"], data=data, headers=headers)
        except httpx.HTTPError as e:
            raise AuthError(f"Token endpoint unreachable: {e}") from e
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or "access_token" not in body:
        err = body.get("error_description") or body.get("error") or resp.text[:200]
        raise AuthError(f"Token request ({grant_type}) failed with HTTP {resp.status_code}: {err}")
    token = {k: body[k] for k in ("access_token", "token_type", "refresh_token", "scope", "id_token") if k in body}
    if body.get("expires_in"):
        token["expires_at"] = time.time() + float(body["expires_in"])
    token.setdefault("token_type", "Bearer")
    return token


async def fetch_token(profile: dict) -> dict:
    """Obtain a fresh token for the profile's non-interactive grant."""
    oauth = profile.get("oauth") or {}
    grant = oauth.get("grant_type")
    old = profile.get("token") or {}
    if old.get("refresh_token") and oauth.get("token_url"):
        try:
            new = await request_token(oauth, "refresh_token", refresh_token=resolve(old["refresh_token"]))
            new.setdefault("refresh_token", old["refresh_token"])  # not every IdP rotates it
            return new
        except AuthError as e:
            if grant not in ("client_credentials", "password"):
                raise
            logger.info("Refresh failed (%s); falling back to %s grant", e, grant)
    if grant == "client_credentials":
        return await request_token(oauth, "client_credentials")
    if grant == "password":
        cfg = resolve(oauth)
        return await request_token(oauth, "password", username=cfg.get("username", ""), password=cfg.get("password", ""))
    if grant == "authorization_code":
        raise AuthError("Token expired and has no refresh_token; run auth_oauth_login again for this profile")
    raise AuthError("Profile has no usable token and no oauth.grant_type to obtain one")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def authorization_code_flow(context, oauth: dict, timeout_s: float = 300) -> dict:
    """Run the auth-code + PKCE flow in a Playwright context and exchange the code."""
    cfg = resolve(oauth)
    for key in ("authorize_url", "token_url", "client_id", "redirect_uri"):
        if not cfg.get(key):
            raise AuthError(f"oauth.{key} is required for the authorization_code grant")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if cfg.get("scope"):
        params["scope"] = cfg["scope"]
    if cfg.get("audience"):
        params["audience"] = cfg["audience"]
    params.update(cfg.get("authorize_params") or {})
    sep = "&" if "?" in cfg["authorize_url"] else "?"
    url = cfg["authorize_url"] + sep + urlencode(params)

    redirect = urlsplit(cfg["redirect_uri"])
    loop = asyncio.get_running_loop()
    captured: asyncio.Future[str] = loop.create_future()

    async def _intercept(route):
        if not captured.done():
            captured.set_result(route.request.url)
        await route.fulfill(status=200, content_type="text/html",
                            body="<h1>Signed in</h1><p>qa-screens captured the authorization code. You can close this window.</p>")

    pattern = f"{redirect.scheme}://{redirect.netloc}{redirect.path}**"
    await context.route(pattern, _intercept)
    page = await context.new_page()
    try:
        await page.goto(url)
        try:
            final = await asyncio.wait_for(captured, timeout_s)
        except asyncio.TimeoutError as e:
            raise AuthError(f"No redirect to {cfg['redirect_uri']} within {timeout_s:.0f}s — login not completed") from e
    finally:
        await context.unroute(pattern, _intercept)
        await page.close()

    q = parse_qs(urlsplit(final).query)
    if "error" in q:
        raise AuthError(f"Authorization server returned error: {q.get('error_description', q['error'])[0]}")
    if q.get("state", [None])[0] != state:
        raise AuthError("OAuth state mismatch — possible CSRF, aborting")
    code = q.get("code", [None])[0]
    if not code:
        raise AuthError("Redirect did not contain an authorization code")
    return await request_token(oauth, "authorization_code", code=code, redirect_uri=cfg["redirect_uri"], code_verifier=verifier)
