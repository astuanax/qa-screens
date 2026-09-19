"""QA workflows shared by the MCP server and the CLI."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from . import reporting
from .analyzer import generate_critique
from .browser import BrowserManager, capture_page
from .config import Config
from .diff import compute_diff, make_preview, verdict
from .errors import QAUserError

logger = logging.getLogger("qa_screens.runner")


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


async def _guard(coro, name: str, context: dict) -> dict:
    """Per-page isolation: expected failures become ERROR rows, unexpected ones are also reported."""
    try:
        return await coro
    except QAUserError as e:
        return {"page": name, "status": "ERROR", "error": str(e)}
    except Exception as e:
        reporting.record_exception(e, context={**context, "page": name})
        return {"page": name, "status": "ERROR", "error": f"{type(e).__name__}: {e} (reported automatically)"}


async def capture_named(bm: BrowserManager, cfg: Config, name: str, out_path: Path, *, base_url: str | None = None,
                        profile: str | None = None, url: str | None = None) -> dict:
    mobile = cfg.is_mobile(name)
    ctx = await bm.context(cfg, mobile, profile or cfg.default_profile, base_url)
    return await capture_page(
        ctx, url or cfg.url_for(name, base_url), str(out_path),
        wait_until=cfg.wait_until, timeout_ms=cfg.navigation_timeout_ms, mask_selectors=cfg.mask_selectors,
    )


async def qa_page(bm: BrowserManager, cfg: Config, name: str, *, base_url: str | None = None, profile: str | None = None,
                  url: str | None = None, threshold: float | None = None, align: str = "resize", preview: bool = True,
                  max_changed_ratio: float | None = None) -> dict:
    threshold = cfg.threshold if threshold is None else threshold
    max_changed_ratio = cfg.max_changed_ratio if max_changed_ratio is None else max_changed_ratio
    ref = cfg.reference_file(name)
    rt = cfg.runtime_path
    live = rt / "live" / f"{name}.png"
    meta = await capture_named(bm, cfg, name, live, base_url=base_url, profile=profile, url=url)
    diff = await asyncio.to_thread(
        compute_diff, str(ref), str(live), str(rt / "diff" / f"{name}_diff.png"), str(rt / "diff" / f"{name}_mask.png"), align
    )
    passed = verdict(diff, threshold, max_changed_ratio)
    # An error page makes the score meaningless: don't let the AI "fix" CSS against a 404.
    http_error = bool(meta["status"] and meta["status"] >= 400)
    result = {
        "page": name,
        "status": "ERROR" if http_error else ("PASS" if passed else "FAIL"),
        "threshold": threshold,
        "max_changed_ratio": max_changed_ratio,
        **diff,
        "reference_path": str(ref),
        "live_path": str(live),
        "url": meta["url"],
        "http_status": meta["status"],
    }
    if meta.get("warning"):
        result["warning"] = meta["warning"]
    elif diff["reference_size"][0] != diff["live_size"][0]:
        result["warning"] = (f"Width differs (reference {diff['reference_size'][0]}px, live {diff['live_size'][0]}px); "
                             "check viewport / mobile_device_scale_factor in .qa-screens.json")
    if http_error:
        result["error"] = f"{meta['url']} returned HTTP {meta['status']} — check the route mapping or that the page is built"
        return result
    if not passed and preview:
        result["preview_path"] = await asyncio.to_thread(
            make_preview, str(ref), str(live), diff["regions"], str(rt / "diff" / f"{name}_preview.png")
        )
    if not passed and cfg.ai_critique:
        issues = await asyncio.to_thread(generate_critique, str(ref), str(live), cfg.gemini_model)
        if issues is not None:
            result["ai_issues"] = issues
    return result


def select_pages(cfg: Config, pages: list[str] | None, viewport: str) -> list[str]:
    names = pages or cfg.list_references()
    if viewport == "mobile":
        names = [n for n in names if cfg.is_mobile(n)]
    elif viewport == "desktop":
        names = [n for n in names if not cfg.is_mobile(n)]
    elif viewport != "all":
        raise QAUserError("viewport must be all | desktop | mobile")
    return names


async def qa_batch(bm: BrowserManager, cfg: Config, *, pages: list[str] | None = None, viewport: str = "all",
                   base_url: str | None = None, profile: str | None = None, threshold: float | None = None,
                   align: str = "resize", preview: bool = True, max_changed_ratio: float | None = None) -> dict:
    names = select_pages(cfg, pages, viewport)
    if not names:
        where = cfg.references_path
        found = "no" if not cfg.list_references() else f"no {viewport}"
        raise QAUserError(
            f"There are {found} reference screenshots in {where}. To get some: (1) add PNG/JPG files named "
            "after pages (home.png, home-mobile.png), (2) sync_figma, or (3) update_reference(page=..., "
            "confirm=true) to use the current site as the baseline. For refactors you can skip references: "
            "capture_set before/after + compare_sets. If they live elsewhere, set references_dir in .qa-screens.json.")
    sem = asyncio.Semaphore(cfg.concurrency)

    async def one(n):
        async with sem:
            return await _guard(
                qa_page(bm, cfg, n, base_url=base_url, profile=profile, threshold=threshold, align=align, preview=preview,
                        max_changed_ratio=max_changed_ratio),
                n, {"tool": "run_qa"},
            )

    results = sorted(await asyncio.gather(*(one(n) for n in names)), key=lambda r: (r["status"] == "PASS", r.get("score", 0)))
    summary = {
        "run_id": _ts(),
        "base_url": base_url or cfg.base_url,
        "profile": profile or cfg.default_profile,
        "total": len(results),
        "passed": sum(r["status"] == "PASS" for r in results),
        "failed": sum(r["status"] == "FAIL" for r in results),
        "errors": sum(r["status"] == "ERROR" for r in results),
        "pages": results,
    }
    report = cfg.runtime_path / "reports" / f"run_{summary['run_id']}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (report.parent / "latest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["report_path"] = str(report)
    return summary


async def capture_set(bm: BrowserManager, cfg: Config, label: str, *, pages: list[str] | None = None, viewport: str = "all",
                      base_url: str | None = None, profile: str | None = None) -> dict:
    """Capture every page into runtime/sets/<label>/ (the BEFORE or AFTER side of a parity check)."""
    names = select_pages(cfg, pages, viewport)
    if not names:
        raise QAUserError("No pages to capture: pass `pages` or add reference screenshots")
    out = cfg.runtime_path / "sets" / label
    sem = asyncio.Semaphore(cfg.concurrency)

    async def one(n):
        async with sem:
            async def cap():
                meta = await capture_named(bm, cfg, n, out / f"{n}.png", base_url=base_url, profile=profile)
                return {"page": n, "status": "OK", **({"warning": meta["warning"]} if meta.get("warning") else {})}
            return await _guard(cap(), n, {"tool": "capture_set"})

    rows = await asyncio.gather(*(one(n) for n in names))
    return {"label": label, "directory": str(out), "captured": sum(r["status"] == "OK" for r in rows),
            "failed": [r for r in rows if r["status"] != "OK"], "warnings": [r for r in rows if r.get("warning")]}


def compare_sets(cfg: Config, before: str, after: str, threshold: float = 0.99, align: str = "crop",
                 max_changed_ratio: float = 0.001) -> dict:
    """SSIM-diff two capture sets page by page. A label or a directory path is accepted for each side."""
    def resolve(x: str) -> Path:
        p = Path(x)
        return p if p.is_absolute() or p.exists() else cfg.runtime_path / "sets" / x

    b_dir, a_dir = resolve(before), resolve(after)
    for d in (b_dir, a_dir):
        if not d.is_dir():
            raise QAUserError(f"Capture set not found: {d}")
    b_names = {p.stem for p in b_dir.glob("*.png")}
    a_names = {p.stem for p in a_dir.glob("*.png")}
    diff_dir = cfg.runtime_path / "diff" / f"{b_dir.name}__vs__{a_dir.name}"
    rows = []
    for n in sorted(b_names & a_names):
        d = compute_diff(str(b_dir / f"{n}.png"), str(a_dir / f"{n}.png"), str(diff_dir / f"{n}_diff.png"), None, align)
        row = {"page": n, "score": d["score"], "changed_ratio": d["changed_ratio"],
               "changed": not verdict(d, threshold, max_changed_ratio), "size_mismatch": d["size_mismatch"]}
        if row["changed"]:
            row["diff_path"] = d["diff_path"]
            row["regions"] = d["regions"][:5]
            row["preview_path"] = make_preview(str(b_dir / f"{n}.png"), str(a_dir / f"{n}.png"), d["regions"],
                                               str(diff_dir / f"{n}_preview.png"), labels=("BEFORE", "AFTER"))
        rows.append(row)
    rows.sort(key=lambda r: r["score"])
    return {
        "threshold": threshold,
        "max_changed_ratio": max_changed_ratio,
        "compared": len(rows),
        "identical": not any(r["changed"] for r in rows) and bool(rows),
        "changed": [r for r in rows if r["changed"]],
        "unchanged_worst_score": min((r["score"] for r in rows if not r["changed"]), default=None),
        "only_before": sorted(b_names - a_names),
        "only_after": sorted(a_names - b_names),
    }
