"""The orchestrator: ``run_checkpointed`` — durable checkpoints + bounded
transient retry + domain-defined split + optional incremental combine.

Preserves the acr-analytics correctness contract (content-addressed identity
derived at admission and verified on resume; manifest as the atomic commit
boundary; OOM-first classification; monotonic-clock delayed-retry heap; run-scope
lock; worker-context lifecycle) and adds the generalizations in
``docs/design/checkpoint-library-generalization.md`` §4.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
import random
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Iterable

from .classifiers import default_is_oom, default_is_transient
from .errors import ContentKeyCollisionError, RetryAfterError
from .model import CheckpointItem, CheckpointResult, RetryPolicy, SplitPolicy

logger = logging.getLogger(__name__)


@dataclass
class _Admitted:
    """Runtime identity derived ONCE at admission and carried through
    ready/delayed/inflight. ``depth`` bounds the split chain (design §4.5)."""

    item: CheckpointItem
    content_key: str
    payload_hash: str
    depth: int = 0


class _SplitFatal(Exception):
    """Internal: a split that can't make strict progress / breaches the depth
    cap. Surfaced as a fatal run error (never retried)."""


def _backoff_delay(retry: RetryPolicy, attempt: int, rng: random.Random) -> float:
    """delay = min(max_delay, base * multiplier**(attempt-1)) with full jitter."""
    raw = retry.base_delay * (retry.multiplier ** (attempt - 1))
    capped = min(retry.max_delay, raw)
    if retry.jitter:
        capped *= rng.uniform(1.0 - retry.jitter, 1.0 + retry.jitter)
    return max(0.0, capped)


def _default_split_size(item: CheckpointItem) -> int:
    return int(getattr(item.sched, "target_size", 0) or 0)


def run_checkpointed(
    items: Iterable[CheckpointItem],
    *,
    execute: Callable[[object, object], object],
    content_key_fn: Callable[[object], "tuple[str, str]"],
    combine: "Callable[[list], object] | None" = None,
    store,
    is_oom: Callable[[Exception], bool] = default_is_oom,
    is_transient: Callable[[Exception], bool] = default_is_transient,
    split_policy: "SplitPolicy | None" = None,
    retry: RetryPolicy = RetryPolicy(),
    concurrency: int = 1,
    worker_setup: "Callable[[], object] | None" = None,
    worker_teardown: "Callable[[object], None] | None" = None,
    cleanup_on_success: bool = False,
    reduce: "Callable[[object, object], object] | None" = None,
    finalize: "Callable[[object], object] | None" = None,
    initial: object = None,
    split_size_fn: Callable[[CheckpointItem], int] = _default_split_size,
    max_split_depth: int = 64,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    rng: "random.Random | None" = None,
) -> CheckpointResult:
    """Run keyed work items with durable checkpoints + bounded transient retry.

    Provide EXACTLY one combine mode: ``combine(list_of_results)`` (materializing)
    XOR ``reduce(acc, result)`` + ``finalize(acc)`` (incremental — peak memory ≈
    one result, design §4.6). Each terminal key is emitted through the combine path
    exactly once per run (cached-and-resumed and freshly-committed alike)."""
    # -- combine-mode validation (exactly one; no third mode) ---------------
    has_combine = combine is not None
    has_reduce = reduce is not None or finalize is not None
    if has_combine == has_reduce:
        raise ValueError(
            "provide exactly one of `combine` OR (`reduce` + `finalize`)")
    if has_reduce and (reduce is None or finalize is None):
        raise ValueError("incremental combine requires BOTH `reduce` and `finalize`")

    if rng is None:
        rng = random.Random()
    max_workers = max(1, int(concurrency))

    live_map: "dict[str, str]" = {}
    seq_counter = itertools.count()

    def _admit(item: CheckpointItem, depth: int = 0) -> _Admitted:
        ck, ph = content_key_fn(item.payload)
        if ck in live_map:
            raise ContentKeyCollisionError(
                f"duplicate live content_key {ck} "
                f"(existing hash={live_map[ck]}, new hash={ph}) — a hash "
                f"collision, buggy content_key_fn, or duplicate root/child")
        live_map[ck] = ph
        return _Admitted(item=item, content_key=ck, payload_hash=ph, depth=depth)

    ready: deque = deque()
    delayed: list = []                 # heap of (ready_at, seq, _Admitted)
    terminal_keys: "set[str]" = set()
    terminal_hash: "dict[str, str]" = {}
    transient_attempts: "dict[str, int]" = {}

    for item in items:
        ad = _admit(item)
        terminal_keys.add(ad.content_key)
        terminal_hash[ad.content_key] = ad.payload_hash
        ready.append(ad)

    attempts_total = 0
    committed = 0
    resumed = 0
    transient_retries = 0
    splits = 0
    fatal_exc: "Exception | None" = None

    def _promote_matured() -> None:
        now = monotonic()
        while delayed and delayed[0][0] <= now:
            _, _, ad = heapq.heappop(delayed)
            ready.append(ad)

    def _handle_split(ad: _Admitted) -> None:
        """Split ``ad`` into strictly-smaller children (design §4.5). Raises
        :class:`_SplitFatal` on a depth-cap breach or a non-shrinking split."""
        nonlocal splits
        assert split_policy is not None
        if ad.depth + 1 > max_split_depth:
            raise _SplitFatal(
                f"split depth cap {max_split_depth} exceeded on {ad.content_key}")
        children = split_policy.split(ad.item)
        if not children:
            raise _SplitFatal(f"split produced no children for {ad.content_key}")
        # Strict-progress: when sizes are meaningful (parent + all children > 0),
        # every child MUST be strictly smaller than the parent — no endless chain.
        parent_sz = split_size_fn(ad.item)
        child_szs = [split_size_fn(c) for c in children]
        if parent_sz > 0 and all(s > 0 for s in child_szs):
            if max(child_szs) >= parent_sz:
                raise _SplitFatal(
                    f"split did not strictly shrink {ad.content_key} "
                    f"(parent size {parent_sz}, children {child_szs})")
        # Admit ALL children first (rejects a duplicate live child key) so a
        # partial failure can't half-replace the parent in terminal_keys.
        child_ads = [_admit(c, depth=ad.depth + 1) for c in children]
        terminal_keys.discard(ad.content_key)
        terminal_hash.pop(ad.content_key, None)
        for cad in child_ads:
            terminal_keys.add(cad.content_key)
            terminal_hash[cad.content_key] = cad.payload_hash
            ready.append(cad)
        splits += 1

    def _route_exception(ad: _Admitted, exc: Exception) -> "Exception | None":
        """Classify (OOM-first) and route. Returns a fatal exception or None."""
        nonlocal transient_retries
        # (a) OOM classified FIRST and independently.
        if is_oom(exc):
            if split_policy is not None and split_policy.should_split(exc, ad.item):
                try:
                    _handle_split(ad)
                    return None
                except _SplitFatal as sf:
                    logger.error("%s", sf)
                    return sf
            # OOM at the floor / not splittable -> FATAL (never reclassified as
            # transient even if the message ALSO contains a timeout marker).
            logger.error("persistent OOM (not splittable) on %s: %s",
                         ad.content_key, exc)
            return exc
        # (b) non-OOM domain split (e.g. Graph timeout / too-big).
        if split_policy is not None and split_policy.should_split(exc, ad.item):
            try:
                _handle_split(ad)
                return None
            except _SplitFatal as sf:
                logger.error("%s", sf)
                return sf
        # (c) transient -> delayed requeue of the SAME key, bounded.
        if is_transient(exc):
            n = transient_attempts.get(ad.content_key, 0) + 1
            transient_attempts[ad.content_key] = n
            if n < retry.max_attempts:
                server = (retry.retry_after_fn(exc)
                          if retry.retry_after_fn is not None else None)
                if server is not None:
                    if not math.isfinite(server) or server < 0:
                        return RetryAfterError(
                            f"malformed Retry-After {server!r} on {ad.content_key}")
                    if server > retry.max_server_delay:
                        return RetryAfterError(
                            f"server Retry-After {server}s exceeds operational "
                            f"bound {retry.max_server_delay}s on {ad.content_key}")
                delay = max(_backoff_delay(retry, n, rng), server or 0.0)
                ready_at = monotonic() + delay
                heapq.heappush(delayed, (ready_at, next(seq_counter), ad))
                transient_retries += 1
                logger.info("transient failure on %s (attempt %d/%d); retry in %.2fs: %s",
                            ad.content_key, n, retry.max_attempts, delay, exc)
                return None
            logger.error("transient retry budget exhausted on %s (%d/%d): %s",
                         ad.content_key, n, retry.max_attempts, exc)
            return exc
        # (d) neither OOM/split nor transient -> FATAL.
        logger.error("fatal (non-transient, non-OOM) on %s: %s", ad.content_key, exc)
        return exc

    # --- worker context lifecycle -----------------------------------------
    tls = threading.local()
    created: list = []
    created_lock = threading.Lock()

    def _init_worker() -> None:
        ctx = worker_setup() if worker_setup is not None else None
        tls.ctx = ctx
        with created_lock:
            created.append(ctx)

    def _run_one(payload):
        return execute(payload, getattr(tls, "ctx", None))

    pool = ThreadPoolExecutor(max_workers=max_workers, initializer=_init_worker)
    inflight: dict = {}   # future -> _Admitted
    try:
        while (ready or delayed or inflight) and fatal_exc is None:
            # 1. Dispatch from ready up to the concurrency cap (resume-skip first).
            while ready and len(inflight) < max_workers:
                ad = ready.popleft()
                hit = store.has(ad.content_key, ad.payload_hash)   # may raise collision
                if hit:
                    resumed += 1
                    continue     # already a committed terminal leaf; skip execution
                fut = pool.submit(_run_one, ad.item.payload)
                attempts_total += 1
                inflight[fut] = ad

            if fatal_exc is not None:
                break

            if inflight:
                # 2/3. Wait on in-flight; clamp to the next matured retry so a
                #      ready retry is never blocked behind a long in-flight query.
                timeout = None
                if delayed:
                    timeout = max(0.0, delayed[0][0] - monotonic())
                done, _ = wait(list(inflight), timeout=timeout,
                               return_when=FIRST_COMPLETED)
                for fut in done:
                    ad = inflight.pop(fut)
                    try:
                        result = fut.result()
                    except Exception as exc:  # noqa: BLE001 — routed below
                        fatal = _route_exception(ad, exc)
                        if fatal is not None:
                            fatal_exc = fatal
                            break
                    else:
                        store.commit(ad.content_key, ad.payload_hash, result)
                        committed += 1
                _promote_matured()
            elif delayed:
                # 2. Nothing in flight, only delayed work: sleep until the
                #    earliest ready_at (do NOT exit on zero-in-flight).
                wait_s = max(0.0, delayed[0][0] - monotonic())
                if wait_s > 0:
                    sleep(wait_s)
                _promote_matured()

        if fatal_exc is not None:
            for fut in list(inflight):
                fut.cancel()
    finally:
        pool.shutdown(wait=True)
        if worker_teardown is not None:
            for ctx in created:
                try:
                    worker_teardown(ctx)
                except Exception:  # noqa: BLE001
                    logger.warning("worker_teardown failed (ignored)", exc_info=True)

    if fatal_exc is not None:
        # Re-raise the ORIGINAL exception (never a teardown error). Checkpoints
        # survive so a re-run resumes.
        raise fatal_exc

    # Stitch/fold EXACTLY the live terminal-leaf set — a split parent is absent,
    # so an old child + a later parent can never co-stitch.
    expected = {ck: terminal_hash[ck] for ck in terminal_keys}
    if combine is not None:
        output = combine(store.load_all(expected))
    else:
        acc = initial
        for result in store.iter_results(expected):
            acc = reduce(acc, result)
        output = finalize(acc)

    if cleanup_on_success:
        store.cleanup()
    return CheckpointResult(
        frame=output,
        attempts=attempts_total,
        committed_keys=committed,
        resumed_keys=resumed,
        transient_retries=transient_retries,
        splits=splits,
    )
