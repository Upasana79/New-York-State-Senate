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
- `Learnings from last scrapper/`
- `Reference code 1/`

The two reference folders are kept for project context. They are not required for a normal run.

Generated output goes to `data/` and `logs/`; those folders are ignored by Git.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

On macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Smoke Test

This crawls a tiny sample and writes one XML document if the site is reachable:

```powershell
python src\nysenate_crawler\pilot_crawler.py --config config\nysenate_crawler.yaml --headless true --max-pages 5 --max-documents 1
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

## Output

The crawler writes:

- XML: `data/nysenate_consolidated_laws_pilot.xml`
- visited checkpoint: `data/nysenate_visited_urls.txt`
- failed checkpoint: `data/nysenate_failed_urls.txt`
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

## Notes

- If Chrome is not installed, the script falls back to Playwright Chromium.
- If the site asks for human verification, the crawler stops and preserves checkpoints.
- For GitHub, keep `data/`, `logs/`, browser profiles, and secret files out of commits.
