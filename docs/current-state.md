# Current state — resumable-batch

> **Snapshot:** 2026-07-20. The single, always-current answer to *"where is this
> project right now?"* — a **supplement to the README**, not a design-doc rollup.
> Keep the rest thin — one line per area + **links** to the authoritative topic
> docs. On any conflict, the linked topic doc wins. Update this doc and bump the
> snapshot date as the **last step** of any change that moves the current state.

## Summary

resumable-batch is a standalone, domain-agnostic Python library: a
content-addressed, crash-atomic **checkpoint + transient-retry engine** for
long-running chunked work. You give each unit of work an identity derived from its
payload; the engine persists each result atomically (a JSON manifest is the commit
boundary), skips already-done units on resume, and routes failures into
split / transient-retry / fatal — so a re-run continues from the last checkpoint
and never ships a wrong or incomplete result.

Where it stands today: **implemented (v0.1.0, unreleased).** The engine + Parquet
and JSONL stores are ported from the shipped `acr-analytics`
(`scripts/recipes/batch_checkpoint.py`) behind the generalized API in
`docs/design/checkpoint-library-generalization.md` (v3), with a Linux + Windows CI
matrix. The ported CUD (consumer #1) regression suite plus generalized-feature
tests are green (89 tests on Linux). A Microsoft Graph service-principal inventory
crawler (>1M SPs) is the intended second consumer (not yet built).

## Diagram

_No diagram yet — add one under `docs/diagrams/` when a topic doc needs it._

## Current state

- **Design** — accepted, v3 (rubber-duck passed, 11 findings closed). See
  [`docs/design/checkpoint-library-generalization.md`](design/checkpoint-library-generalization.md).
- **Docs** — newcomer on-ramp added: `guides/getting-started.md`,
  `guides/handling-failures.md`, `reference/concepts.md`, `reference/api.md`; README
  restructured to lead with a runnable example. Fills the previously-empty
  `guides/`/`reference/` buckets.
- **Implementation** — done. Package `src/resumable_batch/`: `engine`
  (`run_checkpointed`), `stores/{base,parquet,jsonl}`, cross-OS `locking` +
  `durability`, `model`, `classifiers`, `errors`.
- **Packaging / CI** — `pyproject.toml` (`parquet`/`test` extras) + Linux + Windows
  GitHub Actions matrix (`.github/workflows/ci.yml`). Repo profile grown `xs` → `m`.

## Future state / vision

An installable package that both `acr-analytics` (consumer #1) and the Graph
inventory crawler (consumer #2) depend on outward — no cross-consumer edge. Ships a
pluggable `ResultStore` (Parquet + JSONL), a bounded Retry-After hook, a cross-OS
lock + durability shim, and an optional incremental combine for very large crawls.

## Open gaps

- **Consumer migration** — cut `acr-analytics` over to the package (swap the two
  import sites, delete the in-repo `batch_checkpoint.py`) and build the Graph
  consumer (spec §5.2). Neither is done here.
- **Windows CI is authored but unverified locally** — the Windows lock/durability
  path (`msvcrt`, `GetDriveType`) is exercised only by the CI matrix, not on this
  Linux host.
- Unresolved product decisions (spec §8): final packaging (pip vs pinned git),
  Windows durability bar, async support, Graph delta-vs-full crawl.
