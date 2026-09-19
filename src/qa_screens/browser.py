from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, async_playwright

from .auth import ProfileStore, apply_to_context, context_kwargs, prepare_profile
from .config import Config
from .errors import QAUserError

logger = logging.getLogger("qa_screens.browser")

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


_install_lock = threading.Lock()


def install_chromium() -> None:
    """Install Playwright's Chromium if missing (idempotent, fast when present).

    Never prints to stdout: that is the MCP channel."""
    with _install_lock:
        logger.info("Checking/installing Playwright Chromium...")
        try:
            r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                               capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise QAUserError(f"Could not run the Chromium installer ({e}). Install it yourself with: "
                              f"{sys.executable} -m playwright install chromium") from None
        if r.returncode != 0:
            tail = " ".join((r.stderr or r.stdout).strip().splitlines()[-3:])[:400]
            raise QAUserError("Could not download Chromium for screenshots (network, proxy or disk problem?): "
                              f"{tail}. Retry, or install it yourself with: {sys.executable} -m playwright install chromium")
        logger.info("Chromium is ready")


def start_background_install() -> None:
    """Fetch Chromium at server start, so the first tool call doesn't wait for a ~150MB download."""
    def run():
        try:
            install_chromium()
        except QAUserError as e:
            logger.warning("%s", e)  # the first capture retries and shows this message

    threading.Thread(target=run, name="qa-screens-chromium-install", daemon=True).start()


def _launch_error(e: Exception) -> QAUserError | None:
    msg = str(e)
    if "missing dependencies" in msg or "error while loading shared libraries" in msg:
        return QAUserError("Chromium is installed but can't start: this machine is missing system libraries. "
                           f"Install them once with: sudo {sys.executable} -m playwright install-deps chromium")
    return None


def has_display() -> bool:
    return sys.platform in ("darwin", "win32") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class BrowserManager:
    """One shared headless Chromium; contexts cached per (auth profile, viewport)."""

    def __init__(self, profiles: ProfileStore | None = None):
        self.profiles = profiles or ProfileStore()
        self._pw = None
        self._browser: Browser | None = None
        self._contexts: dict[tuple, tuple[BrowserContext, float | None]] = {}
        self._lock = asyncio.Lock()

    async def _launch(self, headless: bool = True) -> Browser:
        if self._pw is None:
            self._pw = await async_playwright().start()
        try:
            return await self._pw.chromium.launch(headless=headless)
        except PlaywrightError as e:
            if "Executable doesn't exist" not in str(e):
                raise _launch_error(e) or e
        await asyncio.to_thread(install_chromium)  # waits for a background install in progress
        try:
            return await self._pw.chromium.launch(headless=headless)
        except PlaywrightError as e:
            raise _launch_error(e) or e

    async def browser(self) -> Browser:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                self._browser = await self._launch(headless=True)
                self._contexts.clear()
            return self._browser

    async def launch_login_browser(self, headed: bool) -> Browser:
        """A separate browser for interactive logins; the caller closes it."""
        if headed and not has_display():
            raise QAUserError("A headed browser needs a display (DISPLAY/WAYLAND_DISPLAY). Use headed=false with a scripted login, or run where a display is available.")
        async with self._lock:
            return await self._launch(headless=not headed)

    def viewport_kwargs(self, cfg: Config, mobile: bool) -> dict:
        if mobile:
            return {"viewport": cfg.mobile_viewport, "device_scale_factor": cfg.mobile_device_scale_factor, "user_agent": MOBILE_UA, "has_touch": True, "is_mobile": True}
        return {"viewport": cfg.desktop_viewport, "device_scale_factor": 1}

    async def context(self, cfg: Config, mobile: bool, profile: str | None, base_url: str | None = None) -> BrowserContext:
        key = (profile, mobile, base_url or cfg.base_url, str(cfg.desktop_viewport), str(cfg.mobile_viewport), cfg.mobile_device_scale_factor)
        cached = self._contexts.get(key)
        if cached:
            ctx, expires_at = cached
            if expires_at is None or time.time() < expires_at - 60:
                return ctx
            await ctx.close()  # token about to expire: rebuild with a refreshed one
            self._contexts.pop(key, None)

        browser = await self.browser()
        kw = self.viewport_kwargs(cfg, mobile)
        expires_at = None
        prof = None
        if profile:
            prof = await prepare_profile(self.profiles, profile)
            kw.update(context_kwargs(self.profiles, prof))
            tok = prof.get("token") or {}
            expires_at = tok.get("expires_at")
        ctx = await browser.new_context(**kw)
        if prof:
            await apply_to_context(ctx, prof, base_url or cfg.base_url)
        self._contexts[key] = (ctx, expires_at)
        return ctx

    async def invalidate(self, profile: str | None = None) -> None:
        for key in [k for k in self._contexts if profile is None or k[0] == profile]:
            ctx, _ = self._contexts.pop(key)
            try:
                await ctx.close()
            except Exception:
                pass

    async def stop(self) -> None:
        await self.invalidate()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None


STABILIZE_CSS = """
*, *::before, *::after { animation: none !important; transition: none !important; caret-color: transparent !important; }
::-webkit-scrollbar { display: none; }
html { -ms-overflow-style: none; scrollbar-width: none; }
"""


async def capture_page(
    context: BrowserContext,
    url: str,
    output_path: str,
    *,
    full_page: bool = True,
    wait_until: str = "networkidle",
    timeout_ms: int = 30000,
    wait_for_selector: str | None = None,
    mask_selectors: list[str] | None = None,
    clip_selector: str | None = None,
) -> dict:
    """Navigate and capture a stable screenshot. Returns metadata (status, final URL)."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    page = await context.new_page()
    try:
        # Contexts are reused across calls; without this, the HTTP cache serves the CSS/JS from
        # before the edit being verified and a regression passes as unchanged.
        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        await page.emulate_media(media="screen")
        notes = []
        nav_responses = []  # kept even when goto() times out before returning its response
        page.on("response", lambda r: nav_responses.append(r)
                if r.request.is_navigation_request() and r.frame == page.main_frame else None)
        try:
            resp = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except PlaywrightError as e:
            resp = None
            msg = str(e).splitlines()[0]
            if "ERR_CONNECTION_REFUSED" in msg:
                raise QAUserError(f"Nothing is running at {url}. Start the site/dev server, or set base_url in "
                                  ".qa-screens.json (or pass base_url) to where it runs.") from None
            if "ERR_NAME_NOT_RESOLVED" in msg:
                raise QAUserError(f"Host not found for {url}: check base_url / the URL.") from None
            if "Timeout" in msg and wait_until == "networkidle" and page.url not in ("about:blank", ""):
                # Apps that poll or keep websockets open never go idle: capture once loaded.
                notes.append("the network never went idle (polling/websockets?); captured after 'load'. "
                             'Set "wait_until": "load" in .qa-screens.json to skip the wait.')
                await page.wait_for_load_state("load", timeout=timeout_ms)
            else:
                raise QAUserError(f"Could not load {url}: {msg}") from None
        resp = resp or (nav_responses[-1] if nav_responses else None)
        status = resp.status if resp else None
        if wait_for_selector:
            try:
                await page.wait_for_selector(wait_for_selector, timeout=timeout_ms)
            except PlaywrightError as e:
                raise QAUserError(f"Selector '{wait_for_selector}' did not appear on {url}") from e
        await page.evaluate("document.fonts.ready")
        css = STABILIZE_CSS
        if mask_selectors:
            css += "\n" + ", ".join(mask_selectors) + " { visibility: hidden !important; }"
        await page.add_style_tag(content=css)
        if clip_selector:
            el = await page.query_selector(clip_selector)
            if el is None:
                raise QAUserError(f"clip_selector '{clip_selector}' not found on {url}")
            await el.screenshot(path=output_path)
        else:
            await page.screenshot(path=output_path, full_page=full_page)
        meta = {"url": url, "final_url": page.url, "status": status, "path": output_path}
        if notes:
            meta["warning"] = "; ".join(notes)
        elif status and status >= 400:
            meta["warning"] = f"HTTP {status} — the capture shows an error page"
        elif page.url.split("#")[0].rstrip("/") != url.split("#")[0].rstrip("/"):
            meta["warning"] = f"Redirected to {page.url} (login wall or expired session?)"
        return meta
    finally:
        await page.close()
