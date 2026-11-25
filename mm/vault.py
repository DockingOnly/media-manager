"""API keys and passwords, kept in Windows Credential Manager instead of config.json.

Credential Manager encrypts them with your Windows login, so they can't be read by copying config.json, by a
backup, or by anyone signed in as someone else. They show up in Control Panel → Credential Manager → Windows
Credentials as "media-manager/<name>".

Only the app and the command line switch this on (`enabled`), so tests never touch your real credentials. If
Credential Manager can't be used, keys stay in config.json as before.
"""
import ctypes
import sys
import threading

SECRETS = ("tmdb_key", "omdb_key", "fanart_key", "os_key", "os_pass", "jellyfin_key", "trakt_secret")
PREFIX = "media-manager/"
enabled = False
_cache = {}
_lock = threading.Lock()

if sys.platform == "win32":
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", _FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
    _adv.CredReadW.restype = wintypes.BOOL
    _adv.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    _adv.CredWriteW.restype = wintypes.BOOL
    _adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _adv.CredDeleteW.restype = wintypes.BOOL
    _adv.CredFree.argtypes = [ctypes.c_void_p]
    GENERIC, LOCAL_MACHINE, NOT_FOUND = 1, 2, 1168


def available() -> bool:
    return enabled and sys.platform == "win32"


def _read(name: str):
    p = ctypes.POINTER(_CREDENTIAL)()
    if not _adv.CredReadW(PREFIX + name, GENERIC, 0, ctypes.byref(p)):
        if ctypes.get_last_error() == NOT_FOUND:
            return None
        raise OSError(ctypes.get_last_error(), "Credential Manager couldn't be read")
    try:
        c = p.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
    finally:
        _adv.CredFree(p)


def _write(name: str, value: str) -> None:
    if not value:
        if not _adv.CredDeleteW(PREFIX + name, GENERIC, 0) and ctypes.get_last_error() != NOT_FOUND:
            raise OSError(ctypes.get_last_error(), "Credential Manager couldn't be changed")
        return
    blob = value.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    c = _CREDENTIAL(Type=GENERIC, TargetName=PREFIX + name, CredentialBlobSize=len(blob),
                    CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), Persist=LOCAL_MACHINE,
                    UserName="media-manager", Comment="Media manager: " + name)
    if not _adv.CredWriteW(ctypes.byref(c), 0):
        raise OSError(ctypes.get_last_error(), "Credential Manager couldn't be written")


def get(name: str) -> str:
    with _lock:
        if name not in _cache:
            _cache[name] = _read(name) or ""
        return _cache[name]


def put(name: str, value: str) -> None:
    with _lock:
        _write(name, value or "")
        _cache[name] = value or ""


def merge_into(cfg: dict) -> dict:
    """config.json's settings with the keys from Credential Manager added (what the rest of the app reads)."""
    if not available():
        return cfg
    try:
        for name in SECRETS:
            v = get(name)
            if v and not cfg.get(name):  # a key still in config.json (not moved yet) wins
                cfg[name] = v
    except OSError:
        pass
    return cfg


def take_from(cfg: dict) -> bool:
    """Move any keys in `cfg` into Credential Manager, removing them from it. True if any moved."""
    if not available():
        return False
    moved = False
    for name in SECRETS:
        if name in cfg:
            try:
                put(name, cfg[name])
            except OSError:
                continue  # stays in config.json
            del cfg[name]
            moved = True
    return moved
