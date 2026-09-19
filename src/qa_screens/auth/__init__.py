"""Apply auth profiles to Playwright browser contexts."""
from __future__ import annotations

import json
import logging

from ..errors import AuthError
from .oauth import fetch_token, token_expired
from .profiles import ProfileStore, origin_of, resolve

logger = logging.getLogger("qa_screens.auth")

__all__ = ["ProfileStore", "prepare_profile", "context_kwargs", "apply_to_context", "profile_origins"]


def profile_origins(profile: dict, base_url: str | None) -> list[str]:
    origins = [origin_of(o) for o in profile.get("origins") or []]
    if not origins and base_url:
        origins = [origin_of(base_url)]
    return origins


async def prepare_profile(store: ProfileStore, name: str) -> dict:
    """Load a profile, refreshing its OAuth token first if it is expired."""
    profile = store.get(name)
    token = profile.get("token")
    if (profile.get("oauth") or token) and token_expired(token):
        logger.info("Token for profile '%s' missing or expired; fetching a new one", name)
        profile["token"] = await fetch_token(profile)
        store.save(profile)
    return profile


def context_kwargs(store: ProfileStore, profile: dict) -> dict:
    """Options that must be passed to browser.new_context()."""
    kw: dict = {}
    sp = store.storage_state_path(profile["name"])
    if sp.exists():
        kw["storage_state"] = str(sp)
    if profile.get("http_credentials"):
        kw["http_credentials"] = resolve(profile["http_credentials"])
    return kw


def _auth_header(token: dict) -> str:
    ttype = token.get("token_type") or "Bearer"
    if ttype.lower() == "bearer":
        ttype = "Bearer"
    return f"{ttype} {resolve(token['access_token'])}"


async def apply_to_context(context, profile: dict, base_url: str | None) -> None:
    """Install headers, cookies, token and web storage from the profile into a context."""
    origins = profile_origins(profile, base_url)
    headers = dict(resolve(profile.get("headers") or {}))
    local = {o: dict(v) for o, v in resolve(profile.get("local_storage") or {}).items()}
    session = {o: dict(v) for o, v in resolve(profile.get("session_storage") or {}).items()}
    cookies = list(resolve(profile.get("cookies") or []))

    token = profile.get("token")
    if token and token.get("access_token"):
        if not origins:
            raise AuthError("Profile has a token but no origins and no base_url to scope it to")
        access = resolve(token["access_token"])
        for target in profile.get("apply_token_as") or ["header"]:
            kind, _, key = target.partition(":")
            if kind == "header":
                headers[key or "Authorization"] = _auth_header(token) if not key or key == "Authorization" else access
            elif kind in ("local_storage", "local_storage_json", "session_storage", "session_storage_json"):
                if not key:
                    raise AuthError(f"apply_token_as '{target}' needs a key, e.g. 'local_storage:access_token'")
                value = json.dumps(resolve(token)) if kind.endswith("_json") else access
                bucket = local if kind.startswith("local") else session
                for o in origins:
                    bucket.setdefault(o, {})[key] = value
            elif kind == "cookie":
                for o in origins:
                    cookies.append({"name": key or "access_token", "value": access, "url": o + "/"})
            else:
                raise AuthError(f"Unknown apply_token_as target '{target}'")

    if cookies:
        for c in cookies:
            if "url" not in c and "domain" not in c:
                if not origins:
                    raise AuthError(f"Cookie '{c.get('name')}' needs a url or domain")
                c["url"] = origins[0] + "/"
        await context.add_cookies(cookies)

    if local or session:
        data = {o.lower(): {"local": local.get(o, {}), "session": session.get(o, {})} for o in set(local) | set(session)}
        await context.add_init_script(
            "(d => { try { const e = d[location.origin.toLowerCase()]; if (!e) return;"
            " for (const [k, v] of Object.entries(e.local)) localStorage.setItem(k, v);"
            " for (const [k, v] of Object.entries(e.session)) sessionStorage.setItem(k, v);"
            " } catch (_) {} })(" + json.dumps(data) + ")"
        )

    if headers:
        # Only send credentials to the app's own origins, never to CDNs or third parties.
        allowed = set(origins)

        def _match(url: str) -> bool:
            try:
                return origin_of(url) in allowed
            except Exception:
                return False

        async def _add_headers(route):
            await route.continue_(headers={**route.request.headers, **headers})

        await context.route(_match, _add_headers)
