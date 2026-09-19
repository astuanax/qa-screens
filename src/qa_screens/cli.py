"""Command line: `qa-screens` (MCP server over stdio) plus CI-friendly subcommands."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__


def _run(args) -> int:
    from . import reporting
    from .browser import BrowserManager
    from .config import load_config
    from .errors import QAUserError
    from .runner import qa_batch
    from .server import setup_logging

    setup_logging()
    reporting.install_crash_handlers()
    reporting.startup_scan(background=False)

    async def go():
        cfg = load_config(args.root)
        for w in cfg.warnings:
            print(f"warning: {w}", file=sys.stderr)
        if args.concurrency:
            cfg.concurrency = args.concurrency
        bm = BrowserManager()
        try:
            return await qa_batch(bm, cfg, pages=args.pages or None, viewport=args.viewport, base_url=args.base_url,
                                  profile=args.profile, threshold=args.threshold, align=args.align, preview=True,
                                  max_changed_ratio=args.max_changed_ratio)
        finally:
            await bm.stop()

    try:
        summary = asyncio.run(go())
    except QAUserError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        reporting.record_exception(e, kind="error", context={"command": "run"}, post=False)
        reporting.flush_pending()
        raise
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for r in summary["pages"]:
            score = f"{r['score']:.4f}" if "score" in r else "  -   "
            extra = r.get("error") or r.get("warning") or ""
            print(f"  {r['status']:5s} {score}  {r['page']}  {extra}")
        print(f"\n{summary['passed']}/{summary['total']} passed, {summary['failed']} failed, "
              f"{summary['errors']} errors — report: {summary['report_path']}")
    return 0 if summary["failed"] == 0 and summary["errors"] == 0 else 1


def _reports(args) -> int:
    from . import reporting
    from .server import setup_logging

    setup_logging()
    if args.flush:
        reporting.collect_crashes()
        for url in reporting.flush_pending():
            print(url)
    base = reporting.state_dir() / "reports"
    pending = list((base / "pending").glob("*.json")) if (base / "pending").exists() else []
    print(f"mode={reporting.mode()} pending={len(pending)} dir={base}")
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="qa-screens", description="Visual QA MCP server and CLI")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")

    r = sub.add_parser("run", help="run visual QA against the reference screenshots (exit 1 on failures)")
    r.add_argument("pages", nargs="*", help="reference names (default: all)")
    r.add_argument("--root", help="project root (default: cwd or QA_SCREENS_ROOT)")
    r.add_argument("--base-url")
    r.add_argument("--profile", help="auth profile")
    r.add_argument("--threshold", type=float)
    r.add_argument("--max-changed-ratio", type=float, help="fail if more than this fraction of pixels changed (default 0.02)")
    r.add_argument("--viewport", choices=["all", "desktop", "mobile"], default="all")
    r.add_argument("--align", choices=["resize", "crop", "pad"], default="resize")
    r.add_argument("--concurrency", type=int)
    r.add_argument("--json", action="store_true", help="print the full JSON summary")

    rep = sub.add_parser("reports", help="show / post pending automatic error reports")
    rep.add_argument("--flush", action="store_true", help="post pending reports to GitHub now")

    args = p.parse_args(argv)
    if args.cmd in (None, "serve", "run"):
        # Before the heavy imports (Playwright, OpenCV, MCP): a crash while loading them counts too.
        from . import reporting

        reporting.install_crash_handlers()
    if args.cmd in (None, "serve"):
        from .server import serve

        serve()
    elif args.cmd == "run":
        sys.exit(_run(args))
    elif args.cmd == "reports":
        sys.exit(_reports(args))


if __name__ == "__main__":
    main()
