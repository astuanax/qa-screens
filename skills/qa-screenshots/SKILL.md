---
name: qa-screenshots
description: Visual QA with the qa-screens MCP server — compare the running site or app against reference screenshots (SSIM) and fix visual regressions. ALWAYS use after any change that affects rendered output (HTML, CSS, Tailwind, components, layout, tokens, images, icons, fonts), without being asked. Also use for refactors that must stay pixel-identical (capture before/after), for apps behind a login (auth profiles), or when the user asks to check screenshots, visual regressions or Figma references.
---

# Visual QA with qa-screens

The `qa-screens` MCP server captures pages with Playwright and compares them with
reference screenshots. Failing pages come back with changed regions (px boxes) and a
labelled REFERENCE | LIVE preview image you can look at directly.

## 0. Orient

Call `qa_config` once: it shows the base URL, where references live, how reference
names map to URLs, and which auth profiles exist. The site must already be running
at the base URL — if pages come back `ERROR` with HTTP 404 / connection refused, ask
the user to start the dev server (or fix `route_template`/`routes` in `.qa-screens.json`).

## 1. Choose the mode

- **Design conformance** (does the page match the design/golden screenshot?):
  `run_qa` (all pages, or `pages=[...]`, `viewport="mobile"|"desktop"`).
- **Refactor safety** (the change must NOT alter pixels):
  `capture_set(label="before")` *before editing* → make the change → rebuild →
  `capture_set(label="after")` → `compare_sets()`. `identical: true` is the proof.
- **Two servers** (main branch vs worktree): `ab_compare(base_url_a, base_url_b)`.

## 2. Diagnose each failure

For each FAIL: look at the preview image, read `regions` (x/y/width/height in px,
largest first) and `size_mismatch`/`warning`. Identify the cause (spacing, color,
font, missing/extra element, layout shift) and trace it to the source file.
`ERROR` is not a visual failure — it's a 4xx/5xx, a redirect to a login page, or
a missing reference; fix the environment, not the CSS.

## 3. Fix, verify, repeat

Make the minimal source change, then `qa_page(page=...)` for just that page.
Up to 3 attempts per page, then stop and show the user the preview and your
hypothesis. Finish with one full `run_qa` — shared CSS can break neighbours.

If the difference looks **intentional** (the user just asked for this change),
don't "fix" it: tell the user the reference is stale. Only call
`update_reference(page, confirm=true)` after the user confirms.

## Apps behind a login

Create an auth profile once, then pass `profile="<name>"` to any tool:
- Bearer/API token: `auth_update_profile(name, access_token="env:APP_TOKEN", apply_token_as=["header"])`
  (or `local_storage:<key>` / `session_storage:<key>` / `cookie:<name>` — wherever the app reads it).
- OAuth machine login: `auth_update_profile(name, oauth={grant_type:"client_credentials", token_url, client_id, client_secret:"env:..."})` — refreshed automatically.
- OAuth user login (PKCE): configure `oauth` with `grant_type:"authorization_code"`, then `auth_oauth_login(name)`.
- Any login form / SSO / MFA: `auth_browser_login(name, login_url)` — the user signs in in a window; cookies, localStorage and sessionStorage are saved.
- Verify with `auth_check(name)`. A redirect warning means the session expired.
Prefer `env:VAR` for secrets so they never appear in the conversation.

## Don'ts

- Don't update references or lower `threshold` to make a page pass.
- Don't skip the final full run.
- Don't treat an `ERROR` row as a design regression.
