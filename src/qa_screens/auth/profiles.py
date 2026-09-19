"""Auth profiles: named, persisted sessions applied to every browser context.

A profile can combine any of:
  - storage_state : Playwright cookies + localStorage snapshot (from a browser login)
  - cookies       : explicit cookies to add
  - headers       : extra request headers (only sent to the profile's origins)
  - token         : an OAuth/bearer token, applied per `apply_token_as`
  - oauth         : how to obtain/refresh `token` (see oauth.py)
  - local_storage / session_storage : {origin: {key: value}} injected before page scripts run
  - http_credentials : HTTP basic auth

Any string value may be written as "env:VAR_NAME" so secrets never have to pass
through the AI conversation; it is resolved when the profile is used.

Profiles live in <state>/profiles/ with 0600 permissions — outside the project,
so they are never committed.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import state_dir
from ..errors import AuthError, QAUserError

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def resolve(value: Any) -> Any:
    """Resolve "env:VAR" references recursively."""
    if isinstance(value, str) and value.startswith("env:"):
        var = value[4:]
        if var not in os.environ:
            raise AuthError(f"Environment variable {var} (referenced by the auth profile) is not set")
        return os.environ[var]
    if isinstance(value, dict):
        return {k: resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v) for v in value]
    return value


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise QAUserError(f"Not an absolute URL: {url}")
    return f"{parts.scheme}://{parts.netloc}".lower()


class ProfileStore:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or (state_dir() / "profiles")
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, name: str) -> Path:
        if not _NAME_RE.match(name or ""):
            raise QAUserError(f"Invalid profile name '{name}' (letters, digits, . _ - only)")
        return self.dir / f"{name}.json"

    def storage_state_path(self, name: str) -> Path:
        return self._path(name).with_suffix(".storage.json")

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def get(self, name: str) -> dict:
        p = self._path(name)
        if not p.exists():
            raise QAUserError(f"Auth profile '{name}' does not exist. Available: {self.names() or 'none'}")
        return json.loads(p.read_text(encoding="utf-8"))

    def get_or_new(self, name: str) -> dict:
        return self.get(name) if self.exists(name) else {"name": name, "created_at": time.time()}

    def save(self, profile: dict) -> None:
        p = self._path(profile["name"])
        profile["updated_at"] = time.time()
        _write_private(p, json.dumps(profile, indent=2))

    def save_storage_state(self, name: str, state: dict) -> Path:
        p = self.storage_state_path(name)
        _write_private(p, json.dumps(state, indent=2))
        return p

    def delete(self, name: str) -> bool:
        found = False
        for p in (self._path(name), self.storage_state_path(name)):
            if p.exists():
                p.unlink()
                found = True
        return found

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json") if not p.name.endswith(".storage.json"))

    def summary(self, name: str) -> dict:
        """Non-secret description of a profile, safe to show to the AI."""
        prof = self.get(name)
        token = prof.get("token") or {}
        sp = self.storage_state_path(name)
        cookies = []
        if sp.exists():
            try:
                cookies = json.loads(sp.read_text(encoding="utf-8")).get("cookies", [])
            except Exception:
                pass
        exp = token.get("expires_at")
        return {
            "name": name,
            "origins": prof.get("origins", []),
            "storage_state": sp.exists(),
            "cookies": sorted({f"{c.get('name')}@{c.get('domain')}" for c in cookies + prof.get("cookies", [])}),
            "headers": sorted(prof.get("headers", {})),
            "token": bool(token.get("access_token")),
            "token_expires_in_s": int(exp - time.time()) if exp else None,
            "refreshable": bool(token.get("refresh_token") or (prof.get("oauth") or {}).get("grant_type") in ("client_credentials", "password")),
            "oauth_grant": (prof.get("oauth") or {}).get("grant_type"),
            "apply_token_as": prof.get("apply_token_as", ["header"]),
            "local_storage_keys": {o: sorted(v) for o, v in prof.get("local_storage", {}).items()},
            "session_storage_keys": {o: sorted(v) for o, v in prof.get("session_storage", {}).items()},
            "http_credentials": bool(prof.get("http_credentials")),
        }


def _write_private(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
