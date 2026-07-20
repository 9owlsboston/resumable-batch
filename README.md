# resumable-batch

A domain-agnostic, content-addressed, crash-atomic **checkpoint + transient-retry
engine** that lets long-running *chunked* work resume idempotently instead of
discarding hours of progress on a single transient failure.

> **Current state:** see [`docs/current-state.md`](docs/current-state.md) for the
> dated "where we are now" snapshot. **This repo is at spec stage** — the design is
> accepted (`docs/design/checkpoint-library-generalization.md`, v3, rubber-duck
> passed); the implementation is **not started yet**. This README holds the durable
> orientation; the snapshot holds the moving picture.

## What / why

Long-running batch jobs (comb through millions of records via paged API calls, run
hundreds of chunked cluster queries) lose everything on one blip. This engine gives
each unit of work a **content-addressed identity**, persists each result
**crash-atomically** (manifest = the commit boundary), and classifies failures into
**split** (input too big) / **transient retry** (429/timeout) / **fatal** — so a
re-run resumes from the last good checkpoint and never ships a wrong or incomplete
result.

It is being **extracted** from the proven implementation in `acr-analytics`
(`scripts/recipes/batch_checkpoint.py`, hardened over three rubber-duck rounds) into
a standalone, serialization-agnostic, cross-OS (Linux + Windows) library so multiple
consumers can depend on it outward:

- **Consumer #1** — Azure CUD right-sizing (the origin; the regression target).
- **Consumer #2** — a Microsoft Graph service-principal inventory crawler (>1M SPs).

## Run / use

**Not implemented yet.** The authoritative specification — API surface, the
`ResultStore` boundary, the correctness contract, the cross-OS shim, and the full
test plan — is [`docs/design/checkpoint-library-generalization.md`](docs/design/checkpoint-library-generalization.md).
Start there. The reference implementation to port lives at
`../acr-analytics/scripts/recipes/batch_checkpoint.py` (+ `tests/test_batch_checkpoint.py`).

## Layout

- `docs/design/checkpoint-library-generalization.md` — **the spec** (accepted, v3).
- `AGENTS.md` — operating contract for AI agents (rules, run/test, docs map).
- `docs/current-state.md` — dated "where we are now" snapshot (README supplement).
- `docs/history/execution-log.md` — what was executed, when, and how verified.
