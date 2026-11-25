"""Bringing things back from the Windows Recycle Bin.

Each drive keeps its bin in X:\\$Recycle.Bin\\<your user id>\\. For every item there are two entries: $Rxxxxxx (the
file or folder itself, renamed) and $Ixxxxxx (where it came from, its size and when it was deleted). Restoring is
what File Explorer does: move $R back to where it came from, then remove $I.
"""
import os
import struct
import subprocess
from pathlib import Path

from . import core
from .core import UserError

_sid = None
EPOCH_DIFF = 11644473600  # seconds between 1601 (Windows file times) and 1970


def user_sid() -> str:
    global _sid
    if _sid is None:
        try:
            out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15,
                                 creationflags=core.NO_WINDOW).stdout.strip()
            _sid = out.rsplit(",", 1)[-1].strip().strip('"')
        except Exception:
            _sid = ""
    return _sid


def bin_folder(drive: str) -> Path:
    return Path(drive.rstrip("\\/") + "\\", "$Recycle.Bin", user_sid())


def read_info(path: Path):
    """(original path, size, deleted at) from a $I file, or None."""
    try:
        b = path.read_bytes()
        version = struct.unpack_from("<q", b, 0)[0]
        size, filetime = struct.unpack_from("<qq", b, 8)
        if version == 2:
            n = struct.unpack_from("<i", b, 24)[0]
            original = b[28:28 + n * 2].decode("utf-16-le").rstrip("\0")
        else:
            original = b[24:24 + 520].decode("utf-16-le").split("\0", 1)[0]
        return original, size, filetime / 1e7 - EPOCH_DIFF
    except (OSError, struct.error, UnicodeDecodeError):
        return None


def find(original: str, near: float = None):
    """The bin entry for something that was at `original` ({'r': $R path, 'i': $I path, 'deleted': time}), the one
    deleted closest to `near` when there are several, or None if it isn't in the bin (any more)."""
    folder = bin_folder(os.path.splitdrive(original)[0])
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    best = None
    for name in names:
        if not name.startswith("$I"):
            continue
        info = read_info(folder / name)
        if not info or os.path.normcase(info[0]) != os.path.normcase(original):
            continue
        r = folder / ("$R" + name[2:])
        if not r.exists():
            continue
        cand = {"r": r, "i": folder / name, "deleted": info[2]}
        if best is None or (near is not None and abs(info[2] - near) < abs(best["deleted"] - near)):
            best = cand
    return best


def _long(p: str) -> str:
    """A path Windows accepts at any length (restoring something that was moved up because its paths were too long)."""
    p = os.path.abspath(p)
    return p if p.startswith("\\\\?\\") else "\\\\?\\" + p


def restore(binned: str, to: str = None, near: float = None) -> str:
    """Put an item back from the Recycle Bin, at `to` (default: where it was deleted from). Returns where it went."""
    entry = find(binned, near)
    if not entry:
        raise UserError("It's no longer in the Recycle Bin (emptied, or restored already).")
    target = to or binned
    if os.path.lexists(_long(target)):
        raise UserError(f"Something is already at {target}, so it wasn't put back. Move that first.")
    os.makedirs(_long(os.path.dirname(target)), exist_ok=True)
    os.rename(_long(str(entry["r"])), _long(target))
    try:
        entry["i"].unlink()
    except OSError:
        pass
    return target
