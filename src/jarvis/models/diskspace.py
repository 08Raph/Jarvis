"""Download-size and free-disk-space policy for model files (Stage 5, D-9).

Everything here is integer-byte arithmetic with documented rounding, so it can be tested without a network or real files.

SIZE POLICY (``size_limit``)
  The catalog's ``approx_size_bytes`` is the baseline estimate. A download is stopped as soon as MORE than 115 % of it has been received:
  ``limit = estimate * 115 // 100`` (integer division, rounded DOWN, so the limit is never more generous than 115 %).
  A file of exactly ``limit`` bytes is accepted (inclusive); ``limit + 1`` bytes is refused.
  When the host also states the file's exact size, the effective limit is the smaller of the two numbers: a file that grows past the size
  the host itself declared is stopped too.

DISK POLICY (``required_free``)
  Before anything is written, free bytes on the filesystem that will hold the model are compared with the operation's own peak ADDITIONAL need
  plus a safety margin; the operation is refused when free space is lower. ``margin = max(MIN_MARGIN, need // 10)`` (1 GB or 10 %, whichever is
  larger) so the volume is not run to zero. The need is calculated per operation (``need_for_download`` / ``need_for_copy``); no fixed figure is assumed.
  Free space is measured with ``shutil.disk_usage`` on the nearest existing parent of the destination. If that call fails or returns nonsense, the
  operation is REFUSED (fail closed): ``DiskSpaceError``.

LIMITS (read before relying on this)
  * ``shutil.disk_usage`` is a point-in-time reading; another process can use the space afterwards. A recheck every ``RECHECK_EVERY`` bytes narrows
    the window, and an out-of-space error during a write is translated into ``DiskSpaceError`` with cleanup, but nothing here reserves space.
  * On POSIX the figure is the space available to an unprivileged user. On Windows CPython reports the volume's total free bytes, which can
    overstate what a quota-limited account may use. Windows behaviour has NOT been verified on a Windows machine.
  * Compression, sparse files, antivirus scanning copies, and a separate temp volume are not modelled.
"""
from __future__ import annotations

import errno
import shutil
from pathlib import Path

from jarvis.models.errors import IntegrityError

SIZE_NUMERATOR, SIZE_DENOMINATOR = 115, 100
MIN_MARGIN = 1_000_000_000          # 1 GB (decimal)
META_OVERHEAD = 1_048_576           # licence text, meta.json and filesystem rounding for the small files
RECHECK_EVERY = 256 * 1024 * 1024   # re-measure free space about every 256 MiB while downloading


MAX_EXPECTED_SIZE = 10 ** 13        # 10 TB: anything larger is not a plausible single-file model and is rejected as a typo


class DiskSpaceError(IntegrityError):
    """Not enough free disk space, or free space could not be determined. The message contains numbers only (no paths)."""


def size_limit(estimate_bytes: int, declared_bytes: int | None = None) -> int:
    """Maximum number of bytes a download may reach: 115 % of the estimate (rounded down), or the host-declared size if that is smaller and known."""
    if estimate_bytes <= 0:
        raise ValueError("a positive size estimate is required")
    limit = estimate_bytes * SIZE_NUMERATOR // SIZE_DENOMINATOR
    if declared_bytes and declared_bytes > 0:
        limit = min(limit, int(declared_bytes))
    return limit


def parse_expected_size(value) -> int:
    """Strict parser for a user-typed expected size in bytes (``add-gguf --expected-size-bytes``). Accepts only plain ASCII decimal digits with no
    sign, spaces, separators, exponent or leading zero, and 1 <= n <= MAX_EXPECTED_SIZE. Anything else raises ValueError (nothing is guessed)."""
    if not isinstance(value, str) or not (value.isascii() and value.isdigit()) or value.startswith("0"):
        raise ValueError("the expected size must be a whole number of bytes made of digits only, greater than zero")
    n = int(value)
    if not 1 <= n <= MAX_EXPECTED_SIZE:
        raise ValueError(f"the expected size must be between 1 and {MAX_EXPECTED_SIZE} bytes")
    return n


def parse_content_length(value) -> int | None:
    """A Content-Length header as a non-negative integer, or None when it is absent, malformed, negative or implausible (treated as 'unknown')."""
    if value is None:
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n < 1 << 50 else None


def free_bytes(path: str | Path) -> int:
    """Free bytes on the filesystem that holds ``path`` (or its nearest existing parent). Raises DiskSpaceError if it cannot be determined."""
    p = Path(path)
    try:
        for q in (p, *p.parents):
            if q.exists():
                free = shutil.disk_usage(q).free
                break
        else:
            raise OSError("no existing parent")
        if not isinstance(free, int) or free < 0:
            raise OSError("implausible value")
        return free
    except (OSError, ValueError, TypeError):
        raise DiskSpaceError("could not determine the free disk space for the model folder; refusing to write a large file without that check") from None


def margin_for(need: int) -> int:
    return max(MIN_MARGIN, need // 10)


def required_free(need: int) -> int:
    return need + margin_for(need)


def need_for_download(limit: int) -> int:
    """Peak additional bytes of a catalog fetch: the partial file grows to at most ``limit``; installing it is a rename on the same volume (no copy)."""
    return limit + META_OVERHEAD


def need_for_copy(file_size: int) -> int:
    """Peak additional bytes of ``add-gguf`` (copy mode): the source stays where it is and a full second copy is staged inside the model folder."""
    return file_size + META_OVERHEAD


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


def ensure_free(dest: str | Path, need: int, what: str) -> None:
    """Refuse (DiskSpaceError) unless free space on dest's volume >= need + margin. The message states the required and available amounts only."""
    free, req = free_bytes(dest), required_free(need)
    if free < req:
        raise DiskSpaceError(f"not enough free disk space to {what}: {_gb(req)} needed (including a safety margin), {_gb(free)} available. Nothing was written.")


def translate_oserror(exc: OSError, what: str) -> DiskSpaceError | None:
    """If an OSError means the disk is full, return a path-free DiskSpaceError for it; otherwise None (the caller re-raises the original)."""
    if getattr(exc, "errno", None) in (errno.ENOSPC, errno.EDQUOT) or getattr(exc, "winerror", None) in (112, 39):
        return DiskSpaceError(f"the disk filled up while trying to {what}. The partial files were removed and no model was installed.")
    return None
