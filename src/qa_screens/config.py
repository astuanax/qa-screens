"""Project configuration.

Resolution order (later wins): built-in defaults -> `.qa-screens.json` in the
project root -> environment variables -> per-call tool arguments.

The project root is `QA_SCREENS_ROOT` or the server's working directory (MCP
clients such as Claude Code start servers in the project directory).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import QAUserError

CONFIG_FILENAME = ".qa-screens.json"


def state_dir() -> Path:
    """Per-user state dir for secrets (auth profiles) and crash reports."""
    base = os.environ.get("QA_SCREENS_STATE_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "qa-screens"
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


@dataclass
class Config:
    root: Path = field(default_factory=Path.cwd)
    base_url: str = "http://localhost:8080"
    references_dir: str = "screenshots"
    runtime_dir: str = ".qa-screens/runtime"
    # "{name}" is the reference name with the "-mobile" suffix stripped.
    route_template: str = "/{name}/"
    # Explicit name -> path overrides, e.g. {"home": "/", "nl-be": "/nl-be/"}.
    routes: dict[str, str] = field(default_factory=dict)
    threshold: float = 0.90
    # Also fail when more than this fraction of pixels clearly changed colour. SSIM alone
    # passes a recoloured header (a small share of the page). 1.0 disables the check.
    max_changed_ratio: float = 0.02
    concurrency: int = 3
    mobile_suffix: str = "-mobile"
    desktop_viewport: dict = field(default_factory=lambda: {"width": 1440, "height": 900})
    mobile_viewport: dict = field(default_factory=lambda: {"width": 390, "height": 844})
    # 2 = retina captures (780px wide PNGs). Use 1 if mobile references are 390px wide.
    mobile_device_scale_factor: float = 2
    wait_until: str = "networkidle"
    navigation_timeout_ms: int = 30000
    # CSS selectors hidden before capture (dynamic content: carousels, dates, ads).
    mask_selectors: list[str] = field(default_factory=list)
    default_profile: str | None = None
    ai_critique: bool = False
    gemini_model: str = "gemini-2.5-flash"
    # Problems found while loading (bad values, unknown keys); shown in every tool result.
    warnings: list[str] = field(default_factory=list)

    @property
    def references_path(self) -> Path:
        return (self.root / self.references_dir).resolve()

    @property
    def runtime_path(self) -> Path:
        return (self.root / self.runtime_dir).resolve()

    def is_mobile(self, name: str) -> bool:
        return name.endswith(self.mobile_suffix)

    def base_name(self, name: str) -> str:
        return name[: -len(self.mobile_suffix)] if self.is_mobile(name) else name

    def route_for(self, name: str) -> str:
        base = self.base_name(name)
        if base in self.routes:
            return self.routes[base]
        return self.route_template.format(name=base)

    def url_for(self, name: str, base_url: str | None = None) -> str:
        route = self.route_for(name)
        if route.startswith(("http://", "https://")):
            return route
        return (base_url or self.base_url).rstrip("/") + "/" + route.lstrip("/")

    def reference_file(self, name: str) -> Path:
        for ext in (".png", ".jpg", ".jpeg"):
            p = self.references_path / f"{name}{ext}"
            if p.exists():
                return p
        raise QAUserError(f"Reference screenshot not found for '{name}' in {self.references_path}")

    def list_references(self) -> list[str]:
        if not self.references_path.is_dir():
            return []
        return sorted(
            p.stem for p in self.references_path.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")
        )


_ENV_MAP = {
    "QA_SCREENS_BASE_URL": "base_url",
    "QA_SCREENS_REFERENCES_DIR": "references_dir",
    "QA_SCREENS_RUNTIME_DIR": "runtime_dir",
    "QA_SCREENS_ROUTE_TEMPLATE": "route_template",
    "QA_SCREENS_THRESHOLD": "threshold",
    "QA_SCREENS_MAX_CHANGED_RATIO": "max_changed_ratio",
    "QA_SCREENS_PROFILE": "default_profile",
    "QA_SCREENS_AI_CRITIQUE": "ai_critique",
}


# --------------------------------------------------------------------------- validation
# A bad setting must never make the tool unusable: invalid values are ignored
# (the default is kept) and reported as warnings that every tool result carries.

def _num(lo: float, hi: float):
    def check(v):
        if isinstance(v, str):
            v = float(v)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"must be a number between {lo:g} and {hi:g}")
        return v
    return check


def _int(lo: int, hi: int):
    def check(v):
        if isinstance(v, str) and v.strip().isdigit():
            v = int(v)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"must be a whole number between {lo} and {hi}")
        return v
    return check


def _str(v):
    if not isinstance(v, str) or not v.strip():
        raise ValueError("must be a non-empty string")
    return v


def _url(v):
    v = _str(v)
    if not v.startswith(("http://", "https://")):
        raise ValueError("must start with http:// or https://")
    return v


def _template(v):
    v = _str(v)
    try:
        v.format(name="x")
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"may only use the {{name}} placeholder ({e})") from None
    return v


def _routes(v):
    if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(p, str) for k, p in v.items()):
        raise ValueError('must be an object of page name -> path, e.g. {"home": "/"}')
    return v


def _viewport(v):
    if not (isinstance(v, dict) and all(isinstance(v.get(k), int) and v[k] > 0 for k in ("width", "height"))):
        raise ValueError('must look like {"width": 1440, "height": 900}')
    return {"width": v["width"], "height": v["height"]}


def _str_list(v):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError('must be a list of strings, e.g. [".carousel"]')
    return v


def _optional_str(v):
    return None if v in (None, "") else _str(v)


def _bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    if not isinstance(v, bool):
        raise ValueError("must be true or false")
    return v


def _choice(*options):
    def check(v):
        if v not in options:
            raise ValueError(f"must be one of {', '.join(options)}")
        return v
    return check


_VALIDATORS = {
    "base_url": _url,
    "references_dir": _str,
    "runtime_dir": _str,
    "route_template": _template,
    "routes": _routes,
    "threshold": _num(0, 1),
    "max_changed_ratio": _num(0, 1),
    "concurrency": _int(1, 16),
    "mobile_suffix": _str,
    "desktop_viewport": _viewport,
    "mobile_viewport": _viewport,
    "mobile_device_scale_factor": _num(0.5, 4),
    "wait_until": _choice("load", "domcontentloaded", "networkidle", "commit"),
    "navigation_timeout_ms": _int(1000, 600000),
    "mask_selectors": _str_list,
    "default_profile": _optional_str,
    "ai_critique": _bool,
    "gemini_model": _str,
}


def _apply(cfg: Config, key: str, value, source: str) -> None:
    try:
        setattr(cfg, key, _VALIDATORS[key](value))
    except (ValueError, TypeError) as e:
        cfg.warnings.append(f"{source}: '{key}' {e}; using the default {getattr(cfg, key)!r}")


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


def load_config(root: str | os.PathLike | None = None) -> Config:
    """Load the project configuration. Never raises: problems become `cfg.warnings`."""
    root_path = Path(root or os.environ.get("QA_SCREENS_ROOT") or os.getcwd()).expanduser().resolve()
    cfg = Config(root=root_path)
    cfg_file = root_path / CONFIG_FILENAME
    data = {}
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("the file must contain a JSON object { ... }")
        except (OSError, ValueError) as e:  # JSONDecodeError is a ValueError
            cfg.warnings.append(f"{CONFIG_FILENAME} ignored, using defaults: {e}")
            data = {}
    for key, value in data.items():
        if key in _VALIDATORS:
            _apply(cfg, key, value, CONFIG_FILENAME)
        else:
            hint = difflib.get_close_matches(key, _VALIDATORS, n=1)
            cfg.warnings.append(f"{CONFIG_FILENAME}: unknown key '{key}' ignored"
                                + (f" (did you mean '{hint[0]}'?)" if hint else ""))
    for env, key in _ENV_MAP.items():
        if os.environ.get(env):
            _apply(cfg, key, os.environ[env], env)

    # MCP clients may start the server in / or $HOME: keep outputs somewhere writable.
    if not _writable(cfg.runtime_path):
        fallback = state_dir() / "runtime" / hashlib.sha1(str(root_path).encode()).hexdigest()[:10]
        cfg.warnings.append(f"{cfg.runtime_path} is not writable; saving captures and reports in {fallback}")
        cfg.runtime_dir = str(fallback)
    return cfg
