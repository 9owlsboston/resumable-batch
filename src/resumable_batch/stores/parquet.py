"""``ParquetResultStore`` — pandas DataFrame results via pyarrow/parquet.

This is consumer #1 (acr-analytics CUD right-sizing) behavior: an embedded Arrow
schema-fingerprint + an optional ``expected_schema`` name-set check, PLUS the
result-content digest + generation binding the base provides. Requires the
``parquet`` extra (``pip install resumable-batch[parquet]``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..durability import FsPolicy
from .base import AtomicManifestStore


# ---------------------------------------------------------------------------
# Arrow schema fingerprint helpers (coarse type category — robust to int32/int64
# or float32/float64 width drift while still catching str<->number + add/rename).
# ---------------------------------------------------------------------------

def _arrow_kind(arrow_type) -> str:
    import pyarrow as pa
    if pa.types.is_integer(arrow_type):
        return "int"
    if pa.types.is_floating(arrow_type):
        return "float"
    if pa.types.is_boolean(arrow_type):
        return "bool"
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return "str"
    if pa.types.is_temporal(arrow_type):
        return "temporal"
    return str(arrow_type)


def _schema_fp_from_pairs(pairs: "Iterable[tuple[str, str]]") -> str:
    return ";".join(f"{name}:{kind}" for name, kind in pairs)


def _table_schema_fp(schema) -> str:
    return _schema_fp_from_pairs((f.name, _arrow_kind(f.type)) for f in schema)


def _names_of_fp(fp: str) -> "set[str]":
    return {seg.split(":", 1)[0] for seg in fp.split(";") if seg}


def _expected_name_set(expected_schema) -> "set[str]":
    names = getattr(expected_schema, "names", None)
    if names is not None:                     # pyarrow.Schema
        return set(names)
    return {str(n) for n in expected_schema}  # iterable of column names


def _write_parquet(tmp_path: Path, table) -> None:
    """Finalize/close the pyarrow writer so the footer is flushed (atomicity
    step 1 — the writer close ordering is observable/tested)."""
    import pyarrow.parquet as pq
    writer = pq.ParquetWriter(str(tmp_path), table.schema)
    try:
        writer.write_table(table)
    finally:
        writer.close()   # footer written — mandatory before fsync


class ParquetResultStore(AtomicManifestStore):
    SUFFIX = ".parquet"

    def __init__(self, cache_dir, *, lock, fingerprint: str,
                 expected_schema=None,
                 fs_policy: FsPolicy = FsPolicy.FAIL_CLOSED):
        self.expected_schema = expected_schema
        super().__init__(cache_dir, lock=lock, fingerprint=fingerprint,
                         fs_policy=fs_policy)

    # -- digest scope: records/rows only, deterministic + roundtrip-stable ---

    def _canonical_bytes(self, result) -> bytes:
        # Digest the row data (excludes parquet key-value metadata by
        # construction — non-self-referential, design C1). Normalize THROUGH an
        # Arrow round-trip first so the commit-side digest (raw input frame) and
        # the read-side digest (frame read back via table.to_pandas()) are
        # byte-identical for ALL dtypes — not just scalars. Without this, a list-
        # or Decimal-valued column renders differently after round-trip and a
        # freshly committed result would fail its own has() check forever.
        normalized = self._table_for(result).to_pandas()
        return normalized.to_csv(index=False).encode("utf-8")

    def _record_count(self, result) -> int:
        return int(len(result))

    # -- serialize / deserialize --------------------------------------------

    def _table_for(self, result):
        import pyarrow as pa
        return pa.Table.from_pandas(result, preserve_index=False)

    def _write_file(self, tmp_path: Path, result, meta: "dict[str, str]") -> None:
        table = self._table_for(result)
        kv = dict(table.schema.metadata or {})
        kv.update({k.encode(): v.encode() for k, v in meta.items()})
        _write_parquet(tmp_path, table.replace_schema_metadata(kv))

    def _read_file(self, path: Path):
        import pyarrow.parquet as pq
        # Guard the WHOLE read: read_table, metadata decode, AND to_pandas — a
        # readable-but-corrupt body (bad pandas metadata, unconvertible column)
        # must be a benign miss, never an exception escaping has()/load().
        try:
            table = pq.read_table(str(path))
            meta = self._decode_meta(table.schema.metadata)
            frame = table.to_pandas()
        except Exception:  # noqa: BLE001 — truncated/corrupt -> miss
            return None
        return frame, meta

    @staticmethod
    def _decode_meta(kv) -> "dict[str, str]":
        out: "dict[str, str]" = {}
        if not kv:
            return out
        for k, v in kv.items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            val = v.decode() if isinstance(v, bytes) else str(v)
            out[key] = val
        return out

    # -- structure validation + commit guard --------------------------------

    def _validate_structure(self, result, meta: "dict[str, str]") -> bool:
        stored_fp = meta.get("schema_fingerprint")
        if stored_fp is None:
            return False
        actual_fp = _table_schema_fp(self._table_for(result).schema)
        if actual_fp != stored_fp:
            return False              # body drifted from its recorded metadata
        if self.expected_schema is not None:
            if set(result.columns) != _expected_name_set(self.expected_schema):
                return False
        return True

    def _validate_commit(self, result) -> None:
        if self.expected_schema is not None:
            actual = set(result.columns)
            expected = _expected_name_set(self.expected_schema)
            if actual != expected:
                raise ValueError(
                    f"commit: frame columns {sorted(actual)} "
                    f"!= expected {sorted(expected)}")

    def _commit_extra_meta(self, result) -> "dict[str, str]":
        return {"schema_fingerprint": _table_schema_fp(self._table_for(result).schema)}
