"""qa-screens MCP server: visual QA tools for AI-driven refactoring."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import logging.handlers
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, reporting, runner
from .auth import ProfileStore, profile_origins
from .auth.oauth import authorization_code_flow, fetch_token
from .auth.profiles import origin_of, resolve
from .browser import BrowserManager, capture_page, start_background_install
from .config import load_config, state_dir
from .diff import compute_diff, make_preview, verdict
from .errors import AuthError, QAUserError
from .figma import sync_figma as _sync_figma

logger = logging.getLogger("qa_screens.server")

INSTRUCTIONS = """Visual QA for refactoring. Typical loop: run_qa (or capture_set 'before' ->
change code -> capture_set 'after' -> compare_sets) -> inspect the preview images of failing
pages -> fix the source -> re-run qa_page for that page -> finish with a full run_qa.
Never update references or lower thresholds to make a failing page pass unless the user
confirms the new look is intended. For apps behind a login, create an auth profile first
(auth_update_profile / auth_browser_login / auth_oauth_login) and pass `profile`."""

_bm: BrowserManager | None = None
_profiles: ProfileStore | None = None


def profiles() -> ProfileStore:
    global _profiles
    if _profiles is None:
        _profiles = ProfileStore()
    return _profiles


def browsers() -> BrowserManager:
    global _bm
    if _bm is None:
        _bm = BrowserManager(profiles())
    return _bm


@asynccontextmanager
async def lifespan(_app):
    asyncio.get_running_loop().set_exception_handler(reporting.asyncio_exception_handler)
    try:
        yield {}
    finally:
        if _bm is not None:
            await _bm.stop()


mcp = MCPServer("qa-screens", version=__version__, instructions=INSTRUCTIONS, lifespan=lifespan)


def tool(fn):
    """Register a tool; map expected errors to tool errors and auto-report unexpected ones."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return _with_config_warnings(await fn(*args, **kwargs))
        except ToolError:
            raise
        except QAUserError as e:
            warnings = load_config().warnings
            extra = ("\n\nConfig warnings (fix .qa-screens.json):\n- " + "\n- ".join(warnings)) if warnings else ""
            raise ToolError(f"{e}{extra}") from None
        except Exception as e:
            path = reporting.record_exception(e, context={"tool": fn.__name__, "args": reporting.scrub_args(kwargs)})
            note = " An error report was filed for the qa-screens maintainers." if path else ""
            raise ToolError(f"Unexpected {type(e).__name__}: {reporting.scrub(str(e))}.{note}") from e

    return mcp.tool(structured_output=False)(wrapper)


def _with_config_warnings(result):
    """Attach config warnings to a tool result so the agent sees (and can fix) them."""
    warnings = load_config().warnings
    if not warnings:
        return result
    items = result if isinstance(result, list) else [result]
    first = items[0] if items else None
    if isinstance(first, str):
        try:
            data = json.loads(first)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data["config_warnings"] = warnings
            items = [_json(data), *items[1:]]
            return items if isinstance(result, list) else items[0]
    note = _json({"config_warnings": warnings})
    return [*items, note] if isinstance(result, list) else [result, note]


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _thumbnail(path: Path, out_dir: Path) -> Image:
    """A downscaled copy of a (possibly very tall) screenshot, sized for a vision model."""
    from PIL import Image as PILImage

    PILImage.MAX_IMAGE_PIXELS = None
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{path.stem}_thumb.png"
    with PILImage.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((1200, 3000))
        im.save(out)
    return Image(path=str(out))


def _images(paths: list[str | None], limit: int) -> list[Image]:
    return [Image(path=p) for p in paths if p and Path(p).exists()][:limit]


# --------------------------------------------------------------------------- QA

@tool
async def qa_config() -> str:
    """Show the effective configuration (base URL, reference dir, route mapping), the
    reference screenshots found and the available auth profiles. Start here."""
    cfg = load_config()
    refs = cfg.list_references()
    return _json({
        "root": str(cfg.root),
        "base_url": cfg.base_url,
        "references_dir": str(cfg.references_path),
        "runtime_dir": str(cfg.runtime_path),
        "route_template": cfg.route_template,
        "routes": cfg.routes,
        "threshold": cfg.threshold,
        "default_profile": cfg.default_profile,
        "references": [{"name": n, "url": cfg.url_for(n), "viewport": "mobile" if cfg.is_mobile(n) else "desktop"} for n in refs],
        "auth_profiles": profiles().names(),
        "config_file": str(cfg.root / ".qa-screens.json") + ("" if (cfg.root / ".qa-screens.json").exists() else " (not present, using defaults)"),
    })


@tool
async def run_qa(
    pages: list[str] | None = None,
    viewport: str = "all",
    base_url: str | None = None,
    profile: str | None = None,
    threshold: float | None = None,
    align: str = "resize",
    max_images: int = 3,
    max_changed_ratio: float | None = None,
) -> list:
    """Capture pages from the running site and SSIM-compare them with the reference screenshots.

    pages: reference names (file names without extension); default all references.
    viewport: all | desktop | mobile (names ending in "-mobile" use a 390px mobile context).
    profile: auth profile for pages behind a login.
    align: how to handle different image sizes — resize (default, legacy), crop, or pad
      (pad makes page-height changes count as differences).
    A page FAILs when SSIM < threshold OR more than max_changed_ratio of its pixels clearly
    changed colour (catches local changes such as a recoloured header that SSIM averages away).
    Returns a JSON summary (failures first, with changed regions in px) plus side-by-side
    reference|live preview images of up to `max_images` failing pages.
    """
    cfg = load_config()
    summary = await runner.qa_batch(browsers(), cfg, pages=pages, viewport=viewport, base_url=base_url,
                                    profile=profile, threshold=threshold, align=align, max_changed_ratio=max_changed_ratio)
    previews = [r.get("preview_path") for r in summary["pages"] if r["status"] == "FAIL"]
    return [_json(summary), *_images(previews, max_images)]


@tool
async def qa_page(
    page: str,
    url: str | None = None,
    base_url: str | None = None,
    profile: str | None = None,
    threshold: float | None = None,
    align: str = "resize",
    max_changed_ratio: float | None = None,
) -> list:
    """QA a single page against its reference (fast iteration while fixing).
    `url` overrides the route mapping for this call. Returns the result and, on failure, a
    reference|live side-by-side preview cropped around the changed regions."""
    cfg = load_config()
    r = await runner.qa_page(browsers(), cfg, page, base_url=base_url, profile=profile, url=url,
                             threshold=threshold, align=align, max_changed_ratio=max_changed_ratio)
    return [_json(r), *_images([r.get("preview_path")], 1)]


@tool
async def capture(
    url: str,
    name: str | None = None,
    viewport: str = "desktop",
    profile: str | None = None,
    full_page: bool = True,
    wait_for_selector: str | None = None,
    clip_selector: str | None = None,
    return_image: bool = True,
) -> list:
    """Screenshot any URL (desktop or mobile viewport, optionally logged in via `profile`).
    `clip_selector` captures a single element. Saved under runtime/captures/<name>.png."""
    cfg = load_config()
    if viewport not in ("desktop", "mobile"):
        raise QAUserError("viewport must be desktop or mobile")
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", name or url.split("://", 1)[-1]).strip("_")[:80] or "capture"
    out = cfg.runtime_path / "captures" / f"{name}.png"
    # Credentials stay scoped to the app (profile origins / base_url), never to the captured URL.
    ctx = await browsers().context(cfg, viewport == "mobile", profile or cfg.default_profile)
    meta = await capture_page(ctx, url, str(out), full_page=full_page, wait_until=cfg.wait_until,
                              timeout_ms=cfg.navigation_timeout_ms, wait_for_selector=wait_for_selector,
                              mask_selectors=cfg.mask_selectors, clip_selector=clip_selector)
    result: list = [_json(meta)]
    if return_image:
        result.append(_thumbnail(out, out.parent))
    return result


@tool
async def compare_images(image_a: str, image_b: str, threshold: float = 0.95, align: str = "pad",
                         max_changed_ratio: float = 0.02) -> list:
    """SSIM-compare two image files (e.g. a Figma export vs a capture). Paths may be relative
    to the project root. Returns score, changed regions, a heatmap path and an a|b preview."""
    cfg = load_config()
    a, b = (str(p if Path(p).is_absolute() else cfg.root / p) for p in (image_a, image_b))
    for p in (a, b):
        if not Path(p).exists():
            raise QAUserError(f"Image not found: {p}")
    tag = f"{Path(a).stem}__vs__{Path(b).stem}"
    d = await asyncio.to_thread(compute_diff, a, b, str(cfg.runtime_path / "diff" / f"{tag}_diff.png"), None, align)
    d["status"] = "PASS" if verdict(d, threshold, max_changed_ratio) else "FAIL"
    preview = await asyncio.to_thread(make_preview, a, b, d["regions"], str(cfg.runtime_path / "diff" / f"{tag}_preview.png"))
    return [_json(d), Image(path=preview)]


@tool
async def capture_set(
    label: str,
    pages: list[str] | None = None,
    viewport: str = "all",
    base_url: str | None = None,
    profile: str | None = None,
) -> str:
    """Capture a named set of pages (e.g. label="before" before a refactor, "after" after it).
    Pages default to all reference names; any names work if you pass `pages` and the route
    mapping resolves them. Use compare_sets afterwards. Proves a refactor is pixel-neutral
    without depending on golden references."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
        raise QAUserError("label may only contain letters, digits, . _ -")
    return _json(await runner.capture_set(browsers(), load_config(), label, pages=pages, viewport=viewport,
                                          base_url=base_url, profile=profile))


@tool
async def compare_sets(before: str = "before", after: str = "after", threshold: float = 0.99,
                       align: str = "crop", max_images: int = 3, max_changed_ratio: float = 0.001) -> list:
    """Diff two capture sets (labels from capture_set, or directories). `identical: true`
    means every page scored >= threshold. Returns previews of the most-changed pages."""
    r = await asyncio.to_thread(runner.compare_sets, load_config(), before, after, threshold, align, max_changed_ratio)
    return [_json(r), *_images([c.get("preview_path") for c in r["changed"]], max_images)]


@tool
async def ab_compare(
    base_url_a: str,
    base_url_b: str,
    pages: list[str] | None = None,
    viewport: str = "all",
    profile_a: str | None = None,
    profile_b: str | None = None,
    threshold: float = 0.999,
    max_images: int = 3,
    max_changed_ratio: float = 0.001,
) -> list:
    """Compare two running servers (e.g. main branch on :8080 vs a worktree on :8091) page by page."""
    cfg, bm = load_config(), browsers()
    a = await runner.capture_set(bm, cfg, "ab_a", pages=pages, viewport=viewport, base_url=base_url_a, profile=profile_a)
    b = await runner.capture_set(bm, cfg, "ab_b", pages=pages, viewport=viewport, base_url=base_url_b, profile=profile_b)
    r = await asyncio.to_thread(runner.compare_sets, cfg, "ab_a", "ab_b", threshold, "crop", max_changed_ratio)
    r["capture_failures"] = {"a": a["failed"], "b": b["failed"]}
    return [_json(r), *_images([c.get("preview_path") for c in r["changed"]], max_images)]


@tool
async def update_reference(page: str, confirm: bool = False, source: str | None = None,
                           profile: str | None = None) -> list:
    """Make the current look of a page its reference screenshot: from `source` (an image),
    else the latest qa_page capture, else a fresh capture right now (so this also creates a
    first baseline for a page that has no reference yet). The old reference is backed up.
    Only do this when the USER has confirmed the look is intended; set confirm=true."""
    if not confirm:
        raise QAUserError("Refusing: pass confirm=true only after the user confirmed this look is intended")
    if not re.fullmatch(r"[A-Za-z0-9_. -]{1,120}", page) or ".." in page:
        raise QAUserError("page must be a plain name such as 'home' or 'home-mobile'")
    cfg = load_config()
    if source:
        src = Path(source) if Path(source).is_absolute() else cfg.root / source
        if not src.exists():
            raise QAUserError(f"Image not found: {src}")
    else:
        src = cfg.runtime_path / "live" / f"{page}.png"
        if not src.exists():
            meta = await runner.capture_named(browsers(), cfg, page, src, profile=profile)
            if meta["status"] and meta["status"] >= 400:
                src.unlink(missing_ok=True)
                raise QAUserError(f"{meta['url']} returned HTTP {meta['status']}; not saving an error page as the "
                                  "reference. Check the route mapping (qa_config) or that the page exists.")
    dest = cfg.references_path / f"{page}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        backup = cfg.runtime_path / "reference_backups" / f"{page}_{int(time.time())}.png"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(dest.read_bytes())
    dest.write_bytes(src.read_bytes())
    return [_json({"updated": str(dest), "from": str(src)}), _thumbnail(dest, cfg.runtime_path / "thumbs")]


@tool
async def sync_figma(file_key: str, frame: str | None = None) -> str:
    """Download Figma frames into the references directory (needs FIGMA_TOKEN). Frame names
    become reference names; overwrites existing references with the same name."""
    cfg = load_config()
    names = await asyncio.to_thread(_sync_figma, file_key, cfg.references_path, frame)
    return _json({"synced": names, "directory": str(cfg.references_path)})


# --------------------------------------------------------------------------- auth

@tool
async def auth_profiles(name: str | None = None) -> str:
    """List auth profiles, or show one (secrets are never shown)."""
    store = profiles()
    if name:
        return _json(store.summary(name))
    return _json([store.summary(n) for n in store.names()])


@tool
async def auth_update_profile(
    name: str,
    origins: list[str] | None = None,
    access_token: str | None = None,
    token_type: str | None = None,
    expires_in: int | None = None,
    refresh_token: str | None = None,
    apply_token_as: list[str] | None = None,
    oauth: dict | None = None,
    headers: dict[str, str] | None = None,
    cookies: list[dict] | None = None,
    local_storage: dict[str, dict[str, str]] | None = None,
    session_storage: dict[str, dict[str, str]] | None = None,
    http_credentials: dict[str, str] | None = None,
    clear: list[str] | None = None,
) -> str:
    """Create or update an auth profile. Only the fields you pass change (dicts are merged).

    Any secret may be given as "env:VAR_NAME" so it is read from the server's environment
    instead of passing through the conversation — prefer that.

    origins: origins that receive headers/tokens, e.g. ["https://app.example.com"]
      (default: the base_url's origin). Credentials are never sent to other origins.
    access_token/token_type/expires_in/refresh_token: a bearer token you already have.
    apply_token_as: where the app expects the token — any of "header" (Authorization: Bearer),
      "header:X-Api-Key", "local_storage:<key>", "session_storage:<key>",
      "local_storage_json:<key>" (whole token object as JSON), "cookie:<name>".
    oauth: {grant_type: client_credentials|password|refresh_token|authorization_code,
      token_url, client_id, client_secret, scope, audience, username, password,
      authorize_url, redirect_uri, client_auth: post|basic, extra_params}. For
      client_credentials/password a token is fetched immediately and refreshed automatically.
      For authorization_code, call auth_oauth_login next.
    cookies: Playwright cookies [{name, value, url} or {name, value, domain, path}].
    local_storage/session_storage: {origin: {key: value}} injected before page scripts run.
    http_credentials: {username, password} for HTTP basic auth.
    clear: field names to remove, e.g. ["token", "cookies", "storage_state"].
    """
    store = profiles()
    prof = store.get_or_new(name)
    for field in clear or []:
        if field == "storage_state":
            store.storage_state_path(name).unlink(missing_ok=True)
        else:
            prof.pop(field, None)
    if origins is not None:
        prof["origins"] = [origin_of(o) for o in origins]
    if access_token is not None:
        prof["token"] = {"access_token": access_token, "token_type": token_type or "Bearer"}
        if expires_in:
            prof["token"]["expires_at"] = time.time() + expires_in
        if refresh_token:
            prof["token"]["refresh_token"] = refresh_token
    if apply_token_as is not None:
        prof["apply_token_as"] = apply_token_as
    if oauth is not None:
        prof["oauth"] = {**prof.get("oauth", {}), **oauth}
    for key, val in (("headers", headers), ("http_credentials", http_credentials)):
        if val is not None:
            prof[key] = {**prof.get(key, {}), **val}
    for key, val in (("local_storage", local_storage), ("session_storage", session_storage)):
        if val is not None:
            merged = prof.get(key, {})
            for o, kv in val.items():
                merged[origin_of(o)] = {**merged.get(origin_of(o), {}), **kv}
            prof[key] = merged
    if cookies is not None:
        by_key = {(c["name"], c.get("domain"), c.get("url")): c for c in prof.get("cookies", [])}
        by_key.update({(c["name"], c.get("domain"), c.get("url")): c for c in cookies})
        prof["cookies"] = list(by_key.values())

    fetched = False
    grant = (prof.get("oauth") or {}).get("grant_type")
    if grant in ("client_credentials", "password") and (oauth is not None or not prof.get("token")):
        prof["token"] = await fetch_token(prof)
        fetched = True
    store.save(prof)
    await browsers().invalidate(name)
    return _json({"saved": store.summary(name), "token_fetched": fetched})


@tool
async def auth_browser_login(
    name: str,
    login_url: str,
    headed: bool = True,
    username: str | None = None,
    password: str | None = None,
    username_selector: str = "input[type=email], input[name=username], input[name=email], input[autocomplete=username], input[type=text]",
    password_selector: str = "input[type=password]",
    submit_selector: str = "button[type=submit], input[type=submit]",
    success_url_pattern: str | None = None,
    success_selector: str | None = None,
    timeout_s: int = 300,
) -> str:
    """Log in with a real browser and save the session (cookies, localStorage and
    sessionStorage) into profile `name`. Works for any login incl. SSO/MFA/OAuth redirects.

    headed=true opens a visible window where the user logs in (or finishes MFA); if
    username/password are given (use "env:VAR" for secrets) the form is filled first.
    Completion is detected by `success_url_pattern` (regex on the URL), `success_selector`,
    or — if neither is given — by leaving the login page's URL."""
    store = profiles()
    cfg = load_config()
    bm = browsers()
    browser = await bm.launch_login_browser(headed)
    try:
        ctx = await browser.new_context(viewport=cfg.desktop_viewport)
        page = await ctx.new_page()
        await page.goto(login_url, wait_until="domcontentloaded")
        if username is not None:
            await page.locator(username_selector).first.fill(resolve(username), timeout=15000)
        if password is not None:
            await page.locator(password_selector).first.fill(resolve(password), timeout=15000)
        if username is not None or password is not None:
            await page.locator(submit_selector).first.click(timeout=15000)
        timeout_ms = timeout_s * 1000
        try:
            if success_url_pattern:
                await page.wait_for_url(re.compile(success_url_pattern), timeout=timeout_ms)
            elif success_selector:
                await page.wait_for_selector(success_selector, timeout=timeout_ms)
            else:
                start = login_url.split("?")[0].split("#")[0]
                await page.wait_for_url(lambda u: not u.split("?")[0].split("#")[0].startswith(start), timeout=timeout_ms)
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception as e:
            if "Timeout" in type(e).__name__ or "Timeout" in str(e):
                raise AuthError(f"Login not completed within {timeout_s}s (still at {page.url})") from None
            raise
        state = await ctx.storage_state()
        session = json.loads(await page.evaluate("JSON.stringify(Object.assign({}, sessionStorage))"))
        app_origin = origin_of(page.url)
    finally:
        await browser.close()

    prof = store.get_or_new(name)
    store.save_storage_state(name, state)
    if session:
        prof.setdefault("session_storage", {})[app_origin] = session
    if not prof.get("origins"):
        prof["origins"] = [app_origin]
    store.save(prof)
    await bm.invalidate(name)
    return _json({"saved": store.summary(name), "landed_on": app_origin})


@tool
async def auth_oauth_login(name: str, headed: bool = True, timeout_s: int = 300) -> str:
    """Run the OAuth authorization-code + PKCE flow for profile `name` (configure oauth with
    grant_type "authorization_code", authorize_url, token_url, client_id, redirect_uri first).
    The user signs in in the opened window; the redirect is intercepted, the code exchanged,
    and the token (with refresh_token if issued) stored and auto-refreshed."""
    store = profiles()
    prof = store.get(name)
    oauth = prof.get("oauth") or {}
    if oauth.get("grant_type", "authorization_code") != "authorization_code":
        raise AuthError(f"Profile '{name}' uses grant_type {oauth.get('grant_type')}; auth_update_profile fetches those automatically")
    bm = browsers()
    browser = await bm.launch_login_browser(headed)
    try:
        ctx = await browser.new_context()
        prof["token"] = await authorization_code_flow(ctx, oauth, timeout_s)
    finally:
        await browser.close()
    store.save(prof)
    await bm.invalidate(name)
    return _json(store.summary(name))


@tool
async def auth_import_storage_state(name: str, path: str) -> str:
    """Import a Playwright storage-state JSON (cookies + localStorage), e.g. from a Playwright
    test setup's `storageState` output, into profile `name`."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = load_config().root / p
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise QAUserError(f"Cannot read storage state {p}: {e}") from None
    if not isinstance(state, dict) or "cookies" not in state:
        raise QAUserError("Not a Playwright storage state (expected {cookies: [...], origins: [...]})")
    store = profiles()
    prof = store.get_or_new(name)
    store.save_storage_state(name, state)
    store.save(prof)
    await browsers().invalidate(name)
    return _json(store.summary(name))


@tool
async def auth_check(name: str, url: str | None = None) -> list:
    """Open `url` (default: the base URL) with profile `name` and report whether the session
    works: HTTP status, final URL (a redirect to a login page means it expired) and a screenshot."""
    cfg = load_config()
    store = profiles()
    prof = store.get(name)
    target = url or (profile_origins(prof, cfg.base_url) or [cfg.base_url])[0]
    out = cfg.runtime_path / "captures" / f"auth_check_{name}.png"
    ctx = await browsers().context(cfg, False, name)
    meta = await capture_page(ctx, target, str(out), full_page=False, wait_until="load", timeout_ms=cfg.navigation_timeout_ms)
    meta["authenticated_guess"] = not meta.get("warning")
    meta["profile"] = store.summary(name)
    return [_json(meta), Image(path=str(out))]


@tool
async def auth_delete_profile(name: str) -> str:
    """Delete an auth profile and its saved session."""
    deleted = profiles().delete(name)
    await browsers().invalidate(name)
    return _json({"deleted": deleted, "name": name})


# --------------------------------------------------------------------------- error reporting

@tool
async def error_reports(flush: bool = False) -> str:
    """Show automatic error-reporting status (pending/reported crash and error reports,
    target repo). flush=true posts pending reports to GitHub now."""
    import os

    base = state_dir() / "reports"
    posted = await asyncio.to_thread(reporting.flush_pending) if flush else []
    pending = sorted(p.name for p in (base / "pending").glob("*.json")) if (base / "pending").exists() else []
    reported = sorted((base / "reported").glob("*.json")) if (base / "reported").exists() else []
    return _json({
        "mode": reporting.mode(),
        "repo": os.environ.get("QA_SCREENS_ISSUE_REPO", reporting.DEFAULT_REPO),
        "github_token_available": reporting._token() is not None,
        "pending": pending,
        "reported_count": len(reported),
        "posted_now": posted,
        "directory": str(base),
    })


# --------------------------------------------------------------------------- entry points

def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stderr = logging.StreamHandler(sys.stderr)  # stdout is the MCP protocol channel
    stderr.setFormatter(fmt)
    log_dir = state_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    file = logging.handlers.RotatingFileHandler(log_dir / "server.log", maxBytes=2_000_000, backupCount=3)
    file.setFormatter(fmt)
    root.handlers = [stderr, file]


def serve() -> None:
    setup_logging()
    reporting.install_crash_handlers()
    reporting.startup_scan(background=True)
    start_background_install()
    logger.info("qa-screens %s starting (stdio)", __version__)
    mcp.run("stdio")
