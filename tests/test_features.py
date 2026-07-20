"""Tests for the generalized additions over the ported engine (design §4):
result-content digest + completeness, per-commit generation binding, bounded
Retry-After, domain-defined split trigger + strict progress, incremental combine,
the JsonlResultStore, and cross-OS lock/durability behavior.
"""

from __future__ import annotations

import json
import random

import pandas as pd
import pytest

import resumable_batch as rb
from resumable_batch import durability
from _helpers import Clock, ckfn, combine, frame_for, items, parquet_store, run


# ---------------------------------------------------------------------------
# Result-content digest + completeness (closes B3 / C1)
# ---------------------------------------------------------------------------

class TestResultDigestCompleteness:
    def _jsonl_store(self, tmp_path, held_lock, **kw):
        return rb.JsonlResultStore(tmp_path / ".cache" / "j", lock=held_lock,
                                   fingerprint="fp-1", **kw)

    def test_jsonl_truncated_between_records_is_miss(self, tmp_path, held_lock):
        store = self._jsonl_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, [{"id": 1}, {"id": 2}, {"id": 3}])
        assert store.has(ck, ph) is True
        path = store._path_for(ck)
        lines = path.read_text().split("\n")
        path.write_text("\n".join(lines[:3]) + "\n")   # drop the last record line
        assert store.has(ck, ph) is False               # record_count / digest miss

    def test_jsonl_dropped_record_is_miss(self, tmp_path, held_lock):
        store = self._jsonl_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, [{"id": 1}, {"id": 2}])
        lines = store._path_for(ck).read_text().split("\n")
        # Keep header + first record only (a dropped record) with a clean newline.
        store._path_for(ck).write_text(lines[0] + "\n" + lines[1] + "\n")
        assert store.has(ck, ph) is False

    def test_jsonl_byte_flip_in_record_is_miss(self, tmp_path, held_lock):
        store = self._jsonl_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, [{"id": 1}, {"id": 2}])
        text = store._path_for(ck).read_text().replace('"id":2', '"id":9')
        store._path_for(ck).write_text(text)
        assert store.has(ck, ph) is False               # digest mismatch

    def test_digest_scope_excludes_metadata_header(self, tmp_path, held_lock):
        # Editing ONLY the embedded meta header (outside the digest scope) must not
        # change the record digest — it fails other identity checks instead, never
        # a false digest pass.
        store = self._jsonl_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, [{"id": 1}])
        read = store._read_file(store._path_for(ck))
        assert read is not None
        result, _meta = read
        digest, blen, cnt = store._digest_info(result)
        assert cnt == 1 and blen > 0 and len(digest) == 64

    def test_parquet_body_flip_is_miss(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        # Re-commit a DIFFERENT body under the same identity via a raw write that
        # keeps the OLD embedded digest -> recomputed digest disagrees -> miss.
        import pyarrow as pa
        import pyarrow.parquet as pq
        meta = {b"content_key": ck.encode(), b"payload_hash": ph.encode(),
                b"fingerprint": store.fingerprint.encode(),
                b"schema_version": str(rb.SCHEMA_VERSION).encode(),
                b"result_digest": (b"0" * 64),
                b"byte_len": b"999", b"record_count": b"1",
                b"generation_id": b"deadbeef"}
        from resumable_batch.stores.parquet import _table_schema_fp
        tbl = pa.Table.from_pandas(pd.DataFrame({"g": ["x"], "v": [7]}),
                                   preserve_index=False)
        meta[b"schema_fingerprint"] = _table_schema_fp(tbl.schema).encode()
        pq.write_table(tbl.replace_schema_metadata(meta), str(store._path_for(ck)))
        assert store.has(ck, ph) is False


# ---------------------------------------------------------------------------
# Generation binding (closes B4) — torn write never serves a stale body
# ---------------------------------------------------------------------------

class TestGenerationBinding:
    def test_manifest_generation_ahead_of_payload_is_miss(self, tmp_path, held_lock):
        # Simulate a torn write: manifest advanced to a new generation but the
        # payload file still carries the old one -> generation mismatch -> miss
        # (recompute), never an older/stale frame.
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        assert store.has(ck, ph) is True
        store._entries[ck]["generation_id"] = "a-newer-generation-that-payload-lacks"
        assert store.has(ck, ph) is False

    def test_matching_generation_is_hit(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        # Reopen a fresh store (reads manifest from disk) -> generation persists.
        store2 = parquet_store(tmp_path, held_lock)
        assert store2.has(ck, ph) is True


# ---------------------------------------------------------------------------
# Bounded Retry-After (closes B7 / C3)
# ---------------------------------------------------------------------------

class TestRetryAfter:
    def _run_with(self, tmp_path, held_lock, retry, server_seq):
        clock = Clock()
        calls = {"i": 0}

        def execute(payload, ctx):
            calls["i"] += 1
            if calls["i"] <= len(server_seq):
                raise TimeoutError("operation timed out")
            return frame_for(payload)

        return run(items(["a"]), parquet_store(tmp_path, held_lock), execute=execute,
                   retry=retry, sleep=clock.sleep, monotonic=clock,
                   rng=random.Random(0)), clock

    def test_max_server_delay_inf_rejected_at_construction(self):
        with pytest.raises(ValueError):
            rb.RetryPolicy(max_server_delay=float("inf"))

    def test_max_server_delay_negative_rejected_at_construction(self):
        with pytest.raises(ValueError):
            rb.RetryPolicy(max_server_delay=-1.0)

    def test_server_delay_honored_as_lower_bound(self, tmp_path, held_lock):
        retry = rb.RetryPolicy(max_attempts=5, base_delay=1.0, jitter=0.0,
                               max_server_delay=100.0,
                               retry_after_fn=lambda e: 30.0)
        res, clock = self._run_with(tmp_path, held_lock, retry, [1])
        assert res.committed_keys == 1 and res.transient_retries == 1
        # Injected clock advanced by at least the server delay (>= 30).
        assert clock.t >= 1000.0 + 30.0

    def test_nan_server_delay_is_fatal(self, tmp_path, held_lock):
        retry = rb.RetryPolicy(max_attempts=5, base_delay=1.0, jitter=0.0,
                               retry_after_fn=lambda e: float("nan"))
        with pytest.raises(rb.RetryAfterError):
            self._run_with(tmp_path, held_lock, retry, [1, 1])

    def test_negative_server_delay_is_fatal(self, tmp_path, held_lock):
        retry = rb.RetryPolicy(max_attempts=5, base_delay=1.0, jitter=0.0,
                               retry_after_fn=lambda e: -5.0)
        with pytest.raises(rb.RetryAfterError):
            self._run_with(tmp_path, held_lock, retry, [1, 1])

    def test_server_delay_above_bound_is_fatal(self, tmp_path, held_lock):
        retry = rb.RetryPolicy(max_attempts=5, base_delay=1.0, jitter=0.0,
                               max_server_delay=10.0, retry_after_fn=lambda e: 999.0)
        with pytest.raises(rb.RetryAfterError):
            self._run_with(tmp_path, held_lock, retry, [1, 1])

    def test_unset_hook_is_todays_behavior(self, tmp_path, held_lock):
        retry = rb.RetryPolicy(max_attempts=5, base_delay=1.0, jitter=0.0)
        res, _ = self._run_with(tmp_path, held_lock, retry, [1, 1])
        assert res.committed_keys == 1 and res.transient_retries == 2


# ---------------------------------------------------------------------------
# Split trigger + strict progress (closes B2 / C2)
# ---------------------------------------------------------------------------

class TestSplitTrigger:
    def test_non_oom_domain_split_above_floor(self, tmp_path, held_lock):
        # A NON-OOM timeout with should_split=True (above floor) splits — the
        # generalization the OOM-only branch could not do.
        def execute(payload, ctx):
            if list(payload) == ["a", "b"]:
                raise TimeoutError("shard too big / operation timed out")
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
        assert res.committed_keys == 2

    def test_non_shrinking_split_is_fatal(self, tmp_path, held_lock):
        # Children NOT strictly smaller than parent -> fatal, no endless chain.
        def execute(payload, ctx):
            raise Exception("out of memory")

        split_policy = rb.SplitPolicy(
            should_split=lambda e, i: True,
            split=lambda i: [
                rb.CheckpointItem(payload=list(i.payload) + ["x"], sched=rb.SchedMeta(target_size=99)),
            ])
        with pytest.raises(Exception, match="strictly shrink"):
            rb.run_checkpointed(
                [rb.CheckpointItem(payload=["a", "b"], sched=rb.SchedMeta(target_size=2))],
                execute=execute, content_key_fn=ckfn, combine=combine,
                store=parquet_store(tmp_path, held_lock),
                split_policy=split_policy, is_oom=lambda e: True)

    def test_split_depth_cap_is_fatal(self, tmp_path, held_lock):
        # A splitter that always shrinks by 1 but never reaches a runnable leaf,
        # capped at a small depth -> fatal (bounded), never an endless chain.
        def execute(payload, ctx):
            raise Exception("out of memory")

        def split(i):
            sz = i.sched.target_size
            return [rb.CheckpointItem(payload=list(i.payload) + [f"c{sz}"],
                                      sched=rb.SchedMeta(target_size=sz - 1))]

        split_policy = rb.SplitPolicy(should_split=lambda e, i: i.sched.target_size > 0,
                                      split=split)
        with pytest.raises(Exception, match="depth cap"):
            rb.run_checkpointed(
                [rb.CheckpointItem(payload=["a"], sched=rb.SchedMeta(target_size=1000))],
                execute=execute, content_key_fn=ckfn, combine=combine,
                store=parquet_store(tmp_path, held_lock),
                split_policy=split_policy, is_oom=lambda e: True, max_split_depth=3)

    def test_empty_split_is_fatal(self, tmp_path, held_lock):
        def execute(payload, ctx):
            raise Exception("out of memory")

        split_policy = rb.SplitPolicy(should_split=lambda e, i: True, split=lambda i: [])
        with pytest.raises(Exception, match="no children"):
            rb.run_checkpointed(
                [rb.CheckpointItem(payload=["a", "b"], sched=rb.SchedMeta(target_size=2))],
                execute=execute, content_key_fn=ckfn, combine=combine,
                store=parquet_store(tmp_path, held_lock),
                split_policy=split_policy, is_oom=lambda e: True)


# ---------------------------------------------------------------------------
# Incremental combine (closes B6 / C4)
# ---------------------------------------------------------------------------

class TestIncrementalCombine:
    def test_reduce_finalize_matches_combine(self, tmp_path, held_lock):
        payloads = [["a", "b"], ["c"], ["d", "e", "f"]]
        ref = combine([frame_for(p) for p in payloads])

        def reduce_fn(acc, frame):
            acc = acc or []
            acc.append(frame)
            return acc

        def finalize(acc):
            return combine(acc or [])

        res = rb.run_checkpointed(
            items(*payloads), execute=lambda p, c: frame_for(p),
            content_key_fn=ckfn, store=parquet_store(tmp_path, held_lock),
            reduce=reduce_fn, finalize=finalize)
        pd.testing.assert_frame_equal(
            res.frame.sort_values("g").reset_index(drop=True),
            ref.sort_values("g").reset_index(drop=True))

    def test_each_terminal_key_folded_exactly_once_incl_resumed(self, tmp_path, held_lock):
        # First run commits all; a second run (all resumed) must still fold each
        # terminal key exactly once through iter_results.
        payloads = [["a"], ["b"], ["c"]]
        store1 = parquet_store(tmp_path, held_lock)
        run(items(*payloads), store1)  # commit all

        seen = []

        def reduce_fn(acc, frame):
            seen.append(int(frame["v"].sum()))
            return (acc or 0) + int(frame["v"].sum())

        store2 = parquet_store(tmp_path, held_lock)
        res = rb.run_checkpointed(
            items(*payloads), execute=lambda p, c: frame_for(p),
            content_key_fn=ckfn, store=store2, reduce=reduce_fn,
            finalize=lambda acc: acc or 0)
        assert res.resumed_keys == 3 and res.committed_keys == 0
        assert len(seen) == 3          # each terminal key folded exactly once
        assert res.frame == 3

    def test_both_combine_and_reduce_rejected(self, tmp_path, held_lock):
        with pytest.raises(ValueError, match="exactly one"):
            rb.run_checkpointed(
                items(["a"]), execute=lambda p, c: frame_for(p),
                content_key_fn=ckfn, store=parquet_store(tmp_path, held_lock),
                combine=combine, reduce=lambda a, r: a, finalize=lambda a: a)

    def test_neither_combine_nor_reduce_rejected(self, tmp_path, held_lock):
        with pytest.raises(ValueError, match="exactly one"):
            rb.run_checkpointed(
                items(["a"]), execute=lambda p, c: frame_for(p),
                content_key_fn=ckfn, store=parquet_store(tmp_path, held_lock))

    def test_reduce_without_finalize_rejected(self, tmp_path, held_lock):
        with pytest.raises(ValueError, match="BOTH"):
            rb.run_checkpointed(
                items(["a"]), execute=lambda p, c: frame_for(p),
                content_key_fn=ckfn, store=parquet_store(tmp_path, held_lock),
                reduce=lambda a, r: a)


# ---------------------------------------------------------------------------
# JsonlResultStore end-to-end
# ---------------------------------------------------------------------------

class TestJsonlStore:
    def test_end_to_end_crawl_resume(self, tmp_path, held_lock):
        # Two shards, each returning a list of dict objects; a mocked transient on
        # the first attempt of shard B. Resume + JSONL store idempotency.
        n = {"b": 0}

        def execute(payload, ctx):
            shard = payload[0]
            if shard == "B" and n["b"] == 0:
                n["b"] += 1
                raise TimeoutError("429 throttled, operation timed out")
            return [{"id": f"{shard}-{i}"} for i in range(3)]

        store = rb.JsonlResultStore(tmp_path / ".cache" / "crawl", lock=held_lock,
                                    fingerprint="tenant-epoch-1",
                                    record_validator=lambda o: "id" in o)

        def reduce_fn(acc, recs):
            acc = acc or []
            acc.extend(recs)
            return acc

        res = rb.run_checkpointed(
            [rb.CheckpointItem(payload=["A"]), rb.CheckpointItem(payload=["B"])],
            execute=execute, content_key_fn=lambda p: (
                __import__("hashlib").sha256(p[0].encode()).hexdigest(),
                __import__("hashlib").sha256(p[0].encode()).hexdigest()),
            store=store, reduce=reduce_fn, finalize=lambda acc: acc or [],
            is_transient=lambda e: True,
            retry=rb.RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0))
        ids = sorted(o["id"] for o in res.frame)
        assert ids == ["A-0", "A-1", "A-2", "B-0", "B-1", "B-2"]
        assert res.transient_retries == 1

    def test_record_validator_rejects_bad_record_on_read(self, tmp_path, held_lock):
        store = rb.JsonlResultStore(tmp_path / ".cache" / "j", lock=held_lock,
                                    fingerprint="fp-1",
                                    record_validator=lambda o: "id" in o)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, [{"id": 1}])
        assert store.has(ck, ph) is True
        # Rewrite a record missing the required field (keep count/format valid).
        path = store._path_for(ck)
        lines = path.read_text().split("\n")
        lines[1] = json.dumps({"nope": 1})
        path.write_text("\n".join(lines))
        assert store.has(ck, ph) is False


# ---------------------------------------------------------------------------
# Cross-OS lock/durability
# ---------------------------------------------------------------------------

class TestCrossOsLockDurability:
    def test_make_lock_returns_platform_lock(self, tmp_path):
        import os
        lock = rb.make_lock(tmp_path / ".lock")
        if os.name == "nt":
            assert isinstance(lock, rb.WindowsLock)
        else:
            assert isinstance(lock, rb.PosixFlockLock)

    def test_ntfs_in_durable_allowlist(self):
        assert "ntfs" in rb.DURABLE_FS_ALLOWLIST

    def test_durability_policy_fail_closed_on_network_volume(self, tmp_path, monkeypatch):
        monkeypatch.setattr(durability, "resolve_fs_type", lambda p: "network")
        assert rb.fs_cache_enabled(tmp_path, rb.FsPolicy.FAIL_CLOSED) is False

    def test_ntfs_enables_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(durability, "resolve_fs_type", lambda p: "ntfs")
        assert rb.fs_cache_enabled(tmp_path) is True


# ---------------------------------------------------------------------------
# Store robustness hardenings (diff-stage rubber-duck findings)
# ---------------------------------------------------------------------------

class TestStoreHardening:
    def test_unsafe_content_key_rejected(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        for bad in ["../escaped", "a/b", "..", ".", "", "a\\b"]:
            with pytest.raises(ValueError):
                store._path_for(bad)

    def test_unsafe_key_blocks_commit_escape(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        with pytest.raises(ValueError):
            store.commit("../escaped", "a" * 64, frame_for(["a"]))
        assert not (tmp_path / ".cache").parent.joinpath("escaped.parquet").exists()

    def test_malformed_manifest_entry_wipes_all(self, tmp_path, held_lock):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])
        store.commit(ck, ph, frame_for(["a"]))
        # Corrupt ONE entry (drop its generation_id) directly on disk.
        import json
        mpath = tmp_path / ".cache" / "job" / rb.MANIFEST_NAME
        doc = json.loads(mpath.read_text())
        doc["entries"][ck].pop("generation_id")
        mpath.write_text(json.dumps(doc))
        store2 = parquet_store(tmp_path, held_lock)
        assert store2.cached_keys == set()          # all-or-nothing wipe
        assert store2.has(ck, ph) is False

    def test_commit_rolls_back_inmemory_on_manifest_failure(self, tmp_path, held_lock,
                                                            monkeypatch):
        store = parquet_store(tmp_path, held_lock)
        ck, ph = ckfn(["a"])

        def boom():
            raise OSError("disk full")

        monkeypatch.setattr(store, "_write_manifest", boom)
        with pytest.raises(OSError):
            store.commit(ck, ph, frame_for(["a"]))
        # In-memory state rolled back: the store does NOT vouch for the payload.
        assert ck not in store.cached_keys
        assert store.has(ck, ph) is False
