# Level Capture Surgical Change Requirement

## Objective

Make the crawler follow the confirmed level-capture rule from the reviewed NY Senate workflow:

- Navigation rows are used only to enter the next level.
- XML level text is captured only after entering the destination page.
- The destination page header is the source of truth for `level10`, `level20`, `level30`, and deeper levels.
- Final legal text is captured only from a leaf page's `.nys-openleg-result-text`.

In plain terms:

```text
Click the row first.
Then capture the level from the page you land on.
Do not store level text from the row before clicking.
```

## Current Behavior Summary

The crawler currently visits a URL, reads the destination page title, assigns it to the current level, then either recurses into child links or emits a leaf document. That high-level behavior is correct.

The risk is that related report/debug language and future edits could treat child-row text as authoritative level text because `extract_child_links()` collects row `label` and `description`. This requirement makes the intended behavior explicit and testable.

## Required Behavior

### Root Page

At:

```text
https://www.nysenate.gov/legislation/laws/CONSOLIDATED
```

The crawler must:

1. Locate law rows using `.nys-openleg-result-item-link`.
2. Click or navigate to the selected law URL, for example:

```text
https://www.nysenate.gov/legislation/laws/ABC
```

3. Do not store the root row text, such as `Alcoholic Beverage Control`, as `level10`.
4. After the destination law page loads, capture `level10` from:

```text
.nys-openleg-result-title-headline
.nys-openleg-result-title-short
```

Example expected `level10`:

```text
CHAPTER 3-B Alcoholic Beverage Control
```

### Article Pages

On a law page such as:

```text
https://www.nysenate.gov/legislation/laws/ABC
```

The crawler must:

1. Use article rows only as navigation targets.
2. Do not store the article row text as `level20` before clicking.
3. After entering the article URL, capture `level20` from the destination page header.

Example:

```text
Click:
https://www.nysenate.gov/legislation/laws/ABC/A1

Then capture level20 from the destination page header:
ARTICLE 1 Short Title; Policy of State and Purpose of Chapter: Definitions
```

### Section / Leaf Pages

On an article page such as:

```text
https://www.nysenate.gov/legislation/laws/ABC/A1
```

The crawler must:

1. Use section rows only as navigation targets.
2. Do not store the section row text as `level30` before clicking.
3. After entering the section URL, capture `level30` from the destination page header.
4. If the destination page has legal text and no child links, emit the XML document.
5. Capture document contents from:

```text
.nys-openleg-result-text
```

Example:

```text
Click:
https://www.nysenate.gov/legislation/laws/ABC/1

Then capture:
level30 = SECTION 1 Short title
contents = text from .nys-openleg-result-text
```

## Scope

The surgical change should be limited to:

- Making the destination-page-header rule explicit in code comments or helper names where useful.
- Ensuring `LinkCandidate.label` and `LinkCandidate.description` are treated as navigation/debug metadata only.
- Ensuring records are built only from carried destination-page levels plus leaf legal text.
- Updating tests to assert that child-row text is not used as the captured level.
- Updating or replacing the HTML evidence script/report labels so they show:
  - red = click target
  - blue = destination page header captured as level
  - purple = final legal text captured as contents

## Out Of Scope

Do not include:

- Splitting the one-file crawler into multiple modules.
- Rewriting the crawler architecture.
- Changing the XML schema.
- Changing output field names.
- Changing browser/captcha strategy except what is required to run the evidence report.
- Capturing root row description as `level10`.
- Capturing article or section row descriptions as XML levels.
- Refactoring unrelated logging, config, checkpointing, retries, or XML persistence.

## Acceptance Criteria

The change is accepted only if all of these are true:

1. Root row text is never written directly to `level10`.
2. Article row text is never written directly to `level20`.
3. Section row text is never written directly to `level30`.
4. Each `levelXX` value comes from the destination page header after navigation.
5. Leaf `contents` comes from `.nys-openleg-result-text`.
6. Existing checkpoint and duplicate protection behavior still works.
7. Unit tests pass:

```powershell
python -m unittest tests.test_nysenate_pilot_crawler
```

8. A fresh HTML evidence report can be generated showing the corrected behavior for at least four ABC articles.

## Suggested Test Cases

Add or update focused tests so the rule cannot regress:

1. A parent page exposes a child row with:

```text
row label = ARTICLE 1
row description = Row-only title
```

2. The destination page header exposes:

```text
headline = ARTICLE 1
short title = Destination header title
```

3. The expected captured `level20` is:

```text
ARTICLE 1 Destination header title
```

4. The unexpected value must not appear in the emitted record:

```text
ARTICLE 1 Row-only title
```

Repeat the same pattern for a section-level destination page.

## HTML Evidence Report Requirement

Create or update an evidence script that runs visible Chrome, not headless, and generates a local HTML report with fresh screenshots.

The report must show at least:

- Root page:
  - red annotation on the ABC row click target
  - no green or level-capture label on the root row text
- ABC destination page:
  - blue annotation on the page header captured as `level10`
  - red annotation on Article 1, Article 2, Article 3, and Article 4 row click targets
- Each article destination page:
  - blue annotation on the page header captured as `level20`
  - red annotation on the first section row click target
- Each section destination page:
  - blue annotation on the page header captured as `level30`
  - purple annotation on `.nys-openleg-result-text` captured as `contents`

The report wording must avoid saying row text is captured as a level.

Use labels like:

```text
CLICK to enter level10
CAPTURE level10 from destination header
CLICK to enter level20
CAPTURE level20 from destination header
CLICK to enter level30
CAPTURE level30 from destination header
CAPTURE contents from .nys-openleg-result-text
```

## How To Generate A Similar HTML Report

Run the visible-browser evidence script from the repository root:

```powershell
python tmp\make_nysenate_click_capture_report.py
```

The script should:

1. Launch persistent Chrome with:

```python
context = await p.chromium.launch_persistent_context(
    str(Path("data/nysenate_browser_profile").resolve()),
    channel="chrome",
    headless=False,
    slow_mo=120,
)
```

2. Navigate to:

```text
https://www.nysenate.gov/legislation/laws/CONSOLIDATED
```

3. Click into ABC.
4. Capture four article paths.
5. Save screenshots and `index.html` under:

```text
evidence/nysenate_click_capture_YYYYMMDD_HHMMSS/
```

6. Print the generated report directory path.

If human verification appears, the script should keep Chrome visible and wait for the verification to clear instead of silently failing.

## Expected Deliverables

- A minimal code patch in `src/nysenate_crawler/pilot_crawler.py`, only if needed.
- Focused unit tests in `tests/test_nysenate_pilot_crawler.py`.
- An updated evidence script or replacement script under `tmp/`.
- A fresh evidence report under `evidence/`.
- No broad refactor.
