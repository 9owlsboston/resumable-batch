# Concepts

**What this is:** the vocabulary of `resumable-batch` — the handful of terms you
meet in [getting started](../guides/getting-started.md) and the
[API reference](api.md), defined for someone new to the library. Read this once and
the rest of the docs stop being cryptic.

**Why it exists:** the engine is deliberately domain-agnostic, so its power lives in
a few abstract concepts (identity, fingerprint, the manifest, split vs. transient).
This page is the single place they are explained in plain language.

---

## The mental model in one paragraph

You hand the engine a list of **items**. For each item it derives a **content_key**
(a stable identity) and runs your **`execute`**. Each successful result is written to
a **store** and recorded in a **manifest** — the manifest entry is the moment the work
"counts as done". On a re-run, any item whose key is already in the manifest (and
whose stored result still verifies) is **resumed** (skipped); everything else runs.
At the end, your **`combine`** stitches all the results into one output.

---

## Terms

### item (`CheckpointItem`)
One unit of work: `CheckpointItem(payload=...)`. The `payload` is **opaque to the
engine** — a dict, a list, a dataclass, whatever your `execute` understands. Items
are immutable.

### `execute(payload, ctx)`
Your function that does the actual work for one payload and returns a **result**. The
`ctx` argument is a per-worker context object (e.g. a DB connection) or `None`; see
[worker_setup](api.md#worker_setup--worker_teardown). The engine never inspects the
result — it just hands it to the store and, later, to `combine`.

### content_key and payload_hash
`content_key_fn(payload)` returns a **pair** `(content_key, payload_hash)`:

- **`content_key`** is the item's **identity** — its dedup/resume key. Two payloads
  with the same `content_key` are treated as the same work; the second one resumes
  from the first's result. It becomes part of a filename, so return a filesystem-safe
  string (a hex digest is the safe default).
- **`payload_hash`** is a **guard**: a hash of the *full* payload. The engine stores
  it alongside the result and, on resume, checks that the payload behind a given key
  still hashes the same. If a buggy `content_key_fn` ever returns the same key for two
  genuinely different payloads (a truncated-hash collision, say), the mismatch is
  caught loudly instead of silently serving the wrong cached result.

For most jobs the two are **the same value** (hash the whole canonical payload, use it
for both). They differ only when your `content_key` is intentionally coarser than the
full payload — e.g. a short/opaque key with a separate full-payload hash as the guard.

> **Derive the key from what defines the work** — an ID, a canonical (sorted, de-duped)
> set of members. Never fold in wall-clock time, random values, or anything that
> varies run-to-run, or nothing will ever resume.

### fingerprint
A version stamp for the **entire cache**, passed to the store. The store refuses an
empty fingerprint. If the fingerprint on disk doesn't match the one you pass, the
**whole cache is invalidated** and everything recomputes. Bump it whenever your
`execute` logic changes such that previously cached results would now be wrong.
`content_key` scopes *one item*; `fingerprint` scopes *the whole job*.

### store (`ResultStore`)
Where results are persisted durably. The engine talks to it only through the
[`ResultStore`](api.md#resultstore-protocol) protocol (`has` / `commit` / `load` /
`load_all` / ...). Two are built in: `ParquetResultStore` and `JsonlResultStore` (see
[choosing a store](#choosing-a-store)). A store **requires a held run-scoped lock** and
a fingerprint at construction — it never acquires its own lock.

### manifest — the commit boundary
A small JSON file (`manifest.json`) in the cache dir that is the **source of truth** for
what is done. An item is "done" only once its entry lands in the manifest. Each commit
also binds a per-commit **generation id** plus a **result-content digest, byte-length,
and record-count** into both the result file and the manifest entry. On resume, a `has()`
hit is accepted **only** after the stored result is read back and all of those verify —
so a truncated, torn, or stale result is treated as a **miss** (recompute), never served
as a wrong or incomplete hit. A missing or malformed manifest invalidates the whole cache.
Full protocol: [design §4.4](../design/checkpoint-library-generalization.md).

### lock
A single OS-owned exclusive lock (`make_lock(path)`) held for the **duration of one
run** and threaded into the store. It lives *outside* the cache tree and releases
automatically when the process dies, so two runs can't corrupt one cache. Acquire it
once, `try/finally` release it. See [locking](api.md#locking).

### combine vs. reduce/finalize
Two mutually exclusive ways to produce the final output from the per-item results —
provide **exactly one**:

- **`combine(results)`** — receives the **whole list** of results at once and returns
  the output. Simplest; use it when the full set fits comfortably in memory.
- **`reduce(acc, result)` + `finalize(acc)`** — an **incremental fold**: results are
  folded one at a time so peak memory is ~one result. Use it for very large crawls
  (e.g. millions of records) where materializing everything would blow up memory.

### split vs. transient vs. fatal
When `execute` raises, the engine classifies the failure into one of three routes
(see [handling failures](../guides/handling-failures.md)):

- **split** — the input is *too big* (e.g. an out-of-memory error, or a domain "shard
  too large" signal). If you supplied a `SplitPolicy`, the item is broken into
  strictly-smaller children that are retried in its place. Every split must make
  progress (children strictly smaller, bounded depth) or it becomes fatal.
- **transient** — a temporary blip (timeout, HTTP 429, connection reset). The **same**
  item is re-queued with bounded exponential backoff, up to `RetryPolicy.max_attempts`.
- **fatal** — anything else, or a transient that exhausted its retry budget, or an
  unsplittable OOM. The run aborts and re-raises — **but every checkpoint written so
  far survives**, so a re-run resumes.

By default (no classifiers supplied) only a small set of generic timeout/OOM message
patterns are recognized; everything else is fatal. You inject `is_oom`, `is_transient`,
and `split_policy` to teach the engine your domain's errors.

---

## Choosing a store

| Your result is… | Use | Result type | Extra deps |
|---|---|---|---|
| a pandas **DataFrame** (tabular) | `ParquetResultStore` | `pd.DataFrame` | `resumable-batch[parquet]` (pandas + pyarrow) |
| a **list of JSON objects** | `JsonlResultStore` | `list[dict]` | none (stdlib only) |

Both give you identical correctness guarantees (atomic commit, generation binding,
completeness digest). Pick by the shape of your result:

- **Parquet** additionally records an Arrow **schema fingerprint** and can enforce an
  `expected_schema` column set — handy for typed tabular data.
- **JSONL** stores one object per line with a metadata header; its completeness rests
  on the digest/length/count plus an optional `record_validator`.

If your results are neither, implement the [`ResultStore`](api.md#resultstore-protocol)
protocol (or subclass `AtomicManifestStore`, which already owns all the identity and
atomicity logic — you only serialize/deserialize one result).

---

## See also

- [Getting started](../guides/getting-started.md) — these concepts in a runnable job.
- [Handling failures](../guides/handling-failures.md) — split/transient/fatal in depth.
- [API reference](api.md) — the exact signatures.
- [Design spec](../design/checkpoint-library-generalization.md) — the correctness
  contract and crash-atomicity proof sketch.
