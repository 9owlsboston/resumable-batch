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

Long-running batch jobs — comb through millions of records via paged API calls, run
hundreds of chunked cluster queries — lose everything on one blip. This engine gives
each unit of work a **content-addressed identity**, persists each result
**crash-atomically** (a manifest is the commit boundary), and classifies failures
into **split** (input too big) / **transient retry** (429/timeout) / **fatal** — so a
re-run resumes from the last good checkpoint and never ships a wrong or incomplete
result.

You supply the domain logic (what one unit of work does, how to identify it, how to
stitch results); the engine owns the resilience. It's serialization-agnostic and
cross-OS (Linux + Windows).

## Install

```bash
pip install resumable-batch            # core (stdlib-only) + the JSONL store
pip install 'resumable-batch[parquet]' # add the Parquet store (pandas/pyarrow)
```

The orchestrator core is stdlib-only; `pyarrow`/`pandas` are pulled only by the
`parquet` (and `test`) extra. The `JsonlResultStore` needs no third-party deps.

## Quick example

```python
import hashlib
from pathlib import Path
from resumable_batch import (
    run_checkpointed, CheckpointItem, JsonlResultStore, make_lock,
)

items = [CheckpointItem(payload={"user_id": i}) for i in (1, 2, 3)]

def execute(payload, ctx):                       # do the work for ONE payload
    uid = payload["user_id"]
    return [{"user_id": uid, "score": uid * 10}]

def content_key_fn(payload):                     # a STABLE identity for resume
    digest = hashlib.sha256(str(payload["user_id"]).encode()).hexdigest()
    return digest, digest

def combine(results):                            # stitch the per-item results
    return sorted((row for r in results for row in r), key=lambda x: x["user_id"])

cache_root = Path("./.rb-cache")
lock = make_lock(cache_root / ".rb.lock").acquire()
try:
    store = JsonlResultStore(cache_root / "job", lock=lock, fingerprint="demo-v1")
    result = run_checkpointed(
        items, execute=execute, content_key_fn=content_key_fn,
        combine=combine, store=store,
    )
    print(result.output, "committed:", result.committed_keys,
          "resumed:", result.resumed_keys)
finally:
    lock.release()
```

Run it once and all three items execute (`committed: 3`); run it again and all three
**resume** from the cache (`committed: 0, resumed: 3`) — `execute` is never called.
The [getting-started guide](docs/guides/getting-started.md) walks through this and a
crash-and-resume demo.

For a >1M-object crawl, swap `combine=` for `reduce=`/`finalize=` (incremental fold,
peak memory ≈ one result).

## Documentation

- **[Getting started](docs/guides/getting-started.md)** — a runnable first job +
  crash/resume demo. Start here.
- **[Concepts](docs/reference/concepts.md)** — the vocabulary (`content_key`,
  `fingerprint`, manifest, combine vs. reduce, split vs. transient) and store choice.
- **[Handling failures](docs/guides/handling-failures.md)** — retry, split, and
  classifiers.
- **[API reference](docs/reference/api.md)** — every parameter, the `ResultStore`
  contract, and the error types.
- **[Design spec](docs/design/checkpoint-library-generalization.md)** — the
  authoritative correctness contract, commit protocol, and cross-OS shim.

## Test

```bash
python3 -m pip install -e '.[test]'   # editable install + pytest/pandas/pyarrow
python3 -m pytest                     # Linux + Windows (CI runs both)
```

## Provenance

Extracted from the proven implementation in `acr-analytics`
(`scripts/recipes/batch_checkpoint.py`, hardened over three rubber-duck rounds) into
a standalone library so multiple consumers can depend on it outward:

- **Consumer #1** — Azure CUD right-sizing (the origin; the regression target).
- **Consumer #2** — a Microsoft Graph service-principal inventory crawler (>1M SPs).

## Layout

- `src/resumable_batch/` — the package (engine, stores, cross-OS lock/durability).
- `tests/` — ported CUD regression suite + generalized-feature tests.
- `docs/guides/` — how-to docs ([getting started](docs/guides/getting-started.md),
  [handling failures](docs/guides/handling-failures.md)).
- `docs/reference/` — [concepts](docs/reference/concepts.md) +
  [API reference](docs/reference/api.md).
- `docs/design/checkpoint-library-generalization.md` — **the spec** (accepted, v3).
- `AGENTS.md` — operating contract for AI agents (rules, run/test, docs map).
- `docs/current-state.md` — dated "where we are now" snapshot (README supplement).
- `docs/history/execution-log.md` — what was executed, when, and how verified.
