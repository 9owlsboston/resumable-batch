"""Shared fixtures."""

from __future__ import annotations

import pytest

import resumable_batch as rb


@pytest.fixture
def held_lock(tmp_path):
    lock = rb.make_lock(tmp_path / ".batch-checkpoint.lock")
    lock.acquire()
    yield lock
    lock.release()
