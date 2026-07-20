"""Store abstractions: the :class:`ResultStore` protocol + the
:class:`AtomicManifestStore` base that owns *all* identity, atomicity, result-digest
and generation-binding logic. Concrete stores (parquet, jsonl) only serialize /
deserialize one result and validate store-specific structure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Iterator, Protocol, TypeVar, runtime_checkable

from ..durability import (
    FsPolicy,
    _atomic_replace_file,
    _tmp_name,
)
from ..errors import ContentKeyCollisionError
from ..model import MANIFEST_NAME, SCHEMA_VERSION

logger = logging.getLogger(__name__)

Result = TypeVar("Result")

# A content_key becomes a filename component; restrict it to a safe charset and
# forbid path traversal ('.'/'..', separators). Consumers derive keys from a hash
# digest, which satisfies this; a buggy content_key_fn fails loud instead of
# writing outside the cache dir.
_SAFE_KEY = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")

_MISSING = object()  # sentinel for "no prior manifest entry" (commit rollback)


def _is_safe_key(content_key: str) -> bool:
    return (isinstance(content_key, str)
            and content_key not in (".", "..")
            and "/" not in content_key
            and "\\" not in content_key
            and bool(_SAFE_KEY.match(content_key)))


@runtime_checkable
class ResultStore(Protocol[Result]):
    """The engine's only contract with a durable result cache."""

    @property
    def cached_keys(self) -> "set[str]": ...
    def has(self, content_key: str, expected_payload_hash: str) -> bool: ...
    def load(self, content_key: str, expected_payload_hash: str) -> Result: ...
    def commit(self, content_key: str, payload_hash: str, result: Result) -> None: ...
    def load_all(self, key_to_hash: "dict[str, str] | set[str]") -> "list[Result]": ...
    def iter_results(self, key_to_hash: "dict[str, str] | set[str]") -> "Iterator[Result]": ...
    def cleanup(self) -> None: ...


class AtomicManifestStore:
    """Content-addressed, crash-atomic cache for one job (pass).

    The manifest is THE source of truth: an orphan payload with no manifest entry
    is never read; a missing/malformed/fingerprint-mismatched manifest invalidates
    ALL entries. Every commit binds a **per-commit generation id** + a
    **result-content digest / byte-length / record-count** into BOTH the payload
    file and the manifest entry; ``has()`` reads the body and re-verifies all of
    them before returning True (design §4.4) so a truncated/torn/stale result is a
    loud miss (recompute), never a wrong or incomplete hit.
    """

    #: File suffix for a committed result (e.g. ``.parquet`` / ``.jsonl``).
    SUFFIX: str = ".bin"

    def __init__(self, cache_dir, *, lock,
                 fingerprint: str,
                 fs_policy: FsPolicy = FsPolicy.FAIL_CLOSED):
        # NOTE: ``fs_policy`` is retained for API parity + caller introspection but
        # the store does NOT self-gate on it: whether to use a durable cache at all
        # is a CALLER decision made via ``fs_cache_enabled(cache_dir, fs_policy)``
        # BEFORE constructing the store (design §4.3). Self-gating here would break
        # the common dev/CI case of a tmpfs-backed cache dir. Correctness never
        # depends on FS durability anyway — it is carried by the generation binding
        # + result digest (design §4.4).
        if lock is None or not getattr(lock, "held", False):
            raise RuntimeError(
                "AtomicManifestStore requires a HELD run()-scope lock "
                "(stores never self-acquire)")
        if not fingerprint:
            raise ValueError("store requires a non-empty fingerprint")
        self.cache_dir = Path(cache_dir)
        self.lock = lock
        self.fingerprint = fingerprint
        self.fs_policy = fs_policy
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.cache_dir / MANIFEST_NAME
        self._entries: "dict[str, dict]" = {}
        self._load_or_invalidate_manifest()
        self._sweep_orphan_tmps()

    # ------------------------------------------------------------------
    # Subclass contract — serialize/deserialize ONE result; store-specific
    # structure validation. None of these touch identity/atomicity.
    # ------------------------------------------------------------------

    def _canonical_bytes(self, result) -> bytes:
        """Deterministic byte rendering of the result RECORDS/ROWS ONLY.

        This is the non-self-referential digest scope — it must NOT include any
        embedded identity metadata (design C1). Must reproduce identical bytes for
        a committed result and the same result read back from disk."""
        raise NotImplementedError

    def _record_count(self, result) -> int:
        raise NotImplementedError

    def _write_file(self, tmp_path: Path, result, meta: "dict[str, str]") -> None:
        """Serialize ``result`` to ``tmp_path`` embedding ``meta`` OUTSIDE the
        digested record scope. The base performs the atomic replace afterward."""
        raise NotImplementedError

    def _read_file(self, path: Path):
        """Return ``(result, embedded_meta)`` or ``None`` if unreadable/corrupt."""
        raise NotImplementedError

    def _validate_structure(self, result, meta: "dict[str, str]") -> bool:
        """Store-specific structural check (schema names / record validator).
        Return False for a benign miss. Default: accept."""
        return True

    def _validate_commit(self, result) -> None:
        """Fail LOUDLY (raise) if a producer emits a result the store must never
        persist under a valid identity. Default: accept."""

    def _commit_extra_meta(self, result) -> "dict[str, str]":
        """Extra embedded identity fields (e.g. schema fingerprint). Default: none."""
        return {}

    # ------------------------------------------------------------------
    # Manifest lifecycle
    # ------------------------------------------------------------------

    def _load_or_invalidate_manifest(self) -> None:
        raw = None
        if self._manifest_path.exists():
            try:
                raw = json.loads(self._manifest_path.read_text())
            except (OSError, ValueError):
                raw = None
        if (not isinstance(raw, dict)
                or raw.get("fingerprint") != self.fingerprint
                or raw.get("schema_version") != SCHEMA_VERSION
                or not isinstance(raw.get("entries"), dict)
                or not self._entries_well_formed(raw["entries"])):
            # missing / malformed / fingerprint- or schema-mismatch / a malformed
            # entry -> wipe ALL (the manifest is all-or-nothing, design §4).
            if raw is not None:
                logger.warning("manifest invalid/stale at %s; wiping cache",
                               self.cache_dir)
            self._wipe_tree()
            self._entries = {}
            self._write_manifest()
            return
        self._entries = dict(raw["entries"])

    @staticmethod
    def _entries_well_formed(entries: dict) -> bool:
        """Every entry must be a dict carrying the identity fields ``has()``/
        ``load()`` compare against — a single malformed entry invalidates ALL
        (never a partial manifest that AttributeErrors mid-run)."""
        required = ("payload_hash", "generation_id", "result_digest")
        for key, entry in entries.items():
            if not isinstance(key, str) or not _is_safe_key(key):
                return False
            if not isinstance(entry, dict):
                return False
            if any(not isinstance(entry.get(f), str) for f in required):
                return False
        return True

    def _wipe_tree(self) -> None:
        for child in self.cache_dir.iterdir():
            if child.is_file():
                try:
                    child.unlink()
                except OSError:
                    pass

    def _sweep_orphan_tmps(self) -> None:
        for child in self.cache_dir.glob("*.tmp.*"):
            try:
                child.unlink()
            except OSError:
                pass

    def _write_manifest(self) -> None:
        doc = {
            "schema_version": SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "entries": self._entries,
        }
        tmp = _tmp_name(self._manifest_path)
        tmp.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True))
        _atomic_replace_file(tmp, self._manifest_path)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _path_for(self, content_key: str) -> Path:
        if not _is_safe_key(content_key):
            raise ValueError(
                f"unsafe content_key {content_key!r} — content_key_fn must return "
                f"a filesystem-safe component (e.g. a hex digest); no path "
                f"separators, '.'/'..', or empty keys")
        return self.cache_dir / f"{content_key}{self.SUFFIX}"

    def _digest_info(self, result) -> "tuple[str, int, int]":
        body = self._canonical_bytes(result)
        return hashlib.sha256(body).hexdigest(), len(body), self._record_count(result)

    def _check(self, content_key: str, expected_hash: str, result,
               meta: "dict[str, str]", entry: dict) -> str:
        """Return ``"ok"`` / ``"miss"``; raise :class:`ContentKeyCollisionError`
        on a PRESENT-but-different payload_hash. EVERY required field must be
        present and exact; completeness (digest/len/count) and generation binding
        are verified against the body + the manifest entry."""
        stored_hash = meta.get("payload_hash")
        if stored_hash is None:
            return "miss"           # metadata-less / foreign -> benign miss
        if stored_hash != expected_hash:
            raise ContentKeyCollisionError(
                f"content_key {content_key} stored payload_hash={stored_hash} "
                f"!= expected {expected_hash} (truncated-hash collision or buggy "
                f"content_key_fn)")
        if meta.get("content_key") != content_key:
            return "miss"
        if meta.get("fingerprint") != self.fingerprint:
            return "miss"
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            return "miss"
        if not self._validate_structure(result, meta):
            return "miss"
        # Result completeness — recompute over the body (design C1).
        digest, byte_len, count = self._digest_info(result)
        if meta.get("result_digest") != digest:
            return "miss"
        if meta.get("byte_len") != str(byte_len):
            return "miss"
        if meta.get("record_count") != str(count):
            return "miss"
        # Generation binding — payload file must agree with the manifest entry.
        if meta.get("generation_id") != entry.get("generation_id"):
            return "miss"
        if meta.get("result_digest") != entry.get("result_digest"):
            return "miss"
        return "ok"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def cached_keys(self) -> "set[str]":
        return set(self._entries)

    def has(self, content_key: str, expected_payload_hash: str) -> bool:
        entry = self._entries.get(content_key)
        if entry is None:
            return False
        if entry.get("payload_hash") != expected_payload_hash:
            raise ContentKeyCollisionError(
                f"manifest entry {content_key} payload_hash="
                f"{entry.get('payload_hash')} != expected {expected_payload_hash}")
        path = self._path_for(content_key)
        if not path.exists():
            return False
        read = self._read_file(path)
        if read is None:
            return False
        result, meta = read
        return self._check(content_key, expected_payload_hash, result, meta, entry) == "ok"

    def load(self, content_key: str, expected_payload_hash: str):
        entry = self._entries.get(content_key)
        if entry is None:
            raise RuntimeError(f"load() on uncommitted key {content_key}")
        if entry.get("payload_hash") != expected_payload_hash:
            raise ContentKeyCollisionError(
                f"manifest entry {content_key} payload_hash="
                f"{entry.get('payload_hash')} != expected {expected_payload_hash}")
        path = self._path_for(content_key)
        read = self._read_file(path)
        if read is None:
            raise RuntimeError(f"checkpoint {content_key} unreadable")
        result, meta = read
        status = self._check(content_key, expected_payload_hash, result, meta, entry)
        if status != "ok":
            raise RuntimeError(
                f"checkpoint {content_key} failed identity validation (stale)")
        return result

    def commit(self, content_key: str, payload_hash: str, result) -> None:
        self._validate_commit(result)
        digest, byte_len, count = self._digest_info(result)
        generation_id = uuid.uuid4().hex
        extra = self._commit_extra_meta(result)
        meta = {
            "content_key": content_key,
            "payload_hash": payload_hash,
            "fingerprint": self.fingerprint,
            "schema_version": str(SCHEMA_VERSION),
            "result_digest": digest,
            "byte_len": str(byte_len),
            "record_count": str(count),
            "generation_id": generation_id,
        }
        meta.update(extra)
        final = self._path_for(content_key)
        tmp = _tmp_name(final)
        self._write_file(tmp, result, meta)
        _atomic_replace_file(tmp, final)
        # Manifest is the atomic commit boundary — written last, same protocol.
        entry = {
            "payload_hash": payload_hash,
            "schema_version": SCHEMA_VERSION,
            "result_digest": digest,
            "byte_len": byte_len,
            "record_count": count,
            "generation_id": generation_id,
            "committed_at": time.time(),
        }
        entry.update(extra)
        # Publish in-memory only AFTER the manifest commit succeeds — if the
        # manifest write fails, roll back so this instance never reports has()==True
        # for a payload the on-disk commit boundary doesn't vouch for.
        prior = self._entries.get(content_key, _MISSING)
        self._entries[content_key] = entry
        try:
            self._write_manifest()
        except Exception:
            if prior is _MISSING:
                self._entries.pop(content_key, None)
            else:
                self._entries[content_key] = prior
            raise

    def _pairs(self, key_to_hash: "dict[str, str] | set[str]"):
        if isinstance(key_to_hash, dict):
            return sorted(key_to_hash.items())
        return sorted(
            (k, self._entries.get(k, {}).get("payload_hash", ""))
            for k in key_to_hash)

    def load_all(self, key_to_hash: "dict[str, str] | set[str]") -> list:
        return [self.load(k, h) for k, h in self._pairs(key_to_hash)]

    def iter_results(self, key_to_hash: "dict[str, str] | set[str]") -> Iterator:
        for k, h in self._pairs(key_to_hash):
            yield self.load(k, h)

    def cleanup(self) -> None:
        import shutil
        shutil.rmtree(self.cache_dir, ignore_errors=True)
