# qa-screens

An MCP server for **visual QA during AI refactoring**. It captures pages with
Playwright, compares them with reference screenshots using SSIM, and returns
the score, the changed regions and a labelled *REFERENCE | LIVE* preview image,
so the agent can see what broke and fix it.

- **Design conformance**: compare the running site with golden or Figma screenshots.
- **Refactor safety**: capture *before* and *after* a change and prove it is pixel-neutral.
- **A/B**: compare two running servers, such as `main` and a worktree.
- **Apps behind a login**: OAuth (client credentials, password, refresh, authorization code + PKCE),
  bearer tokens, cookies, sessions, localStorage/sessionStorage and HTTP basic auth.
- **Self-reporting**: significant errors and crashes are filed as GitHub issues automatically,
  with secrets scrubbed. See [Error reporting](#error-reporting).

## Install

```bash
pip install qa-screens          # or: uv tool install qa-screens
```

Chromium is downloaded automatically on first use. To do it up front, run
`python -m playwright install chromium`.

### Claude Code

```bash
claude mcp add qa-screens -- uvx qa-screens
```

or in a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "qa-screens": { "command": "uvx", "args": ["qa-screens"] }
  }
}
```

To run the latest `main` without PyPI, use `uvx --from git+https://github.com/astuanax/qa-screens qa-screens`.

Copy [`skills/qa-screenshots`](skills/qa-screenshots/SKILL.md) into `.claude/skills/`
so the agent runs QA on its own after every UI change.

### Other MCP clients

The server speaks MCP over stdio. The command is `qa-screens` (or `python -m qa_screens`),
and it runs in the project directory, or wherever `QA_SCREENS_ROOT` points.

## Configure

Put a `.qa-screens.json` in the project root. Every key is optional:

```json
{
  "base_url": "http://localhost:8080",
  "references_dir": "qa/screenshots",
  "route_template": "/nl-be/{name}/",
  "routes": { "home": "/", "nl-be": "/nl-be/" },
  "threshold": 0.9,
  "mobile_device_scale_factor": 1,
  "mask_selectors": [".carousel", "[data-testid=clock]"],
  "default_profile": null
}
```

| key | default | meaning |
|---|---|---|
| `base_url` | `http://localhost:8080` | where the site runs (not started by qa-screens) |
| `references_dir` | `screenshots` | reference `.png`/`.jpg` files; the file name is the page name |
| `route_template` | `/{name}/` | page name → path; `{name}` excludes the `-mobile` suffix |
| `routes` | `{}` | per-page overrides (a path, or a full URL) |
| `threshold` | `0.90` | minimum SSIM to pass |
| `mobile_suffix` | `-mobile` | names ending in it are captured in a 390px mobile context |
| `desktop_viewport` / `mobile_viewport` | 1440×900 / 390×844 | |
| `mobile_device_scale_factor` | `2` | set `1` if mobile references are 390px wide |
| `mask_selectors` | `[]` | elements hidden before capture (dynamic content) |
| `wait_until`, `navigation_timeout_ms` | `networkidle`, `30000` | |
| `runtime_dir` | `.qa-screens/runtime` | captures, diffs, previews, reports (add it to `.gitignore`) |
| `default_profile` | `null` | auth profile used when a tool doesn't pass one |
| `ai_critique`, `gemini_model` | `false` | optional Gemini second opinion (`pip install qa-screens[ai]`, `GEMINI_API_KEY`) |

The environment variables `QA_SCREENS_BASE_URL`, `QA_SCREENS_REFERENCES_DIR`,
`QA_SCREENS_ROUTE_TEMPLATE`, `QA_SCREENS_THRESHOLD` and `QA_SCREENS_PROFILE` override the file.

## Tools

| tool | purpose |
|---|---|
| `qa_config` | effective config, references and their URLs, auth profiles; start here |
| `run_qa` | QA all or some pages; returns a JSON summary plus previews of the worst failures |
| `qa_page` | QA one page (fast fix loop); `url` overrides the mapping |
| `capture` | screenshot any URL, full page or a single element (`clip_selector`) |
| `compare_images` | SSIM two image files, for example a Figma export and a capture |
| `capture_set` / `compare_sets` | before/after parity check for refactors |
| `ab_compare` | compare two running servers page by page |
| `update_reference` | promote a live capture to reference (needs `confirm=true`, keeps a backup) |
| `sync_figma` | download Figma frames as references (`FIGMA_TOKEN`) |
| `auth_update_profile` | create or update an auth profile: token, OAuth, cookies, headers, storage |
| `auth_browser_login` | log in with a real browser (form, SSO or MFA) and save the session |
| `auth_oauth_login` | OAuth authorization code + PKCE in a browser window |
| `auth_import_storage_state` | import a Playwright `storageState` JSON |
| `auth_check` | check that a profile is logged in (status, redirect, screenshot) |
| `auth_profiles` / `auth_delete_profile` | list, inspect (never shows secrets) or delete profiles |
| `error_reports` | error-reporting status; `flush=true` posts pending reports |

Page results are `PASS`, `FAIL` or `ERROR`. `ERROR` means the comparison is meaningless:
an HTTP 4xx/5xx, a navigation failure or a missing reference. The agent should fix
the environment, not the CSS.

## Authentication for apps

A **profile** is a named session that is applied to every browser context that uses it:

```text
auth_update_profile(
  name="staging",
  origins=["https://app.staging.example.com"],
  oauth={"grant_type": "client_credentials",
         "token_url": "https://idp.example.com/oauth/token",
         "client_id": "qa-bot", "client_secret": "env:QA_CLIENT_SECRET",
         "audience": "https://api.example.com"},
  apply_token_as=["header", "local_storage:access_token"])

run_qa(profile="staging", base_url="https://app.staging.example.com")
```

- **Secrets**: any value can be `"env:VAR"`. It is resolved from the server's environment
  at use time, so it never passes through the AI conversation.
- **Scoping**: headers and tokens go only to the profile's `origins` (default: the
  base URL's origin), never to CDNs or third parties. Cookies follow normal browser rules.
- **Token placement** (`apply_token_as`): `header` (`Authorization: Bearer …`),
  `header:X-Api-Key`, `local_storage:<key>`, `session_storage:<key>`,
  `local_storage_json:<key>` (the whole token object), `cookie:<name>`.
- **Refresh**: expired tokens are refreshed through `refresh_token` or by re-running the
  client-credentials/password grant, including mid-session in the long-lived server.
- **Storage**: profiles live in `~/.local/state/qa-screens/profiles/` with mode `0600`,
  outside the project, so they are never committed. `QA_SCREENS_STATE_DIR` moves the location.

## Error reporting

qa-screens reports its own bugs, so they reach the maintainers without a manual bug report.

- **Significant errors** (unexpected exceptions in a tool, not user errors like a missing
  reference or bad credentials) are written to `~/.local/state/qa-screens/reports/pending/`
  and posted in the background as a GitHub issue on `astuanax/qa-screens`.
- **Crashes**: uncaught exceptions are logged to a crash file, and hard crashes (segfault,
  abort) are captured by `faulthandler`. On the **next start-up** the server scans for these
  files and posts them.
- A report stays pending until it has been posted, so reports made offline or without a token
  are sent later.
- **De-duplication**: each error has a stable fingerprint. A repeat within 24h is not
  re-posted, and an open issue with the same fingerprint gets a comment instead of a new
  issue. At most 10 posts per day.
- **Privacy**: tokens, JWTs, cookies, passwords, URL query strings and credentials, and the
  home directory path are redacted. Tool arguments that hold secrets are dropped entirely.

| env var | default | |
|---|---|---|
| `QA_SCREENS_ERROR_REPORTING` | `on` | `on`, `local` (write files, never post) or `off` |
| `QA_SCREENS_ISSUE_REPO` | `astuanax/qa-screens` | where issues go (point it at your fork) |
| `QA_SCREENS_GITHUB_TOKEN` | falls back to `GITHUB_TOKEN`, then `gh auth token` | needs `issues:write` |

Without a token nothing is posted; reports wait in `pending/`. `qa-screens reports --flush`
posts them by hand.

## CLI (CI-friendly)

```bash
qa-screens                       # MCP server over stdio (same as `qa-screens serve`)
qa-screens run                   # QA every reference; exit 1 on FAIL/ERROR
qa-screens run nl-be faq --viewport desktop --base-url http://localhost:8080 --json
qa-screens reports --flush       # post pending error reports
```

A report is written to `.qa-screens/runtime/reports/latest.json` on every run.

## How it works

1. The reference name maps to a URL (`route_template` / `routes`). Names ending in `-mobile`
   use a mobile context (390px, touch, mobile UA).
2. The page is loaded with the HTTP cache disabled (so the CSS you just edited is what gets
   measured), with animations, transitions and scrollbars disabled, and after
   `document.fonts.ready`. It is captured full-page.
3. SSIM is computed on grayscale images. Captures above 40MP are decoded at reduced
   resolution, so long pages never run out of memory. Different sizes are handled by `align`:
   `resize` (legacy default), `crop` or `pad` (height changes count as differences).
4. Changed pixels are grouped into region boxes. A red heatmap and a cropped side-by-side
   preview are written, and the preview is returned to the agent as an image.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
python -m playwright install chromium
pytest
```

Releases are published to PyPI by GitHub Actions when a `v*` tag is pushed (trusted publishing).

## License

MIT
