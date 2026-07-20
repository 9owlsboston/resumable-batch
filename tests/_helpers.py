"""Shared test helpers — pure / fake-executor, no ADX, no live network.

Mirrors the reference acr-analytics test harness so the ported suite exercises the
same CUD (consumer #1) wiring against the generalized API.
"""

from __future__ import annotations

import hashlib
import re

import pandas as pd
import pyarrow as pa

import resumable_batch as rb
from resumable_batch.stores.parquet import _table_schema_fp

# --- CUD-like classifiers (mirror acr-analytics recipes/_base.py) ------------

_OOM_RE = re.compile(
    r"out of memory|\bE_RUNAWAY_QUERY\b|memory exceeded|low memory|\bOOM\b",
    re.IGNORECASE,
)
_TRANSIENT_RE = re.compile(
    r"Failed to process network request|Query timed out|request timed out"
    r"|operation timed out|server timeout|not ready to answer.*Role=Secondary"
    r"|\bfailover\b|service unavailable|throttl(?:ed|ing)|connection reset"
    r"|temporarily unavailable",
    re.IGNORECASE,
)


def cud_is_oom(exc: Exception) -> bool:
    return bool(_OOM_RE.search(str(exc)))


def cud_is_transient(exc: Exception) -> bool:
    import socket
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
        return True
    text = str(exc)
    if re.search(r"(?<!\d)429(?!\d)", text):
        return True
    return bool(_TRANSIENT_RE.search(text))


# --- content key / combine / frame -------------------------------------------

def ckfn(payload):
    """content_key_fn over a canonical, order-independent membership."""
    canon = "\n".join(sorted(set(str(s).lower() for s in payload)))
    h = hashlib.sha256(canon.encode()).hexdigest()
    return h, h


def combine(frames):
    if not frames:
        return pd.DataFrame(columns=["g", "v"])
    non_empty = [f for f in frames if not f.empty]
    if not non_empty:
        return pd.DataFrame(columns=["g", "v"])
    return (pd.concat(non_empty, ignore_index=True)
            .groupby("g", as_index=False)["v"].sum())


def frame_for(payload):
    return pd.DataFrame({"g": ["x"], "v": [len(list(payload))]})


class Clock:
    """A deterministic monotonic clock. Injected `sleep` advances it."""

    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)


def parquet_store(tmp_path, held_lock, *, fingerprint="fp-1", job="job",
                  expected_schema=None):
    return rb.ParquetResultStore(tmp_path / ".cache" / job, lock=held_lock,
                                 fingerprint=fingerprint,
                                 expected_schema=expected_schema)


def run(items, store, *, execute=None, concurrency=1, **kw):
    if execute is None:
        def execute(payload, ctx):
            return frame_for(payload)
    return rb.run_checkpointed(
        items, execute=execute, content_key_fn=ckfn, combine=combine,
        store=store, concurrency=concurrency,
        is_oom=cud_is_oom, is_transient=cud_is_transient, **kw)


def items(*payloads):
    return [rb.CheckpointItem(payload=list(p)) for p in payloads]


def write_raw_parquet(path, frame, *, content_key, payload_hash, fingerprint,
                      schema_version=rb.SCHEMA_VERSION, schema_fingerprint="__auto__"):
    """Write a parquet with arbitrary (old-style) embedded identity metadata.

    ``schema_fingerprint``: ``"__auto__"`` embeds the real names+kinds fp; ``None``
    omits it; any str embeds it. Deliberately omits the new digest/generation
    fields so such a hand-forged file is always a benign miss unless it fully
    matches a real commit."""
    import pyarrow.parquet as pq
    table = pa.Table.from_pandas(frame, preserve_index=False)
    kv = dict(table.schema.metadata or {})
    kv.update({
        b"content_key": content_key.encode(),
        b"payload_hash": payload_hash.encode(),
        b"fingerprint": fingerprint.encode(),
        b"schema_version": str(schema_version).encode(),
    })
    if schema_fingerprint == "__auto__":
        kv[b"schema_fingerprint"] = _table_schema_fp(table.schema).encode()
    elif schema_fingerprint is not None:
        kv[b"schema_fingerprint"] = str(schema_fingerprint).encode()
    pq.write_table(table.replace_schema_metadata(kv), str(path))
