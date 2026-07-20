"""Exception taxonomy for the resumable-batch engine.

These are the only exceptions the engine raises on its own behalf; a caller's
``execute`` may raise anything, which the router classifies (OOM / transient /
fatal) per :mod:`resumable_batch.engine`.
"""

from __future__ import annotations


class ContentKeyCollisionError(Exception):
    """A stored/live result's ``payload_hash`` != the expected hash for its key.

    Signals a truncated-hash collision, a buggy ``content_key_fn``, or a stale
    on-disk file. NEVER swallowed into a cache hit — surfaced so the run fails
    loud rather than serving a wrong result.
    """


class CacheLockedError(Exception):
    """Another process holds the run()-scope cache lock."""


class FsUnsupportedError(Exception):
    """The resolved cache filesystem is off the durable allowlist under REQUIRE."""


class RetryAfterError(Exception):
    """A server ``Retry-After`` value was malformed or exceeded the operational
    bound (``RetryPolicy.max_server_delay``). Fatal — the engine refuses to guess
    or to sleep on an unbounded/hostile delay (design §4.2)."""
