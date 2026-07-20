# Current state — resumable-batch

> **Snapshot:** 2026-07-20. The single, always-current answer to *"where is this
> project right now?"* — a **supplement to the README**, not a design-doc rollup.
> Keep the rest thin — one line per area + **links** to the authoritative topic
> docs. On any conflict, the linked topic doc wins. Update this doc and bump the
> snapshot date as the **last step** of any change that moves the current state.

## Summary

resumable-batch will be a standalone, domain-agnostic Python library: a
content-addressed, crash-atomic **checkpoint + transient-retry engine** for
long-running chunked work. You give each unit of work an identity derived from its
payload; the engine persists each result atomically (a JSON manifest is the commit
boundary), skips already-done units on resume, and routes failures into
split / transient-retry / fatal — so a re-run continues from the last checkpoint
and never ships a wrong or incomplete result.

Where it stands today: **spec stage.** The design is written and has passed the
plan-stage rubber-duck gate (two NO-GO rounds → accepted at v3) — see
`docs/design/checkpoint-library-generalization.md`. **No product code exists yet.**
The engine is being *extracted* from the proven, shipped implementation in
`acr-analytics` (`scripts/recipes/batch_checkpoint.py`); the next step is a fresh
implementation session that ports that code behind the generalized API in the spec,
with `acr-analytics` CUD right-sizing as the regression target and a Microsoft Graph
service-principal inventory crawler (>1M SPs) as the second consumer.

## Diagram

_No diagram yet — add one under `docs/diagrams/` when the implementation lands._

## Current state

- **Design** — accepted, v3 (rubber-duck passed, 11 findings closed). See
  [`docs/design/checkpoint-library-generalization.md`](design/checkpoint-library-generalization.md).
- **Implementation** — not started. Reference to port:
  `../acr-analytics/scripts/recipes/batch_checkpoint.py` + `tests/test_batch_checkpoint.py`.
- **Packaging / CI** — none yet (design mandates a Linux + Windows test matrix).

## Future state / vision

An installable package that both `acr-analytics` (consumer #1) and the Graph
inventory crawler (consumer #2) depend on outward — no cross-consumer edge. Ships a
pluggable `ResultStore` (Parquet + JSONL), a bounded Retry-After hook, a cross-OS
lock + durability shim, and an optional incremental combine for very large crawls.

## Open gaps

- Everything downstream of the spec: port the engine, add `JsonlResultStore`, the
  result-digest + generation binding, the domain-split trigger, the Retry-After
  bound, the Windows lock/durability shim, and the incremental combine.
- Unresolved product decisions (spec §8): final packaging (pip vs pinned git),
  Windows durability bar, async support, Graph delta-vs-full crawl.
- Repo is profile `xs` — grow to `m` (hooks + CI matrix) when implementation begins.
