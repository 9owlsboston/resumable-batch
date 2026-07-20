"""resumable-batch — a domain-agnostic, content-addressed, crash-atomic
checkpoint + transient-retry engine for long-running chunked work.

The engine ships **no domain logic**: everything domain-specific (``execute``,
``content_key_fn``, ``combine``/``reduce``, ``is_oom``/``is_transient``,
``split_policy``, ``worker_setup``) is a caller-injected function. Results are an
opaque type served by a pluggable :class:`ResultStore`.

Quick start::

    from resumable_batch import (
        run_checkpointed, CheckpointItem, ParquetResultStore, make_lock,
    )

    lock = make_lock(cache_root / ".rb.lock").acquire()
    try:
        store = ParquetResultStore(cache_root / "job", lock=lock, fingerprint=fp)
        result = run_checkpointed(
            items, execute=execute, content_key_fn=ck_fn, combine=combine,
            store=store)
    finally:
        lock.release()
"""

from __future__ import annotations

from .classifiers import default_is_oom, default_is_transient
from .durability import (
    DURABLE_FS_ALLOWLIST,
    FsPolicy,
    fs_cache_enabled,
    resolve_fs_type,
)
from .engine import run_checkpointed
from .errors import (
    CacheLockedError,
    ContentKeyCollisionError,
    FsUnsupportedError,
    RetryAfterError,
)
from .locking import LockProvider, PosixFlockLock, WindowsLock, make_lock
from .model import (
    MANIFEST_NAME,
    SCHEMA_VERSION,
    CheckpointItem,
    CheckpointResult,
    RetryPolicy,
    SchedMeta,
    SplitPolicy,
)
from .stores import (
    AtomicManifestStore,
    JsonlResultStore,
    ParquetResultStore,
    ResultStore,
)

__version__ = "0.1.0"

__all__ = [
    # engine
    "run_checkpointed",
    # model
    "CheckpointItem",
    "CheckpointResult",
    "RetryPolicy",
    "SchedMeta",
    "SplitPolicy",
    "SCHEMA_VERSION",
    "MANIFEST_NAME",
    # stores
    "ResultStore",
    "AtomicManifestStore",
    "ParquetResultStore",
    "JsonlResultStore",
    # locking
    "LockProvider",
    "PosixFlockLock",
    "WindowsLock",
    "make_lock",
    # durability
    "FsPolicy",
    "fs_cache_enabled",
    "resolve_fs_type",
    "DURABLE_FS_ALLOWLIST",
    # classifiers
    "default_is_oom",
    "default_is_transient",
    # errors
    "ContentKeyCollisionError",
    "CacheLockedError",
    "FsUnsupportedError",
    "RetryAfterError",
]
