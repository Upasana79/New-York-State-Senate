# New York State Senate Laws Crawler

A small Playwright-based crawler for the New York State Senate Consolidated Laws site:

https://www.nysenate.gov/legislation/laws/CONSOLIDATED

It discovers law-title links from the root page, walks the law hierarchy, and writes leaf legal-text pages to incremental XML.

## Repository Contents

Minimum files needed to run the crawler on another computer:

- `src/`
- `config/nysenate_crawler.yaml`
- `requirements.txt`
- `README.md`

Optional but useful:

- `tests/test_nysenate_pilot_crawler.py`
- `tmp/make_nysenate_click_capture_report.py`
- `Learnings from last scrapper/`
- `Reference code 1/`

The two reference folders are kept for project context. They are not required for a normal run.

Generated output goes to `data/` and `logs/`; those folders are ignored by Git.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chrome
```

On macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chrome
```

## Smoke Test

This crawls a tiny sample and writes one XML document if the site is reachable:

```powershell
python src\nysenate_crawler\pilot_crawler.py --config config\nysenate_crawler.yaml --max-pages 5 --max-documents 1
```

Expected output includes:

```text
Crawl complete: 1 document(s)
```

## Normal Run

```powershell
python src\nysenate_crawler\pilot_crawler.py --config config\nysenate_crawler.yaml
```

Default config is intentionally bounded:

- `pilot_title_limit: 1`
- `max_pages: 250`
- `max_documents: 50`

To crawl more laws, edit `config/nysenate_crawler.yaml`:

```yaml
targets:
  pilot_title_limit: null
  max_pages: 100000
  max_documents: 100000
```

## Optional CapSolver Key

Keep your CapSolver key out of Git. The crawler loads it from `CAPSOLVER_API_KEY` first, then from the ignored local file `captcha key.txt`.

```powershell
$env:CAPSOLVER_API_KEY = (Get-Content -Raw "captcha key.txt").Trim()
```

The crawler avoids unnecessary paid solves first.

- Normal page -> no CapSolver.
- Rate limit -> back off, no CapSolver.
- Slow response -> retry, no CapSolver.
- Browser profile works -> keep crawling normally.
- CAPTCHA appears -> CapSolver gets first shot.
- Cloudflare appears -> CapSolver gets first shot.
- Human verification appears -> CapSolver gets first shot.
- CapSolver succeeds -> apply token or cookie.
- Token applied -> reload target URL.
- Cookie applied -> reload target URL.
- Challenge clears -> continue crawling normally.
- CapSolver fails -> try browser interaction.
- Browser fallback clears -> continue crawling normally.
- Everything fails -> save evidence and stop.
- Checkpoints stay preserved after failure.

CapSolver is configured for real solving.

- Startup -> check CapSolver balance.
- Balance check -> no solve is spent.
- Solve attempt -> log task submission.
- Solve completion -> log task completion.
- Turnstile -> use proxyless Turnstile task.
- Cloudflare -> use Cloudflare Challenge task.
- Cloudflare -> requires static or sticky proxy.
- Browser proxy -> match CapSolver proxy.
- Matching proxy -> preserve clearance validity.
- Returned token -> inject response fields.
- Returned cookie -> add Playwright cookies.
- Remaining challenge -> save debug evidence.

Cloudflare managed challenges require a static or sticky proxy in `config/nysenate_crawler.yaml`. By default, the crawler also launches Playwright with this same proxy so CapSolver's clearance cookie matches the browser IP:

```yaml
captcha:
  capsolver_proxy: http:host:port:user:pass
  capsolver_preferred: true
  capsolver_use_proxy_for_browser: true
```

Live logs include `Submitting CapSolver task...` and `CapSolver task completed...` lines for every solve attempt. On startup, the crawler also checks `getBalance` and logs the reported balance when `capsolver_log_balance_on_start` is true.

## Output

The crawler writes:

- XML: `data/nysenate_consolidated_laws_pilot.xml`
- visited checkpoint: `data/nysenate_visited_urls.txt`
- failed checkpoint: `data/nysenate_failed_urls.txt`
- persistent Chrome identity profiles: `data/sessions/nysenate_uaNN_profile/`
- browser identity metadata: `data/sessions/nysenate_uaNN_meta.json`
- log file: `logs/nysenate_crawler.log`
- debug files on failures: `logs/nysenate_debug/`

The XML fields are:

- `sourceURL`
- `revisionDate`
- `level10` through `level100`
- `contents`

## Run Tests

```powershell
python -m unittest tests.test_nysenate_pilot_crawler
```

## Generate HTML Evidence Report

To review the navigation rule visually, run the visible-browser report generator:

```powershell
python tmp\make_nysenate_click_capture_report.py
```

The script loads `config/nysenate_crawler.yaml`, reuses the crawler selectors and browser settings, and writes a timestamped local report under `evidence/nysenate_click_capture_*/index.html`. The final report section summarizes CAPTCHA/anti-bot settings and any challenge, rate-limit, or timeout signals seen during the evidence run.

## Notes

- The default config launches real visible Chrome with `channel: chrome` and `headless: false`.
- User agents are looped through a cursor. Each user agent gets its own `data/sessions/<target>_uaNN_profile/` directory so cookies, local storage, and cache stay matched to that browser identity.
- For multi-title crawls, `rotate_user_agent_per_title: true` opens the next identity between root law titles.
- The browser uses `--disable-blink-features=AutomationControlled`, `--start-maximized`, and applies `playwright-stealth` to the shared crawl page when `browser.stealth_enabled` is true.
- If the site asks for human verification, the crawler preserves screenshot/HTML evidence, backs off, and stops with checkpoints preserved if the challenge does not clear.
- For GitHub, keep `data/`, `logs/`, browser profiles, and secret files out of commits.
