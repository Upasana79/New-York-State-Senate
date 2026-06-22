# Surgical Overengineering Simplification

## Objective

Find and implement seven separate, surgical simplifications in `newyork_statues_scraper`, each targeting exactly one currently overengineered or unintuitive code area, while preserving intended essential behavior.

## Original Request

Review the repo from first principles, find overengineered or convoluted code that is not intuitive for human coders, and improve it with one small validated tweak at a time. Refine the goal so one loop finds the most triggering overengineering thing, surgically fixes that particular thing at once, avoids massive bulk rewrites, and completes seven such changes.

## Intake Summary

- Input shape: `vague`
- Audience: human maintainers of this repo
- Authority: `requested`
- Proof type: `test`
- Completion proof: seven Worker receipts, each showing one narrow simplification target, changed files, verification commands/results, and no loss of intended essential functionality, followed by a final audit with `full_outcome_complete: true`
- Goal oracle: after each loop, the diff is limited to one surgical simplification target, available verification passes or a credible equivalent is recorded, and the final audit confirms exactly seven validated simplification changes
- Likely misfire: making broad architecture rewrites, aesthetic refactors, or many tiny style edits instead of seven behavior-preserving simplifications that reduce real cognitive friction
- Blind spots considered: unknown current test coverage, possible scraper behavior tied to external sites, risk of changing behavior while making code look simpler, and risk of over-planning the cleanup itself
- Existing plan facts: the user wants seven changes; each change should be found and fixed in one loop; no bulk rewrites; tests or equivalent verification must confirm behavior is preserved

## Goal Oracle

The oracle for this goal is:

`Seven completed surgical Worker receipts exist; each receipt identifies one overengineered target, explains why it was unintuitive from first principles, lists a narrow diff for that one target, records verification preserving intended behavior, and the final Judge/PM audit records full_outcome_complete: true.`

The PM must keep comparing task receipts to this oracle. Planning, broad discovery, or one passing simplification is not enough. The goal finishes only when a final Judge/PM audit maps all seven receipts and verification back to this oracle.

## Goal Kind

`open_ended`

## Current Tranche

Complete exactly seven surgical simplification loops. Each loop should inspect just enough of the current repo to identify the single most valuable overengineering/convolution target at that moment, simplify only that target, verify behavior, and record proof before moving to the next loop.

## Non-Negotiable Constraints

- Do not perform massive rewrites, broad architecture changes, or bulk formatting.
- Each Worker loop may fix only one narrow target area.
- Preserve intended essential functionality.
- Prefer existing local patterns over new abstractions.
- Keep changes easy for human maintainers to understand.
- Run tests or the closest available verification after every loop.
- Stop a loop if the target would require touching unrelated areas or behavior is too ambiguous to preserve confidently.
- Do not edit GoalBuddy control files from Worker tasks.

## Stop Rule

Stop only when a final audit proves all seven simplification loops are complete and behavior-preserving evidence is recorded.

Do not stop after planning, discovery, or a single verified Worker package. Continue to the next surgical loop until seven valid simplification changes are complete or a genuine blocker prevents safe local progress.

## Slice Sizing

Each loop is one useful slice: find one target, simplify it, verify it, and stop. Small is not the point; narrow and behavior-preserving is the point.

Avoid turning one loop into multiple helper-only edits. Avoid combining unrelated cleanup into one loop. If two smells are near each other but independently understandable, fix only the stronger one and leave the other for a later loop.

## Canonical Board

Machine truth lives at:

`docs/goals/surgical-overengineering-simplification/state.yaml`

If this charter and `state.yaml` disagree, `state.yaml` wins for task status, active task, receipts, verification freshness, and completion truth.

## Run Command

```text
/goal Follow docs/goals/surgical-overengineering-simplification/goal.md.
```

## PM Loop

On every `/goal` continuation:

1. Read this charter.
2. Read `state.yaml`.
3. Work only on the active board task.
4. For each Worker loop, inspect only enough context to choose one current highest-leverage overengineering target.
5. Apply the smallest behavior-preserving simplification for that target.
6. Run the best available verification.
7. Record a compact receipt before advancing to the next loop.
8. Finish only with a Judge/PM audit receipt that maps seven verified simplification receipts back to the original user outcome and records `full_outcome_complete: true`.
