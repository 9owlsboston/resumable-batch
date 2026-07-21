# Getting started

**What this is:** a copy-pasteable first run of `resumable-batch`. By the end you
will have run a batch, killed it mid-way, re-run it, and watched it **skip the work
it already finished** instead of starting over.

**Who it's for:** anyone with a long-running, chunked job (paged API crawls,
per-shard queries, per-record processing) who wants it to **resume idempotently**
after a crash, a timeout, or a `Ctrl-C` — without re-doing hours of completed work.

You do **not** need to know anything about the project's history to use it. If a term
is unfamiliar (`content_key`, `fingerprint`, `combine`, "split"), see
[concepts](../reference/concepts.md).

---

## Install

```bash
pip install resumable-batch            # core (stdlib-only) + the JSONL store
pip install 'resumable-batch[parquet]' # add the Parquet store (pandas/pyarrow)
```

This tutorial uses the **JSONL store**, which needs no third-party dependencies. If
your results are pandas DataFrames, use the Parquet store instead — see
[choosing a store](../reference/concepts.md#choosing-a-store).

---

## The five things you provide

The engine ships **no domain logic**. You wire in your job by supplying five small
pieces (only the first four are needed to start):

| You provide | What it does |
|---|---|
| **items** | your units of work — each is a `CheckpointItem(payload=...)`; the payload is anything you like |
| **`execute(payload, ctx)`** | does the actual work for one payload and returns its result |
| **`content_key_fn(payload)`** | derives a stable **identity** for a payload, so a re-run recognizes already-done work |
| **`combine(results)`** | stitches the per-item results into your final output |
| a **store** + a **lock** | where results are persisted, and a run-scoped exclusive lock |

## A complete, runnable example

Save this as `demo.py` and run it **twice**.

```python
import hashlib
from pathlib import Path

from resumable_batch import (
    run_checkpointed, CheckpointItem, JsonlResultStore, make_lock,
)

# 1. Your units of work. A payload can be any object you like.
items = [
    CheckpointItem(payload={"user_id": 1}),
    CheckpointItem(payload={"user_id": 2}),
    CheckpointItem(payload={"user_id": 3}),
]

# 2. Do the real work for ONE payload. Return a list[dict] (the JSONL result type).
def execute(payload, ctx):
    uid = payload["user_id"]
    return [{"user_id": uid, "score": uid * 10}]

# 3. Derive a STABLE identity from the payload. Return (content_key, payload_hash).
#    Here both are the same hash; see the concepts doc for when they differ.
def content_key_fn(payload):
    digest = hashlib.sha256(str(payload["user_id"]).encode()).hexdigest()
    return digest, digest

# 4. Stitch the per-item results into the final output.
def combine(results):
    rows = [row for result in results for row in result]
    return sorted(rows, key=lambda r: r["user_id"])

cache_root = Path("./.rb-cache")

# 5. Acquire ONE run-scoped lock, thread it into the store, always release it.
lock = make_lock(cache_root / ".rb.lock").acquire()
try:
    store = JsonlResultStore(cache_root / "job", lock=lock, fingerprint="demo-v1")
    result = run_checkpointed(
        items,
        execute=execute,
        content_key_fn=content_key_fn,
        combine=combine,
        store=store,
    )
finally:
    lock.release()

print("output:  ", result.output)
print("committed:", result.committed_keys, " resumed:", result.resumed_keys)
```

### First run

```
output:   [{'score': 10, 'user_id': 1}, {'score': 20, 'user_id': 2}, {'score': 30, 'user_id': 3}]
committed: 3  resumed: 0
```

All three items ran and were **committed** to the cache under `./.rb-cache`. (The
JSONL store round-trips each record with sorted keys, so `score` prints before
`user_id` — the values are unchanged.)

### Second run — this is the point

```
output:   [{'score': 10, 'user_id': 1}, {'score': 20, 'user_id': 2}, {'score': 30, 'user_id': 3}]
committed: 0  resumed: 3
```

Nothing executed. All three were **resumed** (served from the cache) — `execute`
was never called. Same output, zero recomputation. That is idempotent resume.

---

## Now crash it mid-way

Resume only matters when a run **doesn't finish**. Simulate a crash on the third
item, then re-run cleanly:

```python
def execute_that_crashes(payload, ctx):
    if payload["user_id"] == 3:
        raise RuntimeError("boom")           # simulate a crash / kill / OOM
    return [{"user_id": payload["user_id"], "score": payload["user_id"] * 10}]
```

Run once with `execute_that_crashes` (it raises, and the run aborts **after the
first two items were already committed**). Run again with the normal `execute`:

```
committed: 1  resumed: 2
```

Items 1 and 2 were checkpointed before the crash, so the re-run **resumes** them and
only executes item 3. No progress was lost. Checkpoints always survive a failure so a
re-run continues from the last good state.

> **Why `RuntimeError` aborted instead of retrying:** by default an unknown error is
> **fatal** — the engine never retries something it doesn't recognize as transient.
> To make timeouts/429s retry automatically, and big inputs split, see
> [handling failures](handling-failures.md).

---

## Reading the result

`run_checkpointed` returns a [`CheckpointResult`](../reference/api.md#checkpointresult):

| Field | Meaning |
|---|---|
| `.output` (alias `.frame`) | your `combine(...)` output |
| `.committed_keys` | items executed and persisted **this run** |
| `.resumed_keys` | items served from the cache (skipped) **this run** |
| `.transient_retries` | how many retries happened |
| `.splits` | how many oversized items were split |
| `.attempts` | total `execute` calls |

---

## What changes the cache identity

Two knobs decide whether a re-run resumes or recomputes:

- **`content_key_fn(payload)`** — if it returns the **same** key for a payload, that
  payload is considered "the same work" and resumes. Derive the key from the inputs
  that define the work (an ID, a canonical set of members) — not from wall-clock time
  or anything that varies run-to-run.
- **`fingerprint`** (the store argument) — a version stamp for the whole cache.
  **Change it and the entire cache is invalidated** (everything recomputes). Bump it
  when your `execute` logic changes in a way that makes old results wrong.

Details and the full rationale: [concepts](../reference/concepts.md).

---

## Next steps

- **[Concepts](../reference/concepts.md)** — the vocabulary (`content_key` vs
  `payload_hash`, `fingerprint`, manifest, combine vs reduce, split vs transient) and
  how to choose a store.
- **[Handling failures](handling-failures.md)** — make timeouts/429s retry, make
  oversized inputs split, and set a retry policy.
- **[API reference](../reference/api.md)** — every `run_checkpointed` parameter, the
  `ResultStore` contract, and the error types.
- **[Design spec](../design/checkpoint-library-generalization.md)** — the *why*: the
  correctness contract, the crash-atomic commit protocol, and the cross-OS shim.
