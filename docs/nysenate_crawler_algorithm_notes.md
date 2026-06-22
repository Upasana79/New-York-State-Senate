# NYSenate Consolidated Laws Scraper - Implementation Handoff

Prepared: 2026-06-23

This is the current self-contained handoff for the New York Senate Consolidated Laws scraper. The earlier approval HTML/MD files were deleted so future work should treat this file as the durable project note.

## Goal

Build a Python scraper for:

`https://www.nysenate.gov/legislation/laws/CONSOLIDATED`

For the first implementation test, crawl one title/law only. The scraper should still discover titles from the consolidated laws root, start with the first title returned by that root loop, process that one title, then stop the main title loop.

The purpose of the pilot is to prove the hierarchy algorithm and XML output shape before expanding to all consolidated laws.

## References Already Reviewed

- `Learnings from last scrapper/Advice.md`
  - Use the `level10` through `level100` hierarchy model.
  - Output XML.
  - Include `sourceURL`.
  - Include `contents`.
  - Store/write incrementally so crashes do not lose all progress.
  - Use retries, validation, and clear logging.
- `levels.pdf`
  - Root/list pages are mostly navigation.
  - Capture the level from the page you land on, not from the row you are about to click.
  - Keep looping into the next level if child links continue.
- Previous Playwright/Cloudflare reference folders
  - Use Playwright as the first implementation path for this site.
  - Borrow the AO scraper patterns where they help: persistent browser context, realistic waits, retry/backoff, rendered-DOM extraction, cookie/modal cleanup, visited/failed tracking, incremental writes, and targeted debug artifacts.
  - Do not make browser screenshots the main scraping strategy.

## Team Decisions From Chat

- Pilot scope: one title/law only for now.
- Pilot title selection: use the first title discovered from the root page, then stop the title loop after that one title.
- Output format: XML.
- Table of contents/index pages are not needed as output records.
- Root/chapter/article/index pages are used to discover child URLs and carry levels forward.
- Do not hard-code a level to a semantic name like article, section, chapter, title, part, or subpart.
- Continue to `level50`, `level60`, `level70`, etc. if the selected law has that many nested child pages.
- Continue through `level100` as needed.
- If a path goes beyond `level100`, flag it for schema review instead of silently dropping data.
- Do not aggressively capture black/blocked screenshots before the production Python code exists.

## Core Algorithm

The crawler is depth-based.

It should not stop at a fixed visual step such as "Step 4" and should not stop just because a page appears to be a section. If the current page still has relevant child links, keep looping through those child links and capture contents only after reaching the actual leaf/legal-text page for that path.

Important priority:

1. Capture the current page title into the next available level.
2. Check for relevant child links.
3. If relevant child links exist, recurse into them and do not emit this page yet.
4. If no relevant child links exist and legal text exists, emit one XML record.

In short: child links beat leaf detection.

## Pilot Crawl Flow

### 1. Consolidated Laws Root

URL:

`https://www.nysenate.gov/legislation/laws/CONSOLIDATED`

Behavior:

- Loop through law rows as candidate title/law links.
- For the pilot, take the first law row discovered by the loop.
- Process that one selected title/law.
- Stop the root title loop after the first title.
- Do not capture `level10` from root rows.
- Do not emit root rows.

Why:

The root page is only a chooser. Its rows tell us where to go next, but the level value should come from the selected law page itself.

### 2. Selected Law Page

Sample URL if the first discovered title happens to be `ABC`:

`https://www.nysenate.gov/legislation/laws/ABC`

Behavior:

- Capture the current page title block as `level10`.
- For the current first-title example, expected value is like:
  - `CHAPTER 3-B Alcoholic Beverage Control`
- Find child links.
- Queue child links.
- Do not capture child row text as final data yet.
- Do not emit this page as a record.

### 3. First Child Page

Sample URL if the first discovered title happens to be `ABC`:

`https://www.nysenate.gov/legislation/laws/ABC/A1`

Behavior:

- Capture the current page title block as `level20`.
- For this sample first child, expected value is like:
  - `ARTICLE 1 Short Title; Policy of State and Purpose of Chapter: Definitions`
- Find child links.
- Queue child links.
- Do not emit this page as a record if child links exist.

### 4. Next Child Pages

Sample URL if the first discovered title happens to be `ABC`:

`https://www.nysenate.gov/legislation/laws/ABC/1`

Behavior:

- Capture the current page title block as the next dynamic level.
- In the shallow sample path, this is likely `level30`:
  - `SECTION 1 Short title`
- Check for relevant child links before deciding the page is a leaf.
- If child links exist, continue to `level40`, `level50`, `level60`, `level70`, and onward.
- If no relevant child links exist and `.nys-openleg-result-text` exists, capture that text as `contents` and emit XML.

This page is an example leaf for the current first-title sample, not a universal stopping point.

## Dynamic Level Assignment

Levels are assigned by crawl depth, not by legal label.

Sample shallow path if the first discovered title happens to be `ABC`:

- `/laws/ABC` -> `level10`
- `/laws/ABC/A1` -> `level20`
- `/laws/ABC/1` -> `level30` plus `contents` if it has no relevant child links

Example deeper path:

- selected law page -> `level10`
- child page -> `level20`
- child page -> `level30`
- child page -> `level40`
- child page -> `level50`
- child page -> `level60`
- child page -> `level70`
- continue through `level100`
- final leaf page -> `contents`

Empty deeper levels are acceptable when a law is shallow.

## Page Type Rules

### Navigation/Index Page

Treat a page as navigation/index when it has relevant child links such as `.nys-openleg-result-item-link`.

For navigation pages:

- Capture the current page title into the next level if it is below root.
- Carry all previous levels forward.
- Queue child URLs.
- Do not emit XML for the page itself.
- Do not output table-of-contents body text.

### Leaf/Content Page

Treat a page as a leaf/content page when:

- There are no relevant child links to descend into, and
- Legal text exists in `.nys-openleg-result-text`.

For leaf pages:

- Capture the current page title into the next level.
- Capture `.nys-openleg-result-text` as `contents`.
- Emit one XML `<document>` record.

## Selectors

Use these selectors as the first implementation target:

| Purpose | Selector |
| --- | --- |
| Child URL rows | `.nys-openleg-result-item-link` |
| Child row label | `.nys-openleg-result-item-name` |
| Child row description | `.nys-openleg-result-item-description` |
| Current page headline | `.nys-openleg-result-title-headline` |
| Current page short title | `.nys-openleg-result-title-short` |
| Current page location | `.nys-openleg-result-title-location` |
| Legal contents | `.nys-openleg-result-text` |

Breadcrumbs can be used for validation, but the main level-building strategy is parent-to-child carry-forward.

## Record Fields

Each emitted XML document should contain:

- `sourceURL`
- revision metadata, if visible
- `level10`
- `level20`
- `level30`
- `level40`
- `level50`
- `level60`
- `level70`
- `level80`
- `level90`
- `level100`
- `contents`

Use empty strings for unused deeper levels.

## XML Shape

Use an XML shape consistent with `Advice.md`, adapted for NYSenate. The values below are sample values from the likely first discovered title, not hard-coded scraper inputs:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<nysenateDocuments>
  <metadata>
    <source>New York Senate Consolidated Laws</source>
    <sourceURL>https://www.nysenate.gov/legislation/laws/CONSOLIDATED</sourceURL>
    <extractionDate>...</extractionDate>
    <pilotLaw>first discovered title from root loop</pilotLaw>
    <totalDocuments>...</totalDocuments>
  </metadata>
  <documents>
    <document>
      <sourceURL>https://www.nysenate.gov/legislation/laws/ABC/1</sourceURL>
      <revisionDate>2014-09-22</revisionDate>
      <level10>CHAPTER 3-B Alcoholic Beverage Control</level10>
      <level20>ARTICLE 1 Short Title; Policy of State and Purpose of Chapter: Definitions</level20>
      <level30>SECTION 1 Short title</level30>
      <level40></level40>
      <level50></level50>
      <level60></level60>
      <level70></level70>
      <level80></level80>
      <level90></level90>
      <level100></level100>
      <contents>...</contents>
    </document>
  </documents>
</nysenateDocuments>
```

## Revision Metadata

If visible, capture revision text/date such as:

`Viewing most recent revision (from 2014-09-22)`

For the pilot, use the latest/current visible revision. Historical revisions are a separate mode and should not be mixed into the first scraper unless explicitly requested.

## Validation Rules

Flag or reject a record when:

- `sourceURL` is missing.
- `contents` is empty or too short.
- No captured level exists for the current page.
- The page looks like a navigation/index page but was emitted as content.
- The URL was already visited in the current crawl path.

## Implementation Notes

- Use Playwright first for this scraper. The NYSenate site should be treated as dynamic or hostile enough that the first production implementation should load pages in a real browser context and extract from the rendered DOM.
- Borrow the useful AO scraper practices:
  - Use a persistent Chrome/browser context when it improves stability or preserves trusted session state.
  - Use realistic waits, content-stabilization checks, and retry/backoff for navigation and selector failures.
  - Extract data from the rendered DOM with scoped selectors or bounded page evaluation instead of relying on screenshots.
  - Clean up cookie banners, newsletter modals, overlays, or other blocking UI when they interfere with extraction.
  - Keep `visited_urls` and `failed_urls`.
  - Write XML incrementally so a crash does not lose all progress.
  - Save debug HTML and screenshots only when a page is blocked, empty, malformed, or fails validation.
  - Use the Chrome plugin/real Chrome session as a backup when normal Playwright needs existing browser/session state.
- Do not rely on screenshot evidence as the main extraction source.
- Do not take repeated black/blocked screenshots during planning.
- Implement retries for page load and extraction failures.
- Log each URL, captured level, child count, and emit/skip decision.
- Do not rely on fixed screen coordinates. Use selectors, scoped locators, or bounded DOM evaluation.
- Do not assume legal numbering is sequential. Sections, articles, parts, or other path segments may skip numbers or appear out of expected order.
- Preserve legal references/cross-references inside `contents`; do not treat every legal citation as a crawl target unless it appears as a relevant child navigation link for the current hierarchy.
- Use breadcrumbs as validation evidence when available, but keep parent-to-child carry-forward as the main level-building strategy.
- If CAPTCHA or an explicit human verification appears, pause for user/manual resolution and record it in logs. Do not make CAPTCHA solving or repeated challenge screenshots part of the normal extraction loop.

## Config Guidance

Use a YAML config, following the useful shape of `Reference code 1/config_ao.yaml`, so site-specific scraper behavior is easy to adjust without editing crawler logic.

Recommended config groups:

- `browser`
  - `headless`: default `false` for difficult sites.
  - `channel`: prefer Chrome when using a persistent local profile.
  - `viewport_width` and `viewport_height`: default to a large stable viewport such as `1920x1080`.
  - `locale`, `accept_language`, and `timezone`.
  - `user_data_dir`: persistent browser profile/session directory.
  - `user_agents`: modern Chrome user-agent pool, refreshed for the target run when needed.
- `targets`
  - `root_url`: `https://www.nysenate.gov/legislation/laws/CONSOLIDATED`.
  - `pilot_title_limit`: default `1`.
  - `max_pages` or `max_documents`: safety guard against accidental broad crawls.
- `files`
  - `output_xml`: XML output path.
  - `log_file`: crawler log path.
  - `visited_file`: checkpoint of visited URLs.
  - `failed_file`: checkpoint of failed URLs.
  - `debug_dir`: location for failure-only HTML/screenshots.
- `timing`
  - `page_load_timeout_ms`.
  - `selector_timeout_ms`.
  - `navigation_delay_ms`.
  - `max_retries`.
  - `retry_backoff_seconds`.
- `selectors`
  - Keep the NYSenate selectors from this handoff configurable so selector drift can be patched in config before code changes.
  - Include fallback selectors for title, child links, revision metadata, and legal text when practical.
- `debug`
  - `save_html_on_failure`: default `true`.
  - `save_screenshot_on_failure`: default `true`.
  - `save_debug_on_success`: default `false`.
- `validation`
  - `min_contents_chars`: minimum legal text length required before a leaf record is accepted.
  - `require_source_url`: default `true`.
  - `require_at_least_one_level`: default `true`.
  - `reject_navigation_pages`: default `true`.

## Pseudocode

```python
LEVELS = ["level10", "level20", "level30", "level40", "level50",
          "level60", "level70", "level80", "level90", "level100"]

def crawl_page(url, carried_levels, depth):
    html = fetch(url)
    page = parse(html)

    child_links = extract_relevant_child_links(page)
    legal_text = extract_legal_text(page)

    levels = dict(carried_levels)

    if depth >= 0:
        current_title = extract_current_title(page)
        if current_title:
            levels[LEVELS[depth]] = current_title

    if child_links:
        for child_url in child_links:
            crawl_page(child_url, levels, depth + 1)
        return

    if legal_text:
        emit_xml_record(
            sourceURL=url,
            levels=levels,
            contents=legal_text,
            revisionDate=extract_revision_date(page),
        )
        return

    log_failed_or_empty_page(url)

def run_pilot():
    root = fetch("https://www.nysenate.gov/legislation/laws/CONSOLIDATED")
    for title_url in extract_root_law_urls(root):
        crawl_page(title_url, carried_levels={}, depth=0)
        break  # pilot mode: process one title only
```

## Current Next Step

Write the Python pilot crawler in one-title mode using the recursive level-carrying algorithm above. Do not hard-code `ABC`; let the root loop discover the first title, crawl that one title, then stop. Do not rebuild approval HTML/MD docs unless the user asks for them.
