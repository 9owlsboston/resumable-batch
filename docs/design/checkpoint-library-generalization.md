# Generalizing the batch-checkpoint engine into a standalone resilience library

> **Status:** Proposed — **v3 (plan stage)**. Closes the v1 (7-blocker) and v2
> (4-blocker) plan-stage rubber-duck NO-GOs; see §0 / §0b. Docs-only; this stage
> writes no product code. Intended as the final revision before GO; awaiting user
> go-ahead before implementer handoff.
> **Date:** 2026-07-20 (v3 revision)
> **Ledger:** _to be assigned_ (log as a `repo:acr-analytics` issue on go-ahead)
> **Scope (proposed product change, next stage):** extract the domain-agnostic
> orchestrator now living at `scripts/recipes/batch_checkpoint.py` into a
> **standalone, installable, serialization-agnostic, cross-OS** library
> (working name **`chkpt`** — final name in §9), leaving `acr-analytics` as
> **consumer #1** (unchanged behavior) and enabling a Microsoft Graph
> service-principal inventory crawler (>1M SPs) as **consumer #2**.
> **Builds on / supersedes as SoT for the engine:**
> `docs/design/batch-checkpoint-retry.md` (I-D55T, the in-repo implementation) and
> `docs/design/batch-execution-architecture.md`. Those stay authoritative for the
> *CUD-specific* wiring; this doc owns the *generalized engine contract*.

---

## 0. Changelog — v2 (closes v1 rubber-duck NO-GO)

v1 was NO-GO with 7 blockers — verified against the code, all real. v2 closes them.

- **B1 — Graph shard key was an unsupported filter.** v1's `startsWith(id, prefix)`
  example is **not** a supported Microsoft Graph `/servicePrincipals` `$filter`
  (Graph's advanced-query matrix doesn't support `startsWith` on `id`), so it would
  error or silently full-scan. v2 stops prescribing a filter: the consumer MUST
  pick a **server-supported, provably-disjoint, exhaustive** partition, else fall
  back to the **durable-cursor** pattern (checkpoint the opaque `nextLink`/
  `deltaLink` per shard). This makes explicit that the engine has **two resume
  grains** — content-addressed *sets* (CUD) and durable *cursors* (Graph if no
  clean partition exists) — see §4.7. (§5.2, §4.7)
- **B2 — "split on timeout" is impossible with the OOM-only split branch.**
  Verified: `_handle_split` is reachable ONLY inside `if is_oom(exc)`
  (`batch_checkpoint.py:811-822`); a transient timeout is re-queued on the **same
  key**, never split. v2 **generalizes the split trigger** into a domain-defined
  `should_split(exc, item)` checked *before* the transient branch, independent of
  OOM, with defined precedence + floor. (§4.5)
- **B3 — JSONL could silently ship an INCOMPLETE result.** `payload_hash`
  identifies the **input**, not the serialized output; a JSONL file truncated
  cleanly between records (or missing records) passes every v1 check. v2 adds a
  **result-content digest + byte-length + record-count** to the store contract for
  **both** stores, validated on load before a hit is accepted. (§4.1, §4.4)
- **B4 — the two-rename "transaction" can expose a stale generation.** Without
  durable directory ordering, power loss can keep the **new manifest entry** but
  lose the **payload rename**, leaving an OLDER payload file under the SAME
  content-addressed filename (a live hazard for **mutable sources like Graph**,
  where re-crawling a shard yields different objects under the same key). v2 binds
  a **per-commit generation id + result digest into BOTH the manifest entry and
  the payload file**, and rejects a load whose payload generation/digest ≠ the
  manifest's. This closes the torn-write window on **every** OS. (§4.4)
- **B5 — TTL stale-takeover permits two live writers.** A long-running or
  suspended legitimate owner exceeding a TTL would have its lock stolen → two
  writers corrupt one cache. v2 **drops TTL takeover**: the Windows lock is an
  **OS-owned handle lock** (`msvcrt.locking` on a held fd) that releases on process
  death, exactly matching POSIX `flock` semantics. No lease/fencing needed. (§4.3)
- **B6 — `load_all()` materializes the whole inventory** → a hard memory blocker
  for >1M SPs (smaller shards don't lower final peak). Promoted from an open
  question to a **first-class requirement**: an optional **incremental
  reduce/streaming-combine** path (`reduce(acc, result)` + `finalize`, plus a
  `sink`-based write-through) so the Graph consumer never holds the full set. CUD
  keeps the list-combine (its result is small). (§4.6)
- **B7 — `Retry-After` was unbounded external input** flowing into heap deadlines
  and `sleep`. v2 **validates** it (finite, ≥ 0) and clamps via a new
  `retry.max_server_delay`; a server delay above the operational bound **fails
  clearly** rather than sleeping/overflowing. (§4.2)
- **Folded notes:** legacy-cache compat fixture + the schema-bump consequence for
  §6.2 (N1); Graph crawl-epoch/consistency fingerprinting (N2). See §6.2, §5.2, §7.

---

## 0b. Changelog — v3 (closes v2 rubber-duck NO-GO)

v2 closed the 7 v1 blockers but the confirmation pass found 4 residual defects in
the *v2 fixes themselves*. v3 closes them.

- **C1 — digest was self-referential + verified too late (was B3/B4).** v2 hashed
  `serialized_bytes` and then embedded that digest *into* those bytes (changing
  them), and relied on `load()` to catch corruption — but the orchestrator skips
  `execute` when `has()` returns True, so a `load()`-time failure can't trigger
  recompute. v3: the digest covers a **non-self-referential scope** — the result
  *records/rows only*, never the metadata block that carries the digest (parquet
  key-value metadata and the JSONL trailer live outside the digested region) — and
  **`has()` performs the FULL digest/length/count verification (reading the body)
  before returning True**. A cache hit is never accepted, and execution never
  skipped, on an unverified body. Shards are small, so the read cost is bounded.
  (§4.4)
- **C2 — unsplittable OOM must stay FATAL; splitting must make strict progress
  (was B2).** v2's precedence let a non-split OOM fall through to *transient*,
  regressing CUD (current code makes unsplittable OOM fatal,
  `batch_checkpoint.py:811-822`), and "above floor" didn't guarantee children
  shrink. v3 restores an **explicit unsplittable-OOM → FATAL** branch, adds a
  **separate non-OOM domain-split** branch (Graph timeout/too-big), and **requires
  every split to strictly reduce** (children strictly smaller/fewer) under a
  **split-depth/budget cap** — no endless split chain. (§4.5)
- **C3 — `max_server_delay` itself was unvalidated (was B7).** An `inf` bound makes
  the Retry-After clamp meaningless. v3 requires `max_server_delay` **finite,
  ≥ 0, and operationally bounded**, validated at `RetryPolicy` construction. (§4.2)
- **C4 — standalone `sink` mode was undefined (was B6).** v2's "and/or sink" left
  resume/dedup unspecified. v3 **removes** standalone sink; the only incremental
  mode is `reduce(acc, result) + finalize(acc)`. A write-through sink is
  implemented *inside* `reduce`; resume idempotency holds because the engine
  iterates **each terminal key exactly once per `run()`** (cached + freshly
  committed alike), so `reduce` sees the complete terminal set exactly once. (§4.6)

---

## 1. Summary

`batch_checkpoint.py` is already a domain-agnostic engine — it knows nothing about
ADX/KQL — but it is (a) **physically trapped** inside an analytics repo, (b)
**hard-wired to pandas + parquet** as the result representation, and (c)
**Linux-only** (POSIX `fcntl.flock`, `libc statfs`, directory `fsync`). Two things
now want it: the shipped CUD right-sizing path and a new Graph SP-inventory
crawler. Two independent consumers is the bar for promoting shared infrastructure.

This doc proposes extracting the engine into its own repo/package with a set of
targeted changes that **strengthen** the existing correctness contract rather than
weaken it:

1. A **pluggable `ResultStore`** so results aren't forced to be pandas DataFrames
   (parquet store kept as-is for `acr-analytics`; a JSON/NDJSON store added for
   Graph objects) — with a **result-content digest** so a store can't ship a
   truncated/incomplete result (§4.1, §4.4).
2. A **domain-defined split trigger** (not OOM-only) so "input too big" is
   splittable for any consumer (§4.5).
3. A **bounded, validated `Retry-After` hook** (§4.2).
4. A **cross-OS lock + filesystem-durability shim** with a **per-commit generation
   binding** that closes the torn-two-rename window on all OSes (§4.3, §4.4).
5. An **optional incremental combine** so a >1M-object crawl never materializes the
   full result set (§4.6).

**Non-goals:** changing the *idempotency model* (content-addressed keys,
manifest-as-commit-boundary, OOM/transient/fatal classification stay in intent);
rewriting the CUD consumer's semantics; building the Graph crawler itself (a
*separate* project that depends on this library — this doc only proves the library
can carry it).

## 2. Motivation

- **Reuse without a bad dependency edge.** A Graph inventory tool must not
  `import` an analytics repo to get a checkpoint engine. Today the only way to
  reuse the engine is to copy the file — forking a correctness contract that took
  three rubber-duck rounds (v1→v3, `batch-checkpoint-retry.md`) to get right.
- **The gaps the Graph case exposes are real** — verified against the code:
  timeout-split is impossible (§0 B2), the store can ship an incomplete result
  (§0 B3), `load_all` won't scale to >1M (§0 B6), and the Linux-only primitives
  crash on the Windows-native target (§0 B4/B5).

## 3. What is generic today vs. what must change

### 3.1 Already generic (keep — this is the value)

`run_checkpointed(items, *, execute, content_key_fn, combine, store, is_oom,
is_transient, split_policy, retry, concurrency, worker_setup, worker_teardown,
...)` and its invariants: content-addressed identity derived at admission and
verified on resume (`ContentKeyCollisionError` on disagreement); manifest as the
atomic commit boundary; cross-version `fingerprint` wipes stale caches; OOM-first
classification; monotonic-clock delayed-retry heap; run-scope lock; worker-context
lifecycle. **None of these guarantees is removed** — §4 only adds to them.

### 3.2 Must change

| # | Coupling today | Change | Closes |
|---|---|---|---|
| 1 | `CheckpointStore.commit/load` call `pa.Table.from_pandas`/`to_pandas`; `combine` takes DataFrames | `ResultStore` protocol; `ParquetResultStore` (today) + `JsonlResultStore`; result type is an opaque `TypeVar` | — |
| 2 | store validates input identity only | Store also records + verifies a **result-content digest + length + count** | B3 |
| 3 | payload file + manifest are two independent renames | **Per-commit generation id + digest bound into both**; load rejects a generation mismatch | B4 |
| 4 | split only fires in the OOM branch | Domain-defined `should_split` checked **before** transient, independent of OOM | B2 |
| 5 | `RetryPolicy` = exponential+jitter only | `retry_after_fn(exc)->float\|None`, **validated + clamped** by `max_server_delay` | B7 |
| 6 | `fcntl.flock`, `libc statfs`, dir `fsync` | `LockProvider` + `DurabilityPolicy` behind interfaces; OS-owned Windows lock (no TTL takeover) | B5 |
| 7 | `combine(load_all())` materializes everything | Optional `reduce`/`sink` incremental combine | B6 |

## 4. Design

### 4.1 The `ResultStore` boundary (change 1)

The engine's only contract with a store:

```
class ResultStore(Protocol[Result]):
    @property
    def cached_keys(self) -> set[str]: ...
    def has(self, content_key: str, expected_payload_hash: str) -> bool: ...
    def load(self, content_key: str, expected_payload_hash: str) -> Result: ...
    def commit(self, content_key: str, payload_hash: str, result: Result) -> None: ...
    def load_all(self, key_to_hash: dict[str, str]) -> list[Result]: ...   # legacy path
    def iter_results(self, key_to_hash: dict[str, str]) -> Iterator[Result]: ...  # streaming (§4.6)
    def cleanup(self) -> None: ...
```

`Result` is an opaque `TypeVar`, not `pd.DataFrame`. All identity/atomicity logic —
manifest, embedded-metadata validation, tmp→fsync→replace→fsync-dir, the
**result-content digest** (change 2, §4.4) and **generation binding** (change 3,
§4.4) — lives in an abstract **`AtomicManifestStore`** base; concrete stores only
override *serialize one result to a temp file* + *deserialize + validate one file*:

- **`ParquetResultStore`** — today's `CheckpointStore` behavior (pyarrow table,
  embedded Arrow schema-fingerprint + `expected_schema` name-set check) **plus** the
  new result digest. `acr-analytics` uses this (§6.1).
- **`JsonlResultStore`** — NDJSON, one object per line; result type `list[dict]`.
  It cannot type-check like Arrow, so it relies on: the four identity fields, the
  **result-content digest + byte-length + record-count** (§4.4) validated over the
  **complete** stream before accepting a hit, and an optional
  `record_validator(obj) -> bool`. **The digest/length/count — not the shape check —
  is what makes an incomplete or truncated result a loud miss** (closes B3).

### 4.2 Retry-After hook (change 5), bounded

`RetryPolicy` gains `retry_after_fn: Callable[[Exception], float | None] = None`
and `max_server_delay: float`, **validated finite / ≥ 0 / operationally bounded at
construction** (C3 — an `inf` bound is rejected, else the clamp is meaningless). In
the transient branch:

```
server = retry.retry_after_fn(exc) if retry.retry_after_fn else None
if server is not None:
    if not math.isfinite(server) or server < 0:      # validate external input
        raise <fatal: malformed Retry-After>          # fail clearly, don't guess
    if server > retry.max_server_delay:               # bound it
        raise <fatal: server asked to wait > operational bound>
delay = max(_backoff_delay(retry, n, rng), server or 0.0)
```

`max(...)` preserves jitter/floor; validation + `max_server_delay` stop an infinite
/ hostile value from hanging `sleep`/`wait` or overflowing the heap deadline. Unset
hook = today's behavior exactly (so CUD is untouched). Classification stays
OOM/`should_split`-first (§4.5); a 429 the server also mislabels "timeout" is still
transient.

### 4.3 Cross-OS lock + durability (change 6)

- **`LockProvider`** — `acquire()/release()/held`, same `CacheLockedError`
  contract. `PosixFlockLock` = today's `AccountLock`. `WindowsLock` =
  `msvcrt.locking(LK_NBLCK)` on a byte of a **held** lockfile handle — an
  **OS-owned** lock that releases automatically on process death, so **no TTL
  takeover** and thus **no two-writer window** (closes B5). Contention → bounded
  wait → `CacheLockedError`.
- **`DurabilityPolicy`** — POSIX: today's `statfs` allowlist (`{ext4, xfs}`;
  `overlay/cifs/9p/nfs` fail closed). Windows: no `libc`/`statfs`, so resolve the
  volume via `GetDriveType`/`GetVolumeInformation`; **local NTFS** = durable,
  **network/removable** = fail-closed; default `FAIL_CLOSED` on unrecognized
  volumes on both OSes. Directory-`fsync` degrades to a documented weaker op on
  Windows — but correctness does **not** depend on it because of §4.4.

### 4.4 Generation binding + result digest — correctness independent of dir-fsync

This is the heart of the fix (closes B3/B4 and the v2 residual C1). Every `commit`:

1. computes `result_digest = sha256(canonical_result_records)`, `byte_len`,
   `record_count` over a **non-self-referential scope** — the serialized *result
   records/rows ONLY*, explicitly **excluding** the metadata block that will carry
   the digest (parquet key-value metadata sits outside the row data; the JSONL
   digest covers the record lines, not the trailer/manifest). The digest therefore
   never hashes itself;
2. mints a **monotonic per-commit `generation_id`** (uuid4 or a run-scoped
   counter);
3. embeds `{content_key, payload_hash, fingerprint, schema_version,
   result_digest, byte_len, record_count, generation_id}` in **both** the payload
   file's metadata **and** the manifest entry, each written with the atomic
   protocol (manifest last = the commit boundary).

**`has()` performs the FULL verification — reading the body and recomputing
`result_digest`/`byte_len`/`record_count` — before returning True (C1).** Because
the orchestrator *skips `execute` when `has()` is True*, a metadata-only check
would let a truncated/incomplete body skip recomputation; so completeness MUST be
proven at `has()` time, not deferred to `load()`. A hit is accepted **only if** the
body's recomputed digest/length/count match, **and** the payload file's embedded
`generation_id` + `result_digest` equal the manifest entry's. A torn write (new
manifest, stale/lost payload rename) → generation mismatch → **miss → recompute**,
never a wrong/older frame, on **every** OS regardless of directory-fsync strength.
Truncation/incompleteness → digest/length/count mismatch → miss. This makes the
Windows durability delta a *performance* concern (an extra recompute after power
loss), not a *correctness* one. (Shards are small, so the `has()`-time body read is
a bounded cost.)

> **Assumption made explicit (AGENTS §7):** this assumes `os.replace` is atomic on
> the target FS (true on ext4/xfs/**NTFS**). Where it isn't, `DurabilityPolicy`
> fails closed and caching is disabled.

### 4.5 Domain-defined split trigger (change 4, closes B2)

The router today only splits inside `is_oom`. v3 makes splitting a first-class
action **while preserving CUD's unsplittable-OOM-is-fatal behavior** and
**guaranteeing strict progress**. Precedence in `_route_exception`:

1. **OOM (classified first, unchanged for CUD):** `if is_oom(exc)` → split **iff**
   `split_policy.should_split(exc, item)` and the item is above the floor; **else
   FATAL** (an unsplittable/at-floor OOM is NEVER reclassified as transient —
   preserves `batch_checkpoint.py:811-822`).
2. **Non-OOM domain split (new, for Graph timeout/too-big):** `elif split_policy
   and split_policy.should_split(exc, item)` and above the floor → split.
3. `elif is_transient(exc)` → bounded same-key delayed re-queue (§4.2).
4. `else` → fatal → drain + re-raise original.

**Strict-progress requirement (C2):** `split` MUST return children that are
**strictly smaller/fewer** than the parent, and the engine enforces a **split-depth
/ split-budget cap**; a `split` that fails to shrink, or a depth-cap breach, is
**fatal** — no endless split chain. CUD already satisfies this (`_cp_split` halves
with a floor and `should_split` requires `target_size > FLOOR`); the generalized
contract now *requires* it of every consumer. `is_oom` is retained as the default
step-1 predicate for back-compat.

### 4.6 Incremental combine (change 7, closes B6)

`run_checkpointed` gains an optional streaming path: instead of
`combine(load_all(all_keys))`, a consumer supplies `reduce(acc, result) -> acc` +
`finalize(acc) -> Output`. The engine iterates terminal keys via
`store.iter_results(...)`, folding one result at a time — **peak memory ≈ one
shard**, not the whole inventory. CUD keeps `combine`/`load_all` (its stitched
frame is small). Exactly **one** of `combine` **xor** `reduce`+`finalize` is
required (validated at call — no third mode).

**No standalone `sink` (C4).** A write-through sink is implemented *inside*
`reduce` (append each result to the output, `finalize` closes it). Resume
idempotency holds because the engine iterates **each terminal key exactly once per
`run()`** — cached-and-resumed keys and freshly-committed keys alike are emitted
through `iter_results` exactly once — so `reduce` observes the complete terminal
set with no duplicates within a run. (Dedup *across* separate runs against a
persistent external sink is that sink's responsibility, keyed by `content_key`.)

### 4.7 Two resume grains (closes B1)

The engine supports two idempotent resume models; the consumer chooses:

- **Content-addressed set** (CUD): `content_key = hash(canonical membership)`;
  order-independent; any membership change → new key. Requires a **deterministic,
  disjoint, exhaustive** partition of the input.
- **Durable cursor** (Graph fallback if no server-supported partition exists): a
  shard's `execute` drains `nextLink`/`deltaLink` and the shard is the unit; the
  opaque continuation token is **not** the key (it isn't deterministic). If a shard
  fails mid-drain it re-fetches from the shard start (cheap when shards are small),
  OR the consumer persists the last good `deltaLink` as *shard state* to resume a
  delta crawl. The doc does **not** claim a specific Graph `$filter` works — the
  consumer must validate a partition against Graph's advanced-query matrix, else use
  this cursor grain.

## 5. Consumer wirings

### 5.1 Consumer #1 — CUD right-sizing (regression target)

Swap `bc.CheckpointStore(...)` → `chkpt.ParquetResultStore(...)` and
`import batch_checkpoint as bc` → `import chkpt`. `_content_key_fn`,
`_job_fingerprint`, `_cp_split`, `worker_setup/teardown`,
`expected_schema=[*gcols,"DailyVCores"]`, `should_split = is_oom & above-floor`
(the default) are **unchanged**. Acceptance: existing
`tests/test_batch_checkpoint.py` + `tests/test_right_sizing_compute.py` pass with
no logic edits (golden-frame byte-identity preserved), after the one-time
SCHEMA_VERSION bump (§6.2).

### 5.2 Consumer #2 — Graph SP inventory (the new demand)

- **Partition** on a **server-supported, disjoint, exhaustive** key validated
  against Graph's advanced-query matrix (§4.7) — NOT an assumed `startsWith(id,…)`.
  If none exists, use the **durable-cursor** grain: shard by a supported dimension
  (e.g. `appOwnerOrganizationId`, `servicePrincipalType`, `createdDateTime`
  windows with `$count`+`ConsistencyLevel:eventual`) and drain `nextLink`, or use
  `/servicePrincipals/delta` persisting `deltaLink` as shard state.
- `execute` drains all pages for a shard → `list[dict]`; `store =
  JsonlResultStore(record_validator=lambda o: "id" in o)`.
- `is_transient` = 429/503/504 + resets; `retry_after_fn` reads `Retry-After`
  (§4.2); `should_split` = shard-too-big/timeout → narrow the shard window (§4.5).
- **Incremental combine** (§4.6): fold each shard into an output sink (parquet/db);
  never `load_all` >1M.
- **Crawl epoch (note N2):** the directory is mutable/eventually-consistent, so the
  `fingerprint` MUST bind a **crawl-epoch id + tenant + API version + query shape**,
  and the cache MUST have an **epoch/expiry** — a resumed crawl is a point-in-time
  snapshot within one epoch; crossing epochs wipes. Best-effort cross-time
  consistency is a documented property, not a guarantee.

This wiring touches **zero** engine internals — it only supplies functions, picks
the JSONL store + streaming combine, and sets `retry_after_fn`/`should_split`.

## 6. Migration & compatibility

### 6.1 acr-analytics (consumer #1) path

1. Extract to the target repo (§9); publish installable (pip or pinned git dep in
   `requirements.txt`, `>=` floor per AGENTS §2).
2. Change the two import sites directly and **delete**
   `scripts/recipes/batch_checkpoint.py` (the repo has exactly one consumer — no
   lingering re-export shim).
3. Point `docs/design/batch-checkpoint-retry.md` at this library as engine SoT;
   keep only the CUD-specific wiring there.
4. Add a `docs/history/execution-log.md` entry; the `right_sizing_compute` suite is
   the regression gate.

### 6.2 Backward-compat contract (revised per note N1)

- **No signature change to `run_checkpointed`** except additive
  `retry.retry_after_fn` / `max_server_delay` and the optional `reduce/finalize/
  sink` — CUD stays call-compatible.
- **On-disk caches are NOT preserved across the extraction.** Adding the
  result-digest + generation-id fields (§4.4) is an on-disk format change → a
  **`SCHEMA_VERSION` bump**, which cleanly **invalidates and wipes** any existing
  cache (they're recomputable; a stale mixed-format read is impossible by design).
  v1's "caches remain valid" claim was wrong and is retracted.
- A **legacy-cache fixture** (generated by the current shipped implementation) is
  added to the test suite to prove the bump wipes-not-corrupts (§7).

## 7. Test plan

- **Port** `test_batch_checkpoint.py`; passes unchanged against `ParquetResultStore`
  (identity/atomicity/retry regression) modulo the SCHEMA_VERSION bump.
- **Result-digest / completeness:** a JSONL file truncated between records, a
  dropped record, and a byte-flip each → **`has()` returns False** (recompute), not
  a hit; parquet digest likewise. Confirms `has()` reads+verifies the body (C1) and
  the digest scope is non-self-referential.
- **Generation binding:** simulate new-manifest-but-stale-payload (skip the payload
  rename) → generation mismatch → miss + recompute, never an older frame; on both a
  POSIX and a Windows durability policy.
- **Retry-After:** honored; `max(computed, server)`; `NaN`/`inf`/negative server →
  fatal; `> max_server_delay` → fatal; **`max_server_delay=inf`/negative rejected
  at `RetryPolicy` construction** (C3); unset hook = today's behavior.
- **Split trigger:** unsplittable/at-floor OOM → **fatal** (CUD-preserving, C2);
  non-OOM timeout with `should_split=True` above floor → split; a `split` that
  fails to shrink or breaches the depth/budget cap → **fatal** (no endless chain);
  OOM default path unchanged.
- **Incremental combine:** a mocked 10k-shard crawl folds with bounded memory;
  `combine` **xor** `reduce`+`finalize` validation (no third/sink mode); each
  terminal key emitted exactly once per `run()` including resumed keys (C4).
- **Cross-OS lock:** `WindowsLock` contention → `CacheLockedError`; process-death
  releases the lock (no takeover); `DurabilityPolicy` fail-closed on network volume.
- **Consumer regression:** `acr-analytics` `right_sizing_compute` suite green.
- **Consumer smoke (Graph):** mocked-Graph shard crawl resumes after an injected
  429 + a mid-run kill (end-to-end idempotency on the JSONL store).
- **Legacy-cache fixture:** current-impl cache dir → clean wipe on the bump.
- CI matrix: Linux + Windows runners.

## 8. Open questions

1. **Home repo (§9)** — new dedicated repo vs. an existing shared-utility repo.
2. **Packaging** — pip-published (private feed) vs. pinned git URL (Graph tool's
   deployment target decides).
3. **Windows durability bar** — is NTFS `os.replace` + generation-binding recompute
   acceptable, or is a `FlushFileBuffers`-on-dir-handle (`ctypes`) hardening worth
   it? (v2 makes this a *perf*, not correctness, choice.)
4. **Async** — engine is threads; Graph SDKs are often async. Proposed **out of
   scope for v1** — wrap sync-over-async in `execute`; revisit an `asyncio` variant
   later.
5. **Delta vs. full crawl for Graph** — is `/servicePrincipals/delta` (persisted
   `deltaLink` shard state) the primary mode, or full snapshot per epoch? Consumer
   decision; the engine supports both via §4.7.

## 9. Naming & home (proposal)

- **Package name:** `chkpt` (working) — alternatives `resumable-batch`,
  `checkpoint-crawler`. Decide with the user.
- **Home repo:** a **new** standalone repo (not inside `acr-analytics`, not inside
  the Graph tool), bootstrapped with the dev-env-setup toolkit (profile `m`, hooks
  on, CI matrix Linux+Windows). Both consumers depend on it outward — no
  cross-consumer edge.

## Review attestations

- **Plan-stage rubber-duck:** **ran, 2 rounds** (SDLC round cap honored).
  - *Round 1 (v1)* → **NO-GO**, 7 blockers, all verified against
    `batch_checkpoint.py` (notably the OOM-only split branch at lines 811-822 and
    `combine(load_all())` at line 930). Folded into **v2** (§0).
  - *Round 2 (v2)* → **NO-GO**, 4 residual blockers in the v2 fixes
    (self-referential digest + late verification; unsplittable-OOM regressed to
    transient + unbounded split; unvalidated `max_server_delay`; undefined sink
    mode). Folded into **v3** (§0b).
  - **Terminated by acceptance at v3** — all 11 findings closed; no open blockers
    remain (remaining items are non-blocking product decisions in §8). A brief v3
    confirmation is welcome but not gating for GO.

- **Diff-stage rubber-duck (implementation PR):** **ran, 1 round.** Reviewed the
  ported `src/resumable_batch/` engine + stores + cross-OS shim against the §4
  contract. Raised 5 findings; adjudicated:
  - *Applied (3):* per-entry manifest validation so a single malformed entry
    wipes ALL (all-or-nothing boundary); `commit` rolls back its in-memory entry
    if the manifest write fails (never vouches for a payload the on-disk boundary
    doesn't); content_key path-safety validation (a buggy `content_key_fn` can't
    write outside the cache dir). Each has a regression test
    (`tests/test_features.py::TestStoreHardening`).
  - *Adjudicated non-blocking (2):* FS fail-closed gating is a **caller-side**
    decision via `fs_cache_enabled` (self-gating the store would break the common
    tmpfs dev/CI cache dir, and correctness never depends on FS durability —
    §4.4); the split "budget" is the **depth cap**, which bounds the chain even
    when item sizes are unknown (`target_size == 0`), with strict-shrink enforced
    whenever sizes are known. No open blockers remain.

- **Verify + confirmation rubber-duck (post-merge audit):** stage-3 `verifier`
  returned **PASS** (all 8 acceptance criteria met, CUD parity preserved, 89 tests
  green). A fresh confirmation `rubber-duck` found 2 blocking + 2 non-blocking
  read-path robustness gaps — **all 4 fixed** (PR #2) with regression tests:
  corrupt JSONL (bad UTF-8) and corrupt Parquet (`to_pandas` failure) now return a
  miss instead of raising out of `has()`; the Parquet digest normalizes through an
  Arrow round-trip for non-scalar dtype stability; `has()` binds manifest
  `byte_len`/`record_count`. Also closed the verifier's non-blocking gaps: added
  the §7 legacy-cache fixture test and corrected the stale test count. No open
  blockers remain.
