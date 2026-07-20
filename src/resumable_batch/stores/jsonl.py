"""``JsonlResultStore`` — NDJSON results, one object per line; result type is
``list[dict]``.

Cannot type-check like Arrow, so completeness rests on the base's result-content
digest + byte-length + record-count (validated over the complete stream before a
hit is accepted — this is what makes an incomplete/truncated result a loud miss,
closing B3), plus an optional ``record_validator(obj) -> bool``.

File format: line 0 is a JSON metadata header keyed by ``_META_KEY`` (the embedded
identity block, OUTSIDE the digested record scope); lines 1..N are the records.
The digest covers only the record lines, so it never hashes its own metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from ..durability import FsPolicy
from .base import AtomicManifestStore

_META_KEY = "__resumable_batch_meta__"


def _canonical_record_bytes(records) -> bytes:
    """Deterministic NDJSON rendering of the records (sorted keys, compact)."""
    return b"".join(
        json.dumps(rec, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8") + b"\n"
        for rec in records)


class JsonlResultStore(AtomicManifestStore):
    SUFFIX = ".jsonl"

    def __init__(self, cache_dir, *, lock, fingerprint: str,
                 record_validator: "Callable[[dict], bool] | None" = None,
                 fs_policy: FsPolicy = FsPolicy.FAIL_CLOSED):
        self.record_validator = record_validator
        super().__init__(cache_dir, lock=lock, fingerprint=fingerprint,
                         fs_policy=fs_policy)

    def _canonical_bytes(self, result) -> bytes:
        return _canonical_record_bytes(result)

    def _record_count(self, result) -> int:
        return int(len(result))

    def _write_file(self, tmp_path: Path, result, meta: "dict[str, str]") -> None:
        header = json.dumps({_META_KEY: meta}, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(header + "\n")
            for rec in result:
                fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False) + "\n")
            fh.flush()

    def _read_file(self, path: Path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().split("\n")
        except OSError:
            return None
        # Drop a single trailing empty line from the final newline.
        if lines and lines[-1] == "":
            lines = lines[:-1]
        if not lines:
            return None
        try:
            head = json.loads(lines[0])
        except ValueError:
            return None
        if not isinstance(head, dict) or _META_KEY not in head:
            return None
        meta = head[_META_KEY]
        if not isinstance(meta, dict):
            return None
        records = []
        for raw in lines[1:]:
            if raw == "":
                continue
            try:
                records.append(json.loads(raw))
            except ValueError:
                return None
        return records, {str(k): str(v) for k, v in meta.items()}

    def _validate_structure(self, result, meta: "dict[str, str]") -> bool:
        if self.record_validator is not None:
            return all(self.record_validator(r) for r in result)
        return True
