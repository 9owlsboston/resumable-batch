# Handling failures

**What this is:** how to make a batch survive real-world failures — retry transient
blips (timeouts, HTTP 429), split oversized inputs, and stop cleanly on genuine bugs.
It builds on [getting started](getting-started.md); if "split / transient / fatal" is
new, skim [concepts](../reference/concepts.md#split-vs-transient-vs-fatal) first.

**Why it matters:** by default the engine is conservative — an unrecognized error is
**fatal** and aborts the run (checkpoints survive, so a re-run resumes). To get
automatic recovery you *teach* the engine which of your errors are transient and which
mean "input too big", by injecting three hooks.

---

## The routing rule

When your `execute` raises, the engine classifies the exception in this order:

1. **OOM?** (`is_oom(exc)`) → if a `split_policy` says to split, break the item into
   smaller children; otherwise **fatal**.
2. **Domain split?** (`split_policy.should_split(exc, item)`) → split, even for a
   non-OOM error (e.g. "shard too large").
3. **Transient?** (`is_transient(exc)`) → re-queue the **same** item with backoff, up
   to `retry.max_attempts`. Budget exhausted → **fatal**.
4. **Otherwise → fatal.** The run re-raises the original exception; every checkpoint
   written so far survives.

OOM is always evaluated **first and independently**, so an OOM whose message also
mentions "timeout" can never be mis-routed to the transient path.

---

## Retrying transient errors

Supply an `is_transient` classifier (or rely on the generic
[`default_is_transient`](../reference/api.md#default-classifiers)) and, optionally, a
tuned `RetryPolicy`.

```python
from resumable_batch import run_checkpointed, RetryPolicy

def is_transient(exc):
    text = str(exc).lower()
    return (isinstance(exc, (ConnectionError, TimeoutError))
            or "429" in text or "timed out" in text or "throttled" in text)

result = run_checkpointed(
    items, execute=execute, content_key_fn=ck, combine=combine, store=store,
    is_transient=is_transient,
    retry=RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=30.0),
)
```

A flaky item that raises `ConnectionError("connection reset")` once and then succeeds
is retried automatically and ends up committed; `result.transient_retries` counts how
many retries happened. Backoff is exponential with full jitter:
`delay = min(max_delay, base_delay * multiplier**(n-1))` ± `jitter`.

### Honoring a server `Retry-After`

If your service returns a `Retry-After`, extract it with `retry_after_fn`. The engine
validates it (finite, ≥ 0) and clamps against `max_server_delay`; a server delay above
that bound is **fatal** (a `RetryAfterError`) rather than silently honored.

```python
RetryPolicy(
    retry_after_fn=lambda exc: getattr(exc, "retry_after_seconds", None),
    max_server_delay=120.0,
)
```

---

## Splitting oversized inputs

When an item is too big to process (an out-of-memory error, or a domain "too large"
signal), split it into strictly-smaller children that are retried in its place.
Provide a `SplitPolicy`, and (usually) an `is_oom` classifier.

```python
from resumable_batch import (
    run_checkpointed, CheckpointItem, SplitPolicy, SchedMeta,
)

def is_oom(exc):
    return "out of memory" in str(exc).lower()

def should_split(exc, item):
    return len(item.payload["ids"]) > 1        # only split if there's room to shrink

def split(item):
    ids = item.payload["ids"]
    mid = len(ids) // 2
    return [
        CheckpointItem(payload={"ids": ids[:mid]}, sched=SchedMeta(target_size=mid)),
        CheckpointItem(payload={"ids": ids[mid:]},
                       sched=SchedMeta(target_size=len(ids) - mid)),
    ]

result = run_checkpointed(
    items, execute=execute, content_key_fn=ck, combine=combine, store=store,
    is_oom=is_oom,
    split_policy=SplitPolicy(should_split=should_split, split=split),
    split_size_fn=lambda item: len(item.payload["ids"]),
)
```

With a single item of `ids=[1,2,3,4]` whose `execute` OOMs on batches larger than 2,
the engine splits it into `[1,2]` and `[3,4]`, both of which succeed —
`result.splits == 1` and the final output covers all four ids. The split parent is
removed from the result set, so an old child and a later re-added parent can never both
end up in `combine`.

### The safety rails (enforced for you)

- **Strict progress** — when sizes are known (parent and all children report a size via
  `split_size_fn`), every child **must** be strictly smaller than the parent, or the
  split is fatal. No endless split chains.
- **Depth cap** — `max_split_depth` (default 64) bounds the chain.
- **Unsplittable OOM is fatal** — an OOM at the floor (`should_split` returns `False`,
  or a split can't shrink) aborts rather than looping forever.

`split_size_fn` defaults to reading `item.sched.target_size`; set that on your items
(or pass a custom `split_size_fn`) so strict-progress can be enforced.

---

## When you *want* it to stop

Not every error should be retried. A genuine bug, a permission error, or bad input
should fail fast. Leave those unclassified — they fall through to **fatal**, the run
re-raises the original exception, and your checkpoints survive so you can fix the code
and re-run without losing completed work. Don't classify an error as transient unless
retrying it could actually succeed.

---

## Concurrency and per-worker context

Failure handling composes with concurrency. Raise `concurrency` to run multiple items
in parallel, and use `worker_setup` / `worker_teardown` to build one expensive context
(a client, a connection pool) per worker thread — delivered as the `ctx` argument to
`execute`:

```python
result = run_checkpointed(
    items, execute=execute, content_key_fn=ck, combine=combine, store=store,
    concurrency=8,
    worker_setup=lambda: make_client(),
    worker_teardown=lambda client: client.close(),
)
```

A `worker_teardown` failure is logged and ignored — it never masks a real run error.

---

## See also

- [Concepts](../reference/concepts.md) — split/transient/fatal and the manifest.
- [API reference](../reference/api.md#failure-handling-optional) — every parameter and
  the `RetryPolicy` / `SplitPolicy` fields.
- [Design spec §4.2/§4.5](../design/checkpoint-library-generalization.md) — the retry
  and split correctness rules.
