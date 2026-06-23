# New Project Handoff Advice

This handoff is based on the latest Python diff in this repo as of 2026-06-23. The current working tree changed `src/nysenate_crawler/pilot_crawler.py` by about +589/-13 lines and `tests/test_nysenate_pilot_crawler.py` by +335 lines. The important change is not "more scraper code"; it is a stronger crawler operating model: persistent browser identity, explicit checkpoints, safer retries, challenge detection, optional CapSolver support, and tests for the failure paths.

## What changed in the latest Python version

- Added `CapSolverClient` for `createTask`, `getTaskResult`, and `getBalance` calls.
- Added startup CapSolver readiness logging so a bad key or empty account is visible before a long run.
- Added config knobs for pre-navigation jitter, 429 backoff, timeout backoff, CapSolver API settings, proxy settings, challenge fallback behavior, and challenge cooldown.
- Changed navigation to classify 429, timeouts, no-response navigation, and human-verification pages separately.
- Added challenge fingerprinting from status code, page title, and body text.
- Added two challenge paths: Turnstile token injection and Cloudflare clearance-cookie application.
- Added proxy reuse for Playwright when `capsolver_use_proxy_for_browser` is true, which matters because Cloudflare clearance usually depends on the browser IP matching the solver IP.
- Added debug evidence on challenge and navigation failures.
- Expanded tests with fake pages and fake CapSolver clients so challenge, proxy, cookie, and backoff behavior can be checked without live-site calls.

## The reusable crawler shape

Carry this architecture into the next project:

- Keep runtime configuration outside code in a YAML file.
- Keep credentials outside Git. Load from environment first, then an ignored local file.
- Use a persistent browser context, not a fresh context per page.
- Give each browser identity its own profile directory when rotating user agents.
- Discover links from navigation pages, but capture hierarchy labels from the destination page after navigation.
- Emit only leaf/content pages unless the target schema explicitly wants navigation pages.
- Write XML incrementally and keep visited/failed checkpoints so crashes do not waste the whole run.
- Save HTML and screenshots when a page is rejected or blocked.
- Make extraction functions small enough to test with fake page objects.

## New project intake checklist

Before writing scraper logic, capture these facts in the new repo:

- Target root URL and the exact legal/data scope.
- Whether the site has an official API, downloadable corpus, sitemap, or bulk feed.
- Expected hierarchy levels and the final output schema.
- What counts as a record: navigation page, leaf page, document page, or section block.
- Selectors for child links, page title/header, body text, revision metadata, pagination, and cookie banners.
- Rate limits or terms that apply to the crawl.
- Required browser state: login, cookies, region, locale, timezone, or accepted notices.
- Expected failure modes: 403, 429, 503, CAPTCHA, Cloudflare, login expiry, empty page, malformed page.

## First implementation pass

Start narrow and make the first run boring:

1. Build a bounded crawler with `max_pages`, `max_documents`, and a one-root pilot scope.
2. Implement URL canonicalization and target-domain filtering before recursion.
3. Implement destination-page title extraction before relying on navigation-row text.
4. Add incremental output and checkpoints before attempting a full crawl.
5. Add validation rules that reject short content, missing source URLs, and navigation pages.
6. Run a tiny live smoke test and inspect the XML manually.
7. Only then expand limits and add challenge automation.

## Navigation HTML evidence workflow

Before finalizing selectors or level rules, generate a local HTML evidence report for annotation feedback. The goal is to show the whole navigation decision path visually: what row the crawler clicks, what destination page loads, which header becomes the next `levelXX`, and where final contents come from.

For this repo, run the existing visible-browser report generator from the repository root:

```powershell
python tmp\make_nysenate_click_capture_report.py
```

The generator loads `config/nysenate_crawler.yaml` by default, reuses the crawler selectors, browser identity settings, timeout/backoff values, challenge markers, and CapSolver proxy-to-browser proxy setting. It ends with a CAPTCHA/anti-bot evidence section that masks secrets, summarizes CapSolver/proxy/backoff settings, and lists any rate-limit, timeout, or challenge signals seen during the run. Override the scope when needed:

```powershell
python tmp\make_nysenate_click_capture_report.py --law-path /legislation/laws/ABC --article-count 4
```

It writes a timestamped report like:

```text
evidence/nysenate_click_capture_YYYYMMDD_HHMMSS/index.html
```

The current script launches persistent visible Chrome, navigates to the NY Senate root, clicks into ABC, captures four article paths, saves screenshots, and builds an `index.html` report. If human verification appears, Chrome stays visible and the script waits for the verification to clear.

For a new project, create the same kind of script early. It should:

1. Launch visible persistent Chrome with a reusable profile.
2. Navigate to the root page and dismiss cookie or notice banners.
3. Capture the first representative navigation path through at least three levels.
4. Save screenshots before each click and after each destination page loads.
5. Draw annotations directly on screenshots:
   - Red: click target used only for navigation.
   - Blue: destination header captured as a `levelXX` value.
   - Purple: body/content selector captured as `contents`.
6. Build a local `index.html` that embeds the screenshots and records the URL, row text, captured header text, and content preview.
7. Print the report directory path so it can be opened and shared.

Use wording that prevents the most common scraper mistake:

```text
CLICK row to enter level10
CAPTURE level10 from destination header
CLICK row to enter level20
CAPTURE level20 from destination header
CLICK row to enter level30
CAPTURE level30 from destination header
CAPTURE contents from <content selector>
```

Ask reviewers or annotators to mark the HTML report with:

- whether each red click target is the right navigation link
- whether each blue header is the correct source for the level field
- whether row text should remain navigation-only metadata
- whether the purple content block includes too much, too little, or the right body text
- whether any extra metadata such as revision date, effective date, notes, or breadcrumbs should be captured

After annotation feedback, update selectors and tests before expanding the crawl. Treat the HTML report as the shared contract between the site reviewer and the crawler implementation.

## Challenge-handling policy

Do not spend paid solves or escalate browser behavior just because a page is slow.

- Normal page: keep crawling.
- 429: back off and retry.
- Timeout: back off and retry.
- 403/503 plus challenge text: save evidence, then try the configured challenge path.
- CapSolver key missing: log that only browser fallback is available.
- CapSolver key present: check balance at startup when enabled.
- Turnstile: solve token, inject response fields, trigger callbacks or submit if present.
- Cloudflare managed challenge: require a static or sticky proxy.
- If CapSolver returns cookies or `cf_clearance`, add them to the browser context and reload the target URL.
- If challenge markers remain after a solve, preserve debug files and stop cleanly with checkpoints intact.

For Cloudflare specifically, keep the solver proxy and Playwright proxy aligned:

```yaml
captcha:
  capsolver_proxy: http:host:port:user:pass
  capsolver_preferred: true
  capsolver_use_proxy_for_browser: true
```

## Config knobs to keep

These settings are worth carrying to the next project:

```yaml
timing:
  page_load_timeout_ms: 45000
  selector_timeout_ms: 8000
  navigation_delay_ms: 1000
  content_stable_delay_ms: 1200
  pre_navigation_delay_min_ms: 1000
  pre_navigation_delay_max_ms: 3000
  max_retries: 3
  retry_backoff_seconds: 2
  rate_limit_backoff_base_seconds: 15
  timeout_backoff_base_seconds: 10

debug:
  save_html_on_failure: true
  save_screenshot_on_failure: true

validation:
  min_contents_chars: 20
  require_source_url: true
  require_at_least_one_level: true
  reject_navigation_pages: true

captcha:
  capsolver_key_file: captcha key.txt
  capsolver_api_base: https://api.capsolver.com
  capsolver_proxy: ""
  capsolver_poll_interval_seconds: 2
  capsolver_timeout_seconds: 120
  capsolver_request_timeout_seconds: 30
  capsolver_preferred: true
  capsolver_log_balance_on_start: true
  capsolver_use_proxy_for_browser: true
  challenge_interaction_enabled: true
  challenge_cooldown_seconds: 15
```

## Testing pattern

The tests that matter most are not full browser tests. Keep most coverage cheap:

- Pure URL tests for canonicalization and target filtering.
- Pure hierarchy tests for level assignment and overflow.
- Fake-page navigation tests for 429, challenge detection, failed navigation, and retry timing.
- Fake CapSolver tests for polling, error handling, cookie payload conversion, and proxy parsing.
- XML store tests for incremental writes, duplicate source URLs, and malformed existing XML.
- Evidence-report review for navigation workflow and annotation feedback before broad crawling.
- One manual smoke test against the live site before a long crawl.

Run:

```powershell
python -m unittest tests.test_nysenate_pilot_crawler
python src\nysenate_crawler\pilot_crawler.py --config config\nysenate_crawler.yaml --max-pages 5 --max-documents 1
```

## Handoff risks

- The current challenge solver code is defensive, but live challenge vendors and Cloudflare flows change often. Treat live challenge clearing as something to verify, not assume.
- CapSolver Cloudflare tasks need a static or sticky proxy. Without that, cookie clearance may not match the browser session.
- Headful real Chrome is the default. A server environment may need different browser installation and display handling.
- The crawler is currently NY Senate specific. Do not blindly reuse selectors, root URL filtering, XML root names, or level semantics on the next site.
- The old `Learnings from last scrapper/Advice.md` is useful background for Playwright scraping, but it is NJAC/LexisNexis specific and should not drive NY Senate or future-site assumptions.

## Clean handoff package

For a new project handoff, include:

- `README.md` with setup, smoke test, normal run, output files, and test command.
- `config/<target>_crawler.yaml` with conservative limits.
- `src/<target>_crawler/` with crawler code.
- `tests/` with fake-page and pure-function tests.
- `.gitignore` that excludes output, logs, browser profiles, and secret files.
- `advise.md` with site facts, operating model, known risks, and the latest diff summary.

The main habit to preserve is simple: make every long crawl resumable, inspectable, and bounded before making it broad.
