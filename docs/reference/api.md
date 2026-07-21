# API reference

**What this is:** the exact public surface of `resumable-batch` — every
`run_checkpointed` parameter, the `ResultStore` contract, the built-in stores, the
locking helpers, and the error types. For the *ideas* behind these names, read
[concepts](concepts.md); for a runnable walkthrough, [getting started](../guides/getting-started.md).

Everything below is importable from the top-level package:

```python
from resumable_batch import run_checkpointed, CheckpointItem, JsonlResultStore, ...
```

The authoritative behavioral spec is
[`docs/design/checkpoint-library-generalization.md`](../design/checkpoint-library-generalization.md);
this page is the quick lookup.

---

## `run_checkpointed(items, *, ...) -> CheckpointResult`

Runs keyed work items with durable checkpoints, bounded transient retry, optional
domain-defined split, and an optional incremental combine.

### Required

| Parameter | Type | Purpose |
|---|---|---|
| `items` | `Iterable[CheckpointItem]` | the units of work |
| `execute` | `(payload, ctx) -> result` | does the work for one payload; `ctx` is the worker context or `None` |
| `content_key_fn` | `payload -> (content_key, payload_hash)` | derives the item's identity + guard hash (see [concepts](concepts.md#content_key-and-payload_hash)) |
| `store` | `ResultStore` | durable result cache (constructed with a held lock + fingerprint) |
| **one combine mode** | see below | **exactly one** of `combine` **or** (`reduce` + `finalize`) |

### Combine mode (provide exactly one)

| Parameter | Type | Purpose |
|---|---|---|
| `combine` | `list[result] -> output` | stitch all results at once (materializing) |
| `reduce` | `(acc, result) -> acc` | incremental fold step (peak memory ≈ one result) |
| `finalize` | `acc -> output` | finish the fold; required with `reduce` |
| `initial` | `object` | starting accumulator for the fold (default `None`) |

Passing both modes, or `reduce` without `finalize` (or vice-versa), raises `ValueError`.

### Failure handling (optional)

| Parameter | Type | Default | Purpose |
|---|---|---|---|
| `is_oom` | `Exception -> bool` | `default_is_oom` | classify an out-of-memory failure (routed to split) |
| `is_transient` | `Exception -> bool` | `default_is_transient` | classify a retriable blip |
| `split_policy` | `SplitPolicy \| None` | `None` | when/how to split an oversized item |
| `retry` | `RetryPolicy` | `RetryPolicy()` | backoff + attempt budget (see below) |
| `split_size_fn` | `CheckpointItem -> int` | uses `sched.target_size` | size used to enforce strict-shrink on split |
| `max_split_depth` | `int` | `64` | hard cap on the split chain |

See [handling failures](../guides/handling-failures.md) for the routing rules and
examples.

### Concurrency & worker lifecycle (optional)

| Parameter | Type | Default | Purpose |
|---|---|---|---|
| `concurrency` | `int` | `1` | max in-flight `execute` calls (thread pool) |
| `worker_setup` | `() -> ctx \| None` | `None` | build a per-worker context once (e.g. a client/connection) |
| `worker_teardown` | `(ctx) -> None \| None` | `None` | tear each context down at the end |
| `cleanup_on_success` | `bool` | `False` | delete the whole cache dir after a fully successful run |

### Testing hooks (optional)

`sleep`, `monotonic`, and `rng` are injectable (`time.sleep`, `time.monotonic`, a
fresh `random.Random` by default) so retry backoff is deterministic under test.

### Returns

A [`CheckpointResult`](#checkpointresult). On a fatal failure it **raises the original
exception** — checkpoints written so far survive, so a re-run resumes.

---

## Data model

### `CheckpointItem(payload, sched=SchedMeta())`
Immutable unit of work. `payload` is opaque to the engine. `sched` is optional
scheduling/telemetry metadata — **never** an identity.

### `SchedMeta(root_order=0, range_start=0, range_end=0, target_size=0)`
Scheduling/telemetry only. `target_size` is the batch size this attempt runs at and is
what the default `split_size_fn` reads to enforce strict-shrink on split.

### `CheckpointResult`
The run output plus telemetry.

| Field | Meaning |
|---|---|
| `frame` / `output` | the `combine`/`finalize` output (`.output` is an alias) |
| `attempts` | total `execute` calls (successful + retried + split) |
| `committed_keys` | distinct keys persisted **this run** |
| `resumed_keys` | keys served from cache (skipped) **this run** |
| `transient_retries` | number of transient retries performed |
| `splits` | number of items split |

### `RetryPolicy(...)`
Bounded exponential backoff with full jitter, plus an optional server-directed delay.

| Field | Default | Meaning |
|---|---|---|
| `max_attempts` | `4` | total tries per key on transient error |
| `base_delay` | `2.0` | first backoff, seconds |
| `max_delay` | `60.0` | per-attempt cap before jitter |
| `multiplier` | `2.0` | exponential factor: `base * multiplier**(n-1)` |
| `jitter` | `0.5` | ± fraction of the computed delay |
| `retry_after_fn` | `None` | `Exception -> float \| None`; extract a server `Retry-After` |
| `max_server_delay` | `300.0` | operational ceiling; a server delay above it is **fatal** (must be finite ≥ 0) |

### `SplitPolicy(should_split, split)`
Domain-defined split trigger + splitter.

- `should_split(exc, item) -> bool` — split this item on this failure? (OOM *or* a
  non-OOM domain condition like "shard too big").
- `split(item) -> list[CheckpointItem]` — the strictly-smaller children. The engine
  enforces strict progress and the depth cap regardless.

### Constants
`SCHEMA_VERSION` (on-disk format version; bumped on breaking format changes) and
`MANIFEST_NAME` (`"manifest.json"`).

---

## `ResultStore` protocol

The engine's only contract with a durable cache. Implement this (or subclass
`AtomicManifestStore`, which already owns identity/atomicity) for a custom result type.

| Member | Purpose |
|---|---|
| `cached_keys -> set[str]` | keys currently in the manifest |
| `has(content_key, expected_payload_hash) -> bool` | is this key committed **and** does its stored result fully verify? |
| `load(content_key, expected_payload_hash) -> result` | read one verified result |
| `commit(content_key, payload_hash, result) -> None` | atomically persist one result |
| `load_all(key_to_hash) -> list[result]` | read all (for `combine`) |
| `iter_results(key_to_hash) -> Iterator[result]` | stream all (for `reduce`) |
| `cleanup() -> None` | delete the cache dir |

### `AtomicManifestStore`
The base that implements **all** identity, atomicity, result-digest, and
generation-binding logic. Subclasses only serialize/deserialize one result and do
store-specific structural validation. Constructor:

```python
AtomicManifestStore(cache_dir, *, lock, fingerprint, fs_policy=FsPolicy.FAIL_CLOSED)
```

Raises `RuntimeError` if `lock` is missing or not held, and `ValueError` on an empty
`fingerprint`.

### `ParquetResultStore(cache_dir, *, lock, fingerprint, expected_schema=None, fs_policy=...)`
Result type `pd.DataFrame`. Records an Arrow schema fingerprint; `expected_schema`
(a `pyarrow.Schema` or an iterable of column names) enforces the column set on commit
and read. Requires the `parquet` extra.

### `JsonlResultStore(cache_dir, *, lock, fingerprint, record_validator=None, fs_policy=...)`
Result type `list[dict]`. One JSON object per line after a metadata header. Optional
`record_validator(obj) -> bool` rejects malformed records. Stdlib-only.

---

## Locking

```python
from resumable_batch import make_lock

lock = make_lock(lockfile, timeout=30.0, poll=0.5).acquire()
try:
    ...   # construct store(s) with lock=lock, run_checkpointed(...)
finally:
    lock.release()
```

- **`make_lock(lockfile, **kwargs) -> LockProvider`** — returns the OS-appropriate
  lock (`PosixFlockLock` on POSIX, `WindowsLock` on Windows). Both are OS-owned handle
  locks that release when the process dies (no TTL takeover, no two-writer window).
- Also usable as a context manager: `with make_lock(path): ...`.
- Put the lockfile **outside** the cache dir so a cache wipe never touches the lock
  inode. `.acquire()` raises `CacheLockedError` if another process holds it past
  `timeout`.

---

## Durability policy

The default cache is enabled only on a known-durable, allowlisted filesystem
(ext/xfs/ntfs); elsewhere it fails closed. Correctness never depends on FS strength —
it's carried by the generation binding + result digest.

- **`fs_cache_enabled(path, policy=FsPolicy.FAIL_CLOSED) -> bool`** — call this
  **before** building the store to decide whether a durable cache is usable.
- **`FsPolicy`** — `FAIL_CLOSED` (default: disable caching off-allowlist), `REQUIRE`
  (hard-error), `ALLOW` (dev best-effort).
- **`resolve_fs_type(path) -> str`**, **`DURABLE_FS_ALLOWLIST`** — introspection.

---

## Default classifiers

- **`default_is_oom(exc)`** — matches generic OOM markers (`out of memory`,
  `memory exceeded`, `low memory`, `e_runaway_query`).
- **`default_is_transient(exc)`** — `True` for `ConnectionError` / `TimeoutError` /
  `socket.timeout`, and messages containing `timed out`, `timeout`,
  `temporarily unavailable`, `connection reset`, `service unavailable`, `throttl`.

Inject your own for domain-specific errors (see
[handling failures](../guides/handling-failures.md)).

---

## Errors

| Exception | Raised when |
|---|---|
| `CacheLockedError` | the run-scoped lock is held by another process past `timeout` |
| `ContentKeyCollisionError` | two live items share a `content_key` but differ in `payload_hash` (a buggy `content_key_fn` or a hash collision) |
| `FsUnsupportedError` | `FsPolicy.REQUIRE` and the cache path is on a non-durable filesystem |
| `RetryAfterError` | a server `Retry-After` is malformed or exceeds `max_server_delay` |
