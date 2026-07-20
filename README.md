# resumable-batch

A domain-agnostic, content-addressed, crash-atomic **checkpoint + transient-retry
engine** that lets long-running *chunked* work resume idempotently instead of
discarding hours of progress on a single transient failure.

> **Current state:** see [`docs/current-state.md`](docs/current-state.md) for the
> dated "where we are now" snapshot. **The engine is implemented** — the
> orchestrator + Parquet/JSONL stores are ported behind the generalized API of
> `docs/design/checkpoint-library-generalization.md` (v3), with a Linux + Windows
> CI matrix. This README holds the durable orientation; the snapshot holds the
> moving picture.

## What / why

Long-running batch jobs (comb through millions of records via paged API calls, run
hundreds of chunked cluster queries) lose everything on one blip. This engine gives
each unit of work a **content-addressed identity**, persists each result
**crash-atomically** (manifest = the commit boundary), and classifies failures into
**split** (input too big) / **transient retry** (429/timeout) / **fatal** — so a
re-run resumes from the last good checkpoint and never ships a wrong or incomplete
result.

It was **extracted** from the proven implementation in `acr-analytics`
(`scripts/recipes/batch_checkpoint.py`, hardened over three rubber-duck rounds) into
a standalone, serialization-agnostic, cross-OS (Linux + Windows) library so multiple
consumers can depend on it outward:

- **Consumer #1** — Azure CUD right-sizing (the origin; the regression target).
- **Consumer #2** — a Microsoft Graph service-principal inventory crawler (>1M SPs).

## Install

```bash
python3 -m pip install -e '.[test]'   # editable + pytest/pandas/pyarrow
# runtime, with the parquet store:  pip install 'resumable-batch[parquet]'
```

The orchestrator core is stdlib-only; `pyarrow`/`pandas` are pulled only by the
`parquet` (and `test`) extra. The `JsonlResultStore` needs no third-party deps.

## Run / use

```python
from resumable_batch import (
    run_checkpointed, CheckpointItem, ParquetResultStore, make_lock,
)

lock = make_lock(cache_root / ".rb.lock").acquire()
try:
    store = ParquetResultStore(cache_root / "job", lock=lock, fingerprint=job_fp)
    result = run_checkpointed(
        items,                       # Iterable[CheckpointItem]
        execute=execute,             # (payload, ctx) -> result
        content_key_fn=content_key,  # payload -> (content_key, payload_hash)
        combine=combine,             # list[result] -> output  (OR reduce+finalize)
        store=store,
    )
    print(result.frame, result.committed_keys, result.resumed_keys)
finally:
    lock.release()
```

For a >1M-object crawl, swap `combine=` for `reduce=`/`finalize=` (incremental fold,
peak memory ≈ one result) and use `JsonlResultStore`. The authoritative
specification — API surface, the `ResultStore` boundary, the correctness contract,
the cross-OS shim, and the full test plan — is
[`docs/design/checkpoint-library-generalization.md`](docs/design/checkpoint-library-generalization.md).

## Test

```bash
python3 -m pytest        # Linux + Windows (CI runs both via .github/workflows/ci.yml)
```

## Layout

- `src/resumable_batch/` — the package (engine, stores, cross-OS lock/durability).
- `tests/` — ported CUD regression suite + generalized-feature tests.
- `docs/design/checkpoint-library-generalization.md` — **the spec** (accepted, v3).
- `AGENTS.md` — operating contract for AI agents (rules, run/test, docs map).
- `docs/current-state.md` — dated "where we are now" snapshot (README supplement).
- `docs/history/execution-log.md` — what was executed, when, and how verified.
