"""Ported from acr-analytics tests/test_batch_checkpoint.py — exercises consumer
#1 (CUD) wiring against the generalized ParquetResultStore + engine. Covers
content-addressed identity, crash-atomicity + manifest boundary, coordinator
liveness (delayed heap / OOM-first split), run()-scope locking, FS-type allowlist,
and the collision contract.
"""

from __future__ import annotations

import os
import random
import threading
import time

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import resumable_batch as rb
from resumable_batch import durability, engine
from _helpers import (
    Clock,
    ckfn,
    combine,
    cud_is_oom,
    cud_is_transient,
    frame_for,
    items,
    parquet_store,
    run,
    write_raw_parquet,
)


# ---------------------------------------------------------------------------
# Content-addressed keys + verified hash
# ---------------------------------------------------------------------------

class TestContentAddressedIdentity:
    def test_content_key_stable_over_membership_order(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        res1 = run(items(["a", "b", "c"]), store)
        assert res1.committed_keys == 1 and res1.resumed_keys == 0
        store2 = parquet_store(tmp_path, held_lock)
        res2 = run(items(["c", "a", "b"]), store2)
        assert res2.committed_keys == 0 and res2.resumed_keys == 1

    def test_changed_membership_invalidates(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        run(items(["a", "b"]), store)
        store2 = parquet_store(tmp_path, held_lock)
        res = run(items(["a", "b", "c"]), store2)
        assert res.committed_keys == 1 and res.resumed_keys == 0

    def test_content_key_is_full_sha256_hex(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        run(items(["a", "b"]), store)
        files = list((tmp_path / ".cache" / "job").glob("*.parquet"))
        assert len(files) == 1
        stem = files[0].name[:-len(".parquet")]
        assert len(stem) == 64 and all(c in "0123456789abcdef" for c in stem)

    def test_forced_collision_raises_not_hits(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck = "c" * 64
        store.commit(ck, "a" * 64, frame_for(["x"]))
        assert store.has(ck, "a" * 64) is True
        with pytest.raises(rb.ContentKeyCollisionError):
            store.has(ck, "b" * 64)
        with pytest.raises(rb.ContentKeyCollisionError):
            store.load(ck, "b" * 64)

    def test_live_map_rejects_second_hash_for_same_key(self, tmp_path, held_lock):
        import hashlib
        store = parquet_store(tmp_path, held_lock)

        def bad_ckfn(payload):
            key = "k" * 64
            return key, hashlib.sha256(str(payload).encode()).hexdigest()

        with pytest.raises(rb.ContentKeyCollisionError):
            rb.run_checkpointed(
                items(["a"], ["b"]), execute=lambda p, c: frame_for(p),
                content_key_fn=bad_ckfn, combine=combine, store=store)

    def test_split_duplicate_child_key_rejected(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)

        def execute(payload, ctx):
            raise Exception("E_RUNAWAY_QUERY: out of memory")

        dup_child = rb.CheckpointItem(payload=["z"], sched=rb.SchedMeta(target_size=15))
        split_policy = rb.SplitPolicy(
            should_split=lambda e, i: True,
            split=lambda i: [dup_child, dup_child])
        with pytest.raises(rb.ContentKeyCollisionError):
            rb.run_checkpointed(
                [rb.CheckpointItem(payload=["a", "b"], sched=rb.SchedMeta(target_size=50))],
                execute=execute, content_key_fn=ckfn, combine=combine,
                store=store, split_policy=split_policy, is_oom=lambda e: True)

    def test_resume_hash_mismatch_on_disk_raises(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a", "b"])
        store.commit(ck, ph, frame_for(["a", "b"]))
        path = store._path_for(ck)
        write_raw_parquet(path, frame_for(["a", "b"]), content_key=ck,
                          payload_hash="d" * 64, fingerprint=store.fingerprint)
        with pytest.raises(rb.ContentKeyCollisionError):
            store.has(ck, ph)


# ---------------------------------------------------------------------------
# Every embedded field must be PRESENT + exact; schema validated
# ---------------------------------------------------------------------------

class TestEmbeddedMetadataRequired:
    def test_metadataless_parquet_is_miss_never_ok(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a", "b"])
        store.commit(ck, ph, frame_for(["a", "b"]))
        table = pa.Table.from_pandas(frame_for(["a", "b"]), preserve_index=False)
        pq.write_table(table.replace_schema_metadata(None), str(store._path_for(ck)))
        assert store.has(ck, ph) is False
        with pytest.raises(RuntimeError):
            store.load(ck, ph)

    def test_missing_single_field_is_miss(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        base = dict(content_key=ck, payload_hash=ph, fingerprint=store.fingerprint)
        write_raw_parquet(store._path_for(ck), frame_for(["a"]),
                          schema_fingerprint=None, **base)
        store._entries[ck] = {"payload_hash": ph, "schema_version": rb.SCHEMA_VERSION,
                              "schema_fingerprint": "x", "committed_at": 0}
        assert store.has(ck, ph) is False

    def test_wrong_schema_parquet_is_miss(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock, expected_schema=["g", "v"])
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        assert store.has(ck, ph) is True
        wrong = pd.DataFrame({"g": ["x"], "OTHER": [1]})
        write_raw_parquet(store._path_for(ck), wrong, content_key=ck,
                          payload_hash=ph, fingerprint=store.fingerprint)
        assert store.has(ck, ph) is False
        with pytest.raises(RuntimeError):
            store.load(ck, ph)

    def test_body_drift_from_embedded_fp_is_miss(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        write_raw_parquet(store._path_for(ck), frame_for(["a"]),
                          content_key=ck, payload_hash=ph,
                          fingerprint=store.fingerprint,
                          schema_fingerprint="bogus:str;nope:int")
        with pytest.raises(RuntimeError):
            store.load(ck, ph)

    def test_commit_rejects_unexpected_columns(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock, expected_schema=["g", "v"])
        with pytest.raises(ValueError):
            store.commit("c" * 64, "a" * 64, pd.DataFrame({"g": ["x"], "WRONG": [1]}))

    def test_expected_schema_happy_path_round_trip(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock, expected_schema=["g", "v"])
        ck, ph = ckfn(["a", "b"])
        store.commit(ck, ph, frame_for(["a", "b"]))
        assert store.has(ck, ph) is True
        loaded = store.load_all({ck: ph})
        assert list(loaded[0].columns) == ["g", "v"]


class TestCrashAtomicity:
    def test_commit_finalizes_then_fsync_then_replace_then_dirfsync(
            self, tmp_path, held_lock, monkeypatch):
        from resumable_batch.stores import parquet as pqstore
        order = []
        orig_close = pq.ParquetWriter.close
        orig_fsf = durability._fsync_file
        orig_fsd = durability._fsync_dir
        orig_replace = os.replace

        def rec_close(self, *a, **k):
            order.append("close")
            return orig_close(self, *a, **k)

        monkeypatch.setattr(pq.ParquetWriter, "close", rec_close)
        monkeypatch.setattr(durability, "_fsync_file",
                            lambda p: (order.append("fsync_file"), orig_fsf(p))[1])
        monkeypatch.setattr(durability, "_fsync_dir",
                            lambda p: (order.append("fsync_dir"), orig_fsd(p))[1])
        monkeypatch.setattr(os, "replace",
                            lambda a, b: (order.append("replace"), orig_replace(a, b))[1])

        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        order.clear()
        store.commit(ck, ph, frame_for(["a"]))

        i_close = order.index("close")
        tail = order[i_close:]
        assert i_close == 0
        assert tail.index("fsync_file") < tail.index("replace") < tail.index("fsync_dir")
        assert not list((tmp_path / ".cache" / "job").glob("*.tmp.*"))

    def test_crash_before_parquet_replace_reexecutes(self, tmp_path, held_lock):
        cache = tmp_path / ".cache" / "job"
        cache.mkdir(parents=True)
        (cache / "orphan.parquet.tmp.123.abcd").write_text("garbage")
        store = parquet_store(tmp_path, held_lock)
        calls = []

        def execute(payload, ctx):
            calls.append(list(payload))
            return frame_for(payload)

        res = run(items(["a", "b"]), store, execute=execute)
        assert res.committed_keys == 1 and calls == [["a", "b"]]
        assert not list(cache.glob("*.tmp.*"))

    def test_crash_before_manifest_replace_keeps_prior_state(
            self, tmp_path, held_lock, monkeypatch):
        store = parquet_store(tmp_path, held_lock)
        ckA, phA = ckfn(["a"])
        store.commit(ckA, phA, frame_for(["a"]))
        monkeypatch.setattr(store, "_write_manifest", lambda: None)
        ckB, phB = ckfn(["b"])
        store.commit(ckB, phB, frame_for(["b"]))
        monkeypatch.undo()
        store2 = parquet_store(tmp_path, held_lock)
        assert store2.has(ckA, phA) is True
        assert store2.has(ckB, phB) is False

    def test_missing_or_malformed_manifest_invalidates_all(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        (tmp_path / ".cache" / "job" / rb.MANIFEST_NAME).write_text("{ this is not json")
        store2 = parquet_store(tmp_path, held_lock)
        assert store2.cached_keys == set()
        assert store2.has(ck, ph) is False

    def test_manifest_fingerprint_mismatch_wipes_cache(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock, fingerprint="fp-A")
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        store2 = parquet_store(tmp_path, held_lock, fingerprint="fp-B")
        assert store2.cached_keys == set()
        assert not list((tmp_path / ".cache" / "job").glob("*.parquet"))

    def test_readable_but_wrong_fingerprint_is_miss(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        write_raw_parquet(store._path_for(ck), frame_for(["a"]),
                          content_key=ck, payload_hash=ph, fingerprint="other-fp")
        assert store.has(ck, ph) is False

    def test_truncated_parquet_treated_as_miss(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        store._path_for(ck).write_bytes(b"PAR1\x00\x00truncated")
        assert store.has(ck, ph) is False

    def test_empty_frame_roundtrip_preserves_schema(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        empty = pd.DataFrame({"g": pd.Series([], dtype="object"),
                              "v": pd.Series([], dtype="int64")})
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, empty)
        out = store.load(ck, ph)
        assert list(out.columns) == ["g", "v"]
        assert len(out) == 0


# ---------------------------------------------------------------------------
# FS policy
# ---------------------------------------------------------------------------

class TestFsPolicy:
    def test_allowlisted_fs_enables_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(durability, "resolve_fs_type", lambda p: "ext")
        assert rb.fs_cache_enabled(tmp_path) is True

    def test_offallowlist_fs_fail_closed_disables(self, tmp_path, monkeypatch):
        monkeypatch.setattr(durability, "resolve_fs_type", lambda p: "tmpfs")
        assert rb.fs_cache_enabled(tmp_path, rb.FsPolicy.FAIL_CLOSED) is False

    def test_offallowlist_fs_require_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(durability, "resolve_fs_type", lambda p: "overlay")
        with pytest.raises(rb.FsUnsupportedError):
            rb.fs_cache_enabled(tmp_path, rb.FsPolicy.REQUIRE)

    def test_probe_success_alone_does_not_enable_unknown_fs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(durability, "resolve_fs_type", lambda p: "unknown(0xdead)")
        assert rb.fs_cache_enabled(tmp_path, rb.FsPolicy.FAIL_CLOSED) is False

    def test_resolve_fs_type_on_real_tmp_path(self, tmp_path):
        assert isinstance(rb.resolve_fs_type(tmp_path), str)


# ---------------------------------------------------------------------------
# Coordinator state machine
# ---------------------------------------------------------------------------

class TestCoordinator:
    def test_delayed_only_item_does_not_end_loop(self, tmp_path, held_lock):
        clock = Clock()
        state = {"n": 0}

        def execute(payload, ctx):
            state["n"] += 1
            if state["n"] == 1:
                raise TimeoutError("operation timed out")
            return frame_for(payload)

        res = run(items(["a"]), parquet_store(tmp_path, held_lock), execute=execute,
                  sleep=clock.sleep, monotonic=clock, rng=random.Random(0))
        assert res.committed_keys == 1 and res.transient_retries == 1

    def test_equal_ready_at_uses_seq_no_typeerror(self, tmp_path, held_lock):
        clock = Clock()
        fails = {"a": True, "b": True}

        def execute(payload, ctx):
            k = payload[0]
            if fails.get(k):
                fails[k] = False
                raise TimeoutError("timed out")
            return frame_for(payload)

        retry = rb.RetryPolicy(base_delay=1.0, jitter=0.0)
        res = run(items(["a"], ["b"]), parquet_store(tmp_path, held_lock),
                  execute=execute, concurrency=2, retry=retry,
                  sleep=clock.sleep, monotonic=clock, rng=random.Random(0))
        assert res.committed_keys == 2 and res.transient_retries == 2

    def test_matured_retry_not_blocked_by_long_inflight(self, tmp_path, held_lock):
        peak = {"cur": 0, "max": 0}
        plock = threading.Lock()
        a_fail = {"x": True}

        def execute(payload, ctx):
            with plock:
                peak["cur"] += 1
                peak["max"] = max(peak["max"], peak["cur"])
            try:
                if payload[0] == "A":
                    if a_fail["x"]:
                        a_fail["x"] = False
                        raise TimeoutError("timed out")
                    return frame_for(payload)
                time.sleep(0.4)
                return frame_for(payload)
            finally:
                with plock:
                    peak["cur"] -= 1

        retry = rb.RetryPolicy(base_delay=0.02, jitter=0.0)
        res = run(items(["A"], ["B"]), parquet_store(tmp_path, held_lock),
                  execute=execute, concurrency=2, retry=retry)
        assert res.committed_keys == 2
        assert peak["max"] == 2

    def test_split_replaces_parent_in_terminal_set(self, tmp_path, held_lock):
        n = {"i": 0}

        def execute(payload, ctx):
            n["i"] += 1
            if list(payload) == ["a", "b", "c", "d"]:
                raise Exception("out of memory")
            return pd.DataFrame({"g": ["x"], "v": [len(list(payload))]})

        split_policy = rb.SplitPolicy(
            should_split=lambda e, i: i.sched.target_size > 2,
            split=lambda i: [
                rb.CheckpointItem(payload=list(i.payload)[:2], sched=rb.SchedMeta(target_size=2)),
                rb.CheckpointItem(payload=list(i.payload)[2:], sched=rb.SchedMeta(target_size=2)),
            ])
        res = rb.run_checkpointed(
            [rb.CheckpointItem(payload=["a", "b", "c", "d"], sched=rb.SchedMeta(target_size=4))],
            execute=execute, content_key_fn=ckfn, combine=combine,
            store=parquet_store(tmp_path, held_lock), split_policy=split_policy,
            is_oom=lambda e: "out of memory" in str(e))
        assert res.splits == 1
        assert res.committed_keys == 2
        assert int(res.frame["v"].sum()) == 4

    def test_stale_parent_and_children_never_costitch(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        stale_ck = "f" * 64
        store.commit(stale_ck, "f" * 64, pd.DataFrame({"g": ["x"], "v": [999]}))

        def execute(payload, ctx):
            if list(payload) == ["a", "b", "c", "d"]:
                raise Exception("out of memory")
            return pd.DataFrame({"g": ["x"], "v": [len(list(payload))]})

        split_policy = rb.SplitPolicy(
            should_split=lambda e, i: i.sched.target_size > 2,
            split=lambda i: [
                rb.CheckpointItem(payload=list(i.payload)[:2], sched=rb.SchedMeta(target_size=2)),
                rb.CheckpointItem(payload=list(i.payload)[2:], sched=rb.SchedMeta(target_size=2)),
            ])
        res = rb.run_checkpointed(
            [rb.CheckpointItem(payload=["a", "b", "c", "d"], sched=rb.SchedMeta(target_size=4))],
            execute=execute, content_key_fn=ckfn, combine=combine,
            store=store, split_policy=split_policy,
            is_oom=lambda e: "out of memory" in str(e))
        assert int(res.frame["v"].sum()) == 4


# ---------------------------------------------------------------------------
# Retry / OOM routing
# ---------------------------------------------------------------------------

class TestRoutingAndRetry:
    def test_oom_at_floor_is_fatal_even_with_timeout_text(self, tmp_path, held_lock):
        def execute(payload, ctx):
            raise Exception("out of memory; operation timed out")

        split_policy = rb.SplitPolicy(should_split=lambda e, i: False, split=lambda i: [])
        with pytest.raises(Exception, match="out of memory"):
            run(items(["a"]), parquet_store(tmp_path, held_lock), execute=execute,
                split_policy=split_policy)

    def test_oom_classified_first_splits_not_retries(self, tmp_path, held_lock):
        events = []

        def execute(payload, ctx):
            events.append(list(payload))
            if list(payload) == ["a", "b"]:
                raise Exception("out of memory; timed out")
            return frame_for(payload)

        split_policy = rb.SplitPolicy(
            should_split=lambda e, i: i.sched.target_size > 1,
            split=lambda i: [
                rb.CheckpointItem(payload=[list(i.payload)[0]], sched=rb.SchedMeta(target_size=1)),
                rb.CheckpointItem(payload=[list(i.payload)[1]], sched=rb.SchedMeta(target_size=1)),
            ])
        res = run([rb.CheckpointItem(payload=["a", "b"], sched=rb.SchedMeta(target_size=2))],
                  parquet_store(tmp_path, held_lock), execute=execute,
                  split_policy=split_policy)
        assert res.splits == 1 and res.transient_retries == 0

    def test_transient_retry_then_success(self, tmp_path, held_lock):
        clock = Clock()
        n = {"i": 0}

        def execute(payload, ctx):
            n["i"] += 1
            if n["i"] <= 2:
                raise TimeoutError("operation timed out")
            return frame_for(payload)

        res = run(items(["a"]), parquet_store(tmp_path, held_lock), execute=execute,
                  retry=rb.RetryPolicy(max_attempts=5, base_delay=1.0, jitter=0.0),
                  sleep=clock.sleep, monotonic=clock, rng=random.Random(0))
        assert res.transient_retries == 2 and res.committed_keys == 1
        assert res.attempts == 3

    def test_fatal_after_retry_bound_reraises_original(self, tmp_path, held_lock):
        clock = Clock()

        class Blip(TimeoutError):
            pass

        def execute(payload, ctx):
            raise Blip("operation timed out")

        with pytest.raises(Blip):
            run(items(["a"]), parquet_store(tmp_path, held_lock), execute=execute,
                retry=rb.RetryPolicy(max_attempts=3, base_delay=1.0, jitter=0.0),
                sleep=clock.sleep, monotonic=clock, rng=random.Random(0))

    def test_fatal_non_transient_non_oom_preserves_cache(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)

        def execute(payload, ctx):
            if list(payload) == ["a"]:
                return frame_for(payload)
            raise ValueError("hard schema error")

        with pytest.raises(ValueError, match="hard schema"):
            run(items(["a"], ["b"]), store, execute=execute, concurrency=1)
        ckA, phA = ckfn(["a"])
        assert store.has(ckA, phA) is True

    def test_backoff_schedule_and_jitter_bounds(self, tmp_path, held_lock):
        rng = random.Random(0)
        retry = rb.RetryPolicy(base_delay=2.0, multiplier=2.0, max_delay=60.0, jitter=0.5)
        for attempt in range(1, 6):
            d = engine._backoff_delay(retry, attempt, rng)
            base = min(60.0, 2.0 * 2.0 ** (attempt - 1))
            assert base * 0.5 - 1e-9 <= d <= base * 1.5 + 1e-9


# ---------------------------------------------------------------------------
# load_all validation
# ---------------------------------------------------------------------------

class TestLoadAll:
    def test_load_all_delegates_to_verified_load(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ckA, phA = ckfn(["a"])
        ckB, phB = ckfn(["b"])
        store.commit(ckA, phA, frame_for(["a"]))
        store.commit(ckB, phB, frame_for(["b"]))
        frames = store.load_all({ckA: phA, ckB: phB})
        assert len(frames) == 2

    def test_load_all_raises_on_wrong_expected_hash(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ckA, phA = ckfn(["a"])
        store.commit(ckA, phA, frame_for(["a"]))
        with pytest.raises(rb.ContentKeyCollisionError):
            store.load_all({ckA: "z" * 64})


# ---------------------------------------------------------------------------
# Concurrency + locking
# ---------------------------------------------------------------------------

class TestLocking:
    def test_store_requires_held_lock(self, tmp_path):
        lock = rb.make_lock(tmp_path / ".batch-checkpoint.lock")  # not acquired
        with pytest.raises(RuntimeError):
            rb.ParquetResultStore(tmp_path / ".cache" / "j", lock=lock, fingerprint="f")

    def test_lock_is_outside_cache_tree(self, tmp_path):
        import shutil
        lockfile = tmp_path / ".batch-checkpoint.lock"
        lock = rb.make_lock(lockfile)
        lock.acquire()
        try:
            cache = tmp_path / ".cache"
            cache.mkdir()
            (cache / "x").write_text("y")
            shutil.rmtree(cache)
            assert lockfile.exists()
            assert lock.held is True
        finally:
            lock.release()

    def test_second_run_blocks_then_CacheLockedError(self, tmp_path):
        lockfile = tmp_path / ".batch-checkpoint.lock"
        a = rb.make_lock(lockfile)
        a.acquire()
        try:
            b = rb.make_lock(lockfile, timeout=0.3, poll=0.05)
            with pytest.raises(rb.CacheLockedError):
                b.acquire()
        finally:
            a.release()

    def test_lock_released_allows_reacquire(self, tmp_path):
        lockfile = tmp_path / ".batch-checkpoint.lock"
        a = rb.make_lock(lockfile)
        a.acquire()
        a.release()
        b = rb.make_lock(lockfile, timeout=0.5)
        b.acquire()
        b.release()

    def test_four_stores_share_one_held_lock(self, tmp_path):
        lock = rb.make_lock(tmp_path / ".batch-checkpoint.lock")
        lock.acquire()
        try:
            stores = [rb.ParquetResultStore(tmp_path / ".cache" / f"pass{i}",
                                            lock=lock, fingerprint=f"fp{i}")
                      for i in range(4)]
            assert len(stores) == 4
        finally:
            lock.release()

    def test_max_inflight_never_exceeds_concurrency(self, tmp_path, held_lock):
        peak = {"cur": 0, "max": 0}
        plock = threading.Lock()

        def execute(payload, ctx):
            with plock:
                peak["cur"] += 1
                peak["max"] = max(peak["max"], peak["cur"])
            time.sleep(0.02)
            with plock:
                peak["cur"] -= 1
            return frame_for(payload)

        it = items(*[[f"s{i}"] for i in range(12)])
        run(it, parquet_store(tmp_path, held_lock), execute=execute, concurrency=3)
        assert peak["max"] <= 3


# ---------------------------------------------------------------------------
# Stitch + lifecycle
# ---------------------------------------------------------------------------

class TestStitchLifecycle:
    def test_stitch_equals_no_cache_result(self, tmp_path, held_lock):
        payloads = [["a", "b"], ["c"], ["d", "e", "f"]]
        ref = combine([frame_for(p) for p in payloads])
        res = run(items(*payloads), parquet_store(tmp_path, held_lock))
        pd.testing.assert_frame_equal(
            res.frame.sort_values("g").reset_index(drop=True),
            ref.sort_values("g").reset_index(drop=True))

    def test_cleanup_on_success_removes_dir(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        run(items(["a"]), store, cleanup_on_success=True)
        assert not (tmp_path / ".cache" / "job").exists()

    def test_cache_kept_when_cleanup_flag_false(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        run(items(["a"]), store, cleanup_on_success=False)
        assert (tmp_path / ".cache" / "job").exists()


# ---------------------------------------------------------------------------
# CUD taxonomy (consumer #1 classifier wiring)
# ---------------------------------------------------------------------------

class TestTaxonomy:
    def test_boundary_regex_not_bare_substrings(self):
        assert not cud_is_oom(Exception("zoom call"))
        assert not cud_is_transient(Exception("processed 4290 rows"))
        assert cud_is_oom(Exception("query hit OOM at follower"))
        assert cud_is_oom(Exception("E_RUNAWAY_QUERY: out of memory"))
        assert cud_is_transient(Exception("Failed to process network request"))
        assert cud_is_transient(Exception("Query timed out"))
        assert cud_is_transient(Exception("node is not ready to answer, Role=Secondary"))
        assert cud_is_transient(Exception("HTTP 429 TooManyRequests throttled"))

    def test_oom_and_transient_are_distinct(self):
        assert cud_is_oom(Exception("out of memory"))
        assert not cud_is_transient(Exception("out of memory"))
        assert cud_is_transient(Exception("connection reset by peer"))
        assert not cud_is_oom(Exception("connection reset by peer"))
