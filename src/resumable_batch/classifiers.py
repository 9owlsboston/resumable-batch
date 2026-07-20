"""Default, domain-agnostic error classifiers.

Callers normally inject domain-specific classifiers (e.g. the CUD
``is_cud_oom_error`` / ``is_transient_error``); these generic defaults exist so
the engine is usable out of the box.
"""

from __future__ import annotations

import socket


def default_is_oom(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("out of memory" in text or "memory exceeded" in text
            or "low memory" in text or "e_runaway_query" in text)


def default_is_transient(exc: Exception) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
        return True
    text = str(exc).lower()
    markers = ("timed out", "timeout", "temporarily unavailable",
               "connection reset", "service unavailable", "throttl")
    return any(m in text for m in markers)
