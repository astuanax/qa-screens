"""Project configuration.

Resolution order (later wins): built-in defaults -> `.qa-screens.json` in the
project root -> environment variables -> per-call tool arguments.

The project root is `QA_SCREENS_ROOT` or the server's working directory (MCP
clients such as Claude Code start servers in the project directory).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
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
    "QA_SCREENS_BASE_URL": ("base_url", str),
    "QA_SCREENS_REFERENCES_DIR": ("references_dir", str),
    "QA_SCREENS_RUNTIME_DIR": ("runtime_dir", str),
    "QA_SCREENS_ROUTE_TEMPLATE": ("route_template", str),
    "QA_SCREENS_THRESHOLD": ("threshold", float),
    "QA_SCREENS_PROFILE": ("default_profile", str),
    "QA_SCREENS_AI_CRITIQUE": ("ai_critique", lambda v: v.lower() in ("1", "true", "yes", "on")),
}


def load_config(root: str | os.PathLike | None = None) -> Config:
    root_path = Path(root or os.environ.get("QA_SCREENS_ROOT") or os.getcwd()).resolve()
    cfg = Config(root=root_path)
    cfg_file = root_path / CONFIG_FILENAME
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise QAUserError(f"Invalid JSON in {cfg_file}: {e}") from e
        known = {f.name for f in fields(Config)} - {"root"}
        unknown = set(data) - known
        if unknown:
            raise QAUserError(f"Unknown keys in {cfg_file}: {sorted(unknown)}")
        for k, v in data.items():
            setattr(cfg, k, v)
    for env, (attr, conv) in _ENV_MAP.items():
        if os.environ.get(env):
            setattr(cfg, attr, conv(os.environ[env]))
    return cfg
