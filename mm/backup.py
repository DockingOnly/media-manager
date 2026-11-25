"""Daily backups of the things you can't get back by scanning again: your matches, your own metadata edits,
rename history (for undo), the History list and your settings.

Each backup is a folder in backups\\ named by date and time. The app makes one when it starts and once a day while
it runs, and keeps the newest 10. Restoring one first backs up what you have now, so a restore can be undone too.
API keys are never in a backup (they live in Windows Credential Manager, or are left out).
"""
import json
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import core, vault
from .core import UserError

KEEP = 10
enabled = False   # only the app and the command line make backups, not tests
_lock = threading.Lock()
WHAT = {"CONFIG": "Settings", "MATCHES": "Movie matches", "TV_MATCHES": "TV show matches", "EDITS": "Your metadata edits",
        "RENAME_LOG": "Movie rename undo", "TV_RENAME_LOG": "TV rename undo", "HISTORY": "History"}
REASONS = {"daily": "Daily", "manual": "Made by you", "before-restore": "Before a restore"}


def folder() -> Path:
    return core.BACKUPS


def _files() -> dict:
    return {name: getattr(core, name) for name in WHAT}


def make(reason: str = "manual") -> dict:
    """Copy the files now. Returns the new backup's listing entry."""
    with _lock:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest = folder() / f"{stamp}_{reason}"
        n = 1
        while dest.exists():
            n += 1
            dest = folder() / f"{stamp}-{n}_{reason}"
        dest.mkdir(parents=True)
        saved = {}
        for name, src in _files().items():
            if not src.exists():
                continue
            data = core.load_json(src, None)
            if data is None:
                continue
            if name == "CONFIG":
                data = {k: v for k, v in data.items() if k not in vault.SECRETS}  # never copy keys around
            (dest / src.name).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            saved[name] = src.name
        (dest / "backup.json").write_text(json.dumps({"time": time.time(), "reason": reason, "files": saved}), encoding="utf-8")
        _prune()
        return _entry(dest)


def _all() -> list:
    try:
        return sorted((p for p in folder().iterdir() if p.is_dir() and (p / "backup.json").exists()), reverse=True)
    except OSError:
        return []


def _prune() -> None:
    for old in _all()[KEEP:]:
        shutil.rmtree(old, ignore_errors=True)


def _entry(p: Path) -> dict:
    try:
        meta = json.loads((p / "backup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    files = meta.get("files") or {}
    size = sum((p / f).stat().st_size for f in files.values() if (p / f).exists())
    return {"id": p.name, "time": meta.get("time"), "reason": REASONS.get(meta.get("reason"), meta.get("reason") or ""),
            "what": [WHAT[n] for n in files if n in WHAT], "size": size}


def listing() -> list:
    return [_entry(p) for p in _all()]


def daily() -> dict:
    """Make today's backup if there isn't one yet."""
    if not enabled:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    if any(p.name.startswith(today) for p in _all()):
        return None
    return make("daily")


def restore(backup_id: str) -> dict:
    """Put a backup's files back (after backing up what's there now). Returns what was restored."""
    src = next((p for p in _all() if p.name == backup_id), None)
    if src is None or "/" in backup_id or "\\" in backup_id:
        raise UserError("That backup is no longer there.")
    meta = json.loads((src / "backup.json").read_text(encoding="utf-8"))
    files = {n: f for n, f in (meta.get("files") or {}).items() if n in WHAT}
    if not files:
        raise UserError("That backup is empty.")
    before = make("before-restore")
    restored = []
    with core.lock:
        for name, fname in files.items():
            data = core.load_json(src / fname, None)
            if data is None:
                continue
            target = getattr(core, name)
            if name == "CONFIG":
                current = core.load_json(target, {})
                data.update({k: v for k, v in current.items() if k in vault.SECRETS})  # keep the keys you have now
            core.save_json(target, data)
            restored.append(WHAT[name])
    return {"restored": restored, "undo": before["id"]}


def loop() -> None:
    while True:
        try:
            daily()
        except Exception:
            pass
        time.sleep(3600)


def start() -> None:
    threading.Thread(target=loop, daemon=True, name="backup").start()
