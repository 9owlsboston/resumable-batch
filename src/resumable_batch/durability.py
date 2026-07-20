"""Filesystem durability policy + atomic-write primitives (cross-OS).

Correctness does **not** depend on directory-fsync strength because every commit
is generation-bound and result-digested (see
:mod:`resumable_batch.stores.base` §4.4): a torn write is detected and
recomputed, never served. Directory ``fsync`` is therefore a *performance*
hardening that degrades to a no-op where the platform can't provide it (Windows).

The FS-type gate is advisory: a successful runtime rename/fsync probe CANNOT prove
power-loss durability, so caching is enabled only on an allowlisted, resolved
(symlink-followed) filesystem — never on a probe.
"""

from __future__ import annotations

import enum
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Atomic-write primitives (module-level so tests can spy on call ordering)
# ---------------------------------------------------------------------------

def _fsync_file(path: os.PathLike | str) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(dirpath: os.PathLike | str) -> None:
    """fsync a directory so a rename is durable. No-op on Windows, where a
    directory handle can't be opened O_RDONLY + fsync'd — correctness is carried
    by generation binding instead (design §4.3/§4.4)."""
    if _IS_WINDOWS:
        return
    fd = os.open(str(dirpath), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_replace_file(tmp_path: Path, final_path: Path) -> None:
    """Durability-ordered finalize: fsync file -> os.replace -> fsync dir.

    ``os.replace`` is atomic on ext4/xfs/NTFS; where it isn't, ``DurabilityPolicy``
    fails closed and caching is disabled (design §4.4)."""
    _fsync_file(tmp_path)
    os.replace(str(tmp_path), str(final_path))
    _fsync_dir(final_path.parent)


def _tmp_name(final_path: Path) -> Path:
    return final_path.with_name(
        f"{final_path.name}.tmp.{os.getpid()}.{os.urandom(4).hex()}")


# ---------------------------------------------------------------------------
# FS-type policy
# ---------------------------------------------------------------------------

class FsPolicy(enum.Enum):
    FAIL_CLOSED = "fail_closed"   # default: disable caching on unknown FS
    REQUIRE = "require"           # hard-error on unknown FS
    ALLOW = "allow"               # dev-only best-effort


# statfs f_type magic numbers -> friendly name (Linux).
_FS_MAGIC = {
    0xEF53: "ext",       # ext2/ext3/ext4 (indistinguishable by magic)
    0x58465342: "xfs",
    0x9123683E: "btrfs",
    0x01021994: "tmpfs",
    0x794C7630: "overlay",
    0x6969: "nfs",
    0xFF534D42: "cifs",
    0x65735546: "fuse",
}

# Known-durable filesystem types (atomic rename + fsync durability guaranteed).
# NTFS is added for the Windows path (resolved via GetDriveType, below).
DURABLE_FS_ALLOWLIST = frozenset({"ext", "ext4", "xfs", "ntfs"})


def _statfs_f_type(path: str) -> int:
    """Return the statfs f_type magic for ``path`` (Linux). Raises on failure."""
    import ctypes
    import struct

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    buf = ctypes.create_string_buffer(120)
    if libc.statfs(os.fsencode(str(path)), buf) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(path))
    # struct statfs on x86_64 Linux: f_type is the first field, __fsword_t (8B).
    (f_type,) = struct.unpack_from("q", buf.raw, 0)
    return f_type & 0xFFFFFFFF


def _nearest_existing(path: os.PathLike | str) -> str:
    real = os.path.realpath(os.fspath(path))
    probe = real
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return probe


def _resolve_fs_type_windows(path: os.PathLike | str) -> str:
    """Classify a Windows volume: local fixed NTFS -> ``ntfs``; network/removable
    -> a non-durable label so caching fails closed (design §4.3)."""
    import ctypes

    probe = _nearest_existing(path)
    drive = os.path.splitdrive(os.path.abspath(probe))[0]
    root = (drive + "\\") if drive else None
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    # DRIVE_* constants: 2=removable, 3=fixed, 4=remote/network, 5=cdrom, 6=ramdisk.
    drive_type = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) if root else 0
    if drive_type == 4:
        return "network"
    if drive_type in (2, 5, 6):
        return "removable"
    if drive_type != 3:
        return f"unknown(drivetype={drive_type})"

    fs_name = ctypes.create_unicode_buffer(261)
    ok = kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), None, 0, None, None, None,
        fs_name, ctypes.sizeof(fs_name) // ctypes.sizeof(ctypes.c_wchar))
    if not ok:
        return "unknown(volinfo-failed)"
    return (fs_name.value or "").lower() or "unknown(empty-fsname)"


def resolve_fs_type(path: os.PathLike | str) -> str:
    """Resolve symlinks, then classify the FS type of the REAL path.

    Walks up to the nearest existing ancestor (the cache dir may not exist yet).
    Returns a friendly name (``ext``/``xfs``/``ntfs``/``tmpfs``/...) or an
    ``unknown(...)`` label when the type is unrecognised."""
    if _IS_WINDOWS:
        try:
            return _resolve_fs_type_windows(path)
        except Exception:  # noqa: BLE001 — any WinAPI failure -> unknown -> fail closed
            return "unknown(winapi-failed)"
    probe = _nearest_existing(path)
    try:
        magic = _statfs_f_type(probe)
    except OSError:
        return "unknown(statfs-failed)"
    return _FS_MAGIC.get(magic, f"unknown(0x{magic:x})")


def fs_cache_enabled(path: os.PathLike | str,
                     policy: FsPolicy = FsPolicy.FAIL_CLOSED) -> bool:
    """Decide whether the durable cache may be used on ``path``'s filesystem.

    Gates ONLY on the resolved statfs/volume FS-type allowlist — never on a
    runtime probe. Off-allowlist: ``FAIL_CLOSED`` (default) disables caching,
    ``REQUIRE`` raises, ``ALLOW`` opts in for dev."""
    from .errors import FsUnsupportedError

    fs_type = resolve_fs_type(path)
    if fs_type in DURABLE_FS_ALLOWLIST:
        return True
    if policy is FsPolicy.REQUIRE:
        raise FsUnsupportedError(
            f"cache path {path!s} is on non-durable FS {fs_type!r} (REQUIRE)")
    if policy is FsPolicy.ALLOW:
        logger.warning("cache FS %r off durable allowlist; ALLOW (dev best-effort): %s",
                       fs_type, path)
        return True
    logger.warning("cache FS %r off durable allowlist; disabling cache (FAIL_CLOSED): %s",
                   fs_type, path)
    return False
