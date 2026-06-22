# NYSenate Crawler Pilot

## Objective

Implement and verify the first Python pilot crawler for the New York Senate Consolidated Laws site, using `docs/nysenate_crawler_algorithm_notes.md` as the authoritative handoff for algorithm rules, implementation tips, and crawler operating constraints.

## Original Request

Prepare the GoalBuddy board for the NYSenate crawler handoff, then update the board after the handoff changed to a Playwright-first implementation strategy.

## Intake Summary

- Input shape: `existing_plan`
- Audience: repository owner and future scraper maintainers.
- Authority: `requested`
- Proof type: `artifact`
- Completion proof: a Python one-title pilot crawler exists, runs locally in bounded pilot mode, discovers title links from the root page, crawls exactly the first title URL returned by that loop without hard-coding any law code, uses the recursive level-carrying algorithm, emits XML leaf records with required fields, and passes focused verification.
- Goal oracle: a bounded local pilot run plus tests/artifact inspection prove that the crawler uses the updated Playwright-first handoff, discovers title links from the root page, crawls the first title URL returned by that loop, then stops, emits only leaf legal-text records, preserves dynamic levels from `level10` through `level100`, writes incrementally, logs decisions, and does not depend on sample law data.
- Likely misfire: building a scraper that looks plausible but either hard-codes sample law data, uses static parsing against the updated Playwright-first decision, emits table-of-contents/index pages as documents, stops at a fixed depth such as `level30`, treats legal labels as level names, or broad-crawls more than the pilot allows.
- Blind spots considered: live pages may require Playwright waits, persistent browser profile, cookie/modal cleanup, or Chrome/session fallback; selectors may drift; `levels.pdf` may be absent from the repo and must be handled as evidence availability rather than a blocker; verification must avoid accidental broad crawling; generated debug artifacts should be targeted and failure-only.
- Existing plan facts: `docs/nysenate_crawler_algorithm_notes.md` is the durable handoff and the source of implementation tips/tricks. Earlier approval HTML/MD files were deleted and should not be rebuilt unless requested. The pilot must discover title links from the root page, crawl the first title URL returned by that loop, then stop. Root rows are discovery only. Child links beat leaf detection. Levels are assigned by crawl depth, not semantic label. XML output must include `sourceURL`, visible revision metadata when available, `level10` through `level100`, and `contents`. Playwright/rendered-DOM extraction is the first implementation path. The AO scraper/config references are implementation examples, not replacement requirements.

## Goal Oracle

The oracle for this goal is:

`A local, bounded one-title pilot run of the NYSenate crawler produces valid XML whose documents come only from leaf/legal-text pages under the first title URL returned by the root-page title-link loop, include sourceURL, revision metadata when visible, level10-level100, and non-empty contents, and are supported by tests/logs showing Playwright-first rendered-DOM extraction, recursive child traversal, one-title stop behavior, incremental writing, validation, and no hard-coded law code or sample law data.`

The PM must keep comparing task receipts to this oracle. Planning, discovery, a passing tiny slice, or a clean-looking board is not enough. The goal finishes only when a final Judge/PM audit maps receipts and verification back to this oracle and records `full_outcome_complete: true`.

## Goal Kind

`existing_plan`

## Current Tranche

Validate the updated handoff against the repository and reference materials, then implement the largest safe useful local slice: a Playwright-first one-title pilot crawler with YAML configuration, bounded crawling safeguards, XML output, incremental persistence, logging, validation, and focused tests or run checks.

## Implementation Authority

Use `docs/nysenate_crawler_algorithm_notes.md` as the current implementation source. In particular, the Worker should preserve these sections:

- Core Algorithm
- Pilot Crawl Flow
- Dynamic Level Assignment
- Page Type Rules
- Selectors
- Record Fields and XML Shape
- Validation Rules
- Implementation Notes
- Config Guidance

The AO scraper/config references may inform browser context, wait, retry, debug, and config patterns, but the NYSenate handoff owns the output shape and crawl semantics.

## Judge Improvement Loop

Judge reviews must apply special pressure to XML and level correctness. A Judge task is not finished by saying "not complete" in prose. If XML, level assignment, traversal, logging, or pilot-scope proof is missing or weak, Judge must define the next Worker loop:

- exact defect or missing proof;
- affected XML/level constraint;
- next Worker objective;
- allowed files;
- verification commands;
- stop conditions;
- whether the existing Worker card can be updated or a new Worker card should be spawned.

The PM must then activate that corrective Worker package unless a stop condition or blocker applies. The goal should loop Worker -> Judge -> Worker until the XML and level constraints are proven or a specific task is blocked with a receipt.

## Non-Negotiable Constraints

- Use Playwright first for page loading and rendered-DOM extraction.
- Use selectors, scoped locators, or bounded DOM evaluation; do not rely on fixed screen coordinates or screenshots as extraction strategy.
- Crawl one root-discovered title/law only in pilot mode, then stop the root title loop.
- Do not hard-code `ABC`, any law code, sample URLs, legal labels, or fixed depth.
- Capture the current page title into the next dynamic level, then check relevant child links before deciding whether to emit.
- Emit XML only for leaf/legal-text pages with no relevant child links.
- Continue through `level100` as needed; flag paths beyond `level100` for schema review instead of dropping data.
- Preserve legal references inside `contents`; do not treat every citation as a child crawl target unless it appears as relevant hierarchy navigation.
- Use YAML config for site behavior, selectors, browser options, file paths, timing, debug settings, and validation thresholds.
- Use retries, content-stabilization checks, clear logging, `visited_urls`, `failed_urls`, and incremental XML writing.
- Save debug HTML/screenshots only for blocked, empty, malformed, or validation-failing pages unless the user asks for broader debug capture.
- If CAPTCHA or explicit human verification appears, pause for user/manual resolution and record it in logs.
- Do not recreate deleted approval HTML/MD artifacts unless the user explicitly asks.

## Stop Rule

Stop only when a final audit proves the full pilot outcome is complete.

Do not stop after Scout/Judge planning if a safe Worker implementation package can be activated. Do not stop after the first implementation pass if verification identifies safe local fixes needed to satisfy the pilot oracle.

## Slice Sizing

Safe means bounded, explicit, verified, and reversible. It does not mean tiny.

For this goal, a good Worker slice should produce a usable pilot crawler and its focused verification in one coherent package whenever Scout/Judge evidence allows it. Avoid spending Worker turns only on tiny helper scaffolding unless that is genuinely required by repo structure or dependency uncertainty.

## Canonical Board

Machine truth lives at:

`docs/goals/nysenate-crawler-pilot/state.yaml`

If this charter and `state.yaml` disagree, `state.yaml` wins for task status, active task, receipts, verification freshness, and completion truth.

## Run Command

```text
/goal Follow docs/goals/nysenate-crawler-pilot/goal.md.
```

## PM Loop

On every `/goal` continuation:

1. Read this charter.
2. Read `state.yaml`.
3. Read the current `docs/nysenate_crawler_algorithm_notes.md` before making implementation decisions.
4. Run the bundled GoalBuddy update checker when available and mention a newer version without blocking.
5. Work only on the active board task.
6. Preserve the updated handoff's Playwright-first and XML hierarchy constraints when selecting or executing work.
7. Write a compact task receipt.
8. Update the board.
9. When Judge finds XML/level gaps, convert the finding into the next bounded Worker package and continue the loop.
10. If safe local work remains, choose the next largest reversible Worker package and continue unless blocked.
11. Review at phase, risk, rejected-verification, ambiguity, or final-completion boundaries.
12. Finish only with a Judge/PM audit receipt that maps receipts and verification back to the original user outcome and records `full_outcome_complete: true`.
