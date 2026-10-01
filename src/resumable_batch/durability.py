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
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"
_IS_MACOS = sys.platform == "darwin"


# ---------------------------------------------------------------------------
# Atomic-write primitives (module-level so tests can spy on call ordering)
# ---------------------------------------------------------------------------

def _fsync_file(path: os.PathLike | str) -> None:
    # fsync needs a WRITABLE fd on Windows (os.fsync -> _commit); an O_RDONLY fd
    # raises EBADF there. O_RDWR works on both platforms for our (writable) files.
    fd = os.open(str(path), os.O_RDWR)
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
# NTFS is added for the Windows path (resolved via GetDriveType, below); apfs/hfs
# for the macOS path (resolved via Darwin statfs f_fstypename, below). As with
# Windows, macOS fsync strength (F_FULLFSYNC) is a performance detail only —
# correctness is carried by generation binding (design §4.3/§4.4), so the same
# atomic-rename guarantee that admits ext/xfs admits apfs/hfs.
DURABLE_FS_ALLOWLIST = frozenset({"ext", "ext4", "xfs", "ntfs", "apfs", "hfs"})


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


# Darwin ``struct statfs`` (64-bit-inode, <sys/mount.h>) field offsets. The FS
# name is carried directly as ``f_fstypename`` (a 16-byte char[]), which is far
# more robust than magic numbers:
#   f_bsize u32 @0, f_iosize i32 @4, f_blocks u64 @8, f_bfree u64 @16,
#   f_bavail u64 @24, f_files u64 @32, f_ffree u64 @40, f_fsid i32[2] @48,
#   f_owner u32 @56, f_type u32 @60, f_flags u32 @64, f_fssubtype u32 @68,
#   f_fstypename char[16] @72, f_mntonname char[1024] @88, ...  (struct ~2168B)
_DARWIN_STATFS_FSTYPENAME_OFF = 72
_DARWIN_MFSTYPENAMELEN = 16
_DARWIN_STATFS_BUFSZ = 4096  # over-allocate (struct is ~2168B) for safety.


def _statfs_fstypename_macos(path: str) -> str:
    """Return Darwin ``f_fstypename`` (e.g. ``apfs``/``hfs``) for ``path``.

    Raises on any failure so the caller falls back to ``unknown(...)`` and the
    cache fails closed — a wrong/garbage read can never *enable* caching. To make
    that invariant robust against a wrong offset or a corrupted buffer, the
    16-byte field is accepted only when it is a well-formed C string: a non-empty
    run of printable ASCII, a NUL terminator, and all-zero padding after it.
    Anything else raises."""
    import ctypes

    # libSystem (always loaded) carries statfs; CDLL(None) resolves it on macOS.
    libc = ctypes.CDLL(None, use_errno=True)
    # macOS 10.6+ exposes the 64-bit-inode struct under the bare ``statfs``
    # symbol; older SDKs need the explicit ``$INODE64`` alias — prefer it.
    statfs = getattr(libc, "statfs$INODE64", None) or libc.statfs
    buf = ctypes.create_string_buffer(_DARWIN_STATFS_BUFSZ)
    if statfs(os.fsencode(str(path)), buf) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(path))
    raw = buf.raw[_DARWIN_STATFS_FSTYPENAME_OFF:
                  _DARWIN_STATFS_FSTYPENAME_OFF + _DARWIN_MFSTYPENAMELEN]
    nul = raw.find(b"\x00")
    if nul <= 0:  # no terminator, or empty name -> malformed field
        raise ValueError(f"f_fstypename not a non-empty C string: {raw!r}")
    if raw[nul:] != b"\x00" * (_DARWIN_MFSTYPENAMELEN - nul):  # non-zero padding
        raise ValueError(f"f_fstypename padding not zeroed: {raw!r}")
    name = raw[:nul]
    if not name.isascii() or not name.replace(b"_", b"").isalnum():
        raise ValueError(f"f_fstypename not an fs-name token: {raw!r}")
    return name.decode("ascii").lower()


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
    Returns a friendly name (``ext``/``xfs``/``ntfs``/``apfs``/``tmpfs``/...) or
    an ``unknown(...)`` label when the type is unrecognised. Dispatch is
    per-platform: Windows volume info, macOS Darwin ``statfs f_fstypename``,
    otherwise Linux ``statfs`` magic."""
    if _IS_WINDOWS:
        try:
            return _resolve_fs_type_windows(path)
        except Exception:  # noqa: BLE001 — any WinAPI failure -> unknown -> fail closed
            return "unknown(winapi-failed)"
    probe = _nearest_existing(path)
    if _IS_MACOS:
        try:
            return _statfs_fstypename_macos(probe)
        except Exception:  # noqa: BLE001 — any failure -> unknown -> fail closed
            return "unknown(statfs-failed)"
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
