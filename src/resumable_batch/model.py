"""Core data model — items, policies, and the run result.

Serialization-agnostic by construction: nothing here imports pandas/pyarrow, and
a result is an opaque ``TypeVar`` (``Result``) the engine never inspects.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

# Bumped on any breaking change to the on-disk format so old caches invalidate
# cleanly. v2 adds the result-content digest + per-commit generation binding
# (design §4.4), an on-disk format change relative to the acr-analytics engine
# (which shipped SCHEMA_VERSION 1) — hence the bump to 2.
SCHEMA_VERSION = 2

MANIFEST_NAME = "manifest.json"

P = TypeVar("P")  # opaque caller payload
Result = TypeVar("Result")  # opaque result type (pd.DataFrame, list[dict], ...)


@dataclass(frozen=True)
class SchedMeta:
    """Scheduling/telemetry metadata ONLY — never a cache identity."""

    root_order: int = 0     # provenance of the original batch (logging/telemetry)
    range_start: int = 0    # positional span [start, end) — split bookkeeping, NOT the key
    range_end: int = 0
    target_size: int = 0    # the batch size this attempt runs at (shrunk on split)


@dataclass(frozen=True)
class CheckpointItem(Generic[P]):
    """An immutable unit of work: an opaque payload + scheduling metadata.

    Identity ``(content_key, full_payload_hash)`` is DERIVED by the coordinator
    via ``content_key_fn(payload)`` at admission — NOT stored here — so a caller
    can't hand-forge an identity that disagrees with its payload.
    """

    payload: P
    sched: SchedMeta = field(default_factory=SchedMeta)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with full jitter, plus an optional bounded
    server ``Retry-After`` hook (design §4.2)."""

    max_attempts: int = 4       # total tries per key on TRANSIENT error
    base_delay: float = 2.0     # seconds
    max_delay: float = 60.0
    multiplier: float = 2.0     # exponential: base * multiplier**(n-1), capped
    jitter: float = 0.5         # +/- fraction of the computed delay (full jitter)
    # Optional server-directed delay (e.g. HTTP Retry-After). Returns seconds or
    # None. Validated + clamped by ``max_server_delay`` in the transient branch.
    retry_after_fn: Callable[[Exception], "float | None"] | None = None
    # Operational ceiling for a server-directed delay. MUST be finite and >= 0
    # (design C3 — an inf bound makes the clamp meaningless). A server delay above
    # this bound is fatal rather than silently honored.
    max_server_delay: float = 300.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.max_server_delay) or self.max_server_delay < 0:
            raise ValueError(
                "RetryPolicy.max_server_delay must be finite and >= 0 "
                f"(got {self.max_server_delay!r})")


@dataclass(frozen=True)
class SplitPolicy(Generic[P]):
    """Domain-defined split trigger + splitter.

    ``should_split(exc, item)`` decides whether an item should be split on a given
    failure (OOM *or* a non-OOM domain condition like "shard too big"); ``split``
    returns the strictly-smaller children. The engine enforces strict progress and
    a depth cap regardless (design §4.5)."""

    should_split: Callable[[Exception, CheckpointItem], bool]
    split: Callable[[CheckpointItem], "list[CheckpointItem]"]


@dataclass
class CheckpointResult(Generic[Result]):
    """The stitched/finalized output plus run telemetry."""

    frame: Result                 # combine(...) output OR finalize(reduce fold)
    attempts: int                 # total executor calls (successful + retried + split)
    committed_keys: int           # distinct content_keys persisted THIS run
    resumed_keys: int             # content_keys served from cache (skipped this run)
    transient_retries: int
    splits: int

    @property
    def output(self) -> Result:
        """Serialization-agnostic alias for :attr:`frame`."""
        return self.frame
