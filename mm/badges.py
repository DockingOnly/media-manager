"""Poster badges: small labels on movie posters for what the file holds (4K, HDR or Dolby Vision, Atmos or 7.1, the
rating), so you can see at a glance in Jellyfin which copy is which.

The original poster is always kept: the first time a poster gets badges, it's copied to badge-originals\\ in the
app folder, and the badges are drawn on a fresh copy of it each time, so they never pile up. Remove badges puts
every original back, byte for byte. When a poster is replaced (new artwork downloaded or chosen), the new one
becomes the original. With "keep posters up to date" on, new artwork gets badges when it's downloaded.
"""
import hashlib
import io
import os
import shutil
import threading
from pathlib import Path

from . import core
from .core import UserError, key_of

KINDS = ("resolution", "hdr", "audio", "rating")
_lock = threading.Lock()
_fonts = {}
FONT_FILES = ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf")


def store() -> Path:
    return core.BADGES.parent / "badge-originals"


def settings(cfg: dict = None) -> dict:
    cfg = core.load_config() if cfg is None else cfg
    show = cfg.get("badge_show") or {"resolution": True, "hdr": True, "audio": True, "rating": False}
    return {"on": bool(cfg.get("badges_on")), "show": {k: bool(show.get(k)) for k in KINDS},
            "position": cfg.get("badge_position") if cfg.get("badge_position") in ("top", "bottom") else "top"}


def font(size: int):
    from PIL import ImageFont
    size = max(8, int(size))
    if size not in _fonts:
        f = None
        for name in FONT_FILES:
            try:
                f = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        _fonts[size] = f or ImageFont.load_default(size)
    return _fonts[size]


# ---------------------------------------------------------------- what a movie's badges say

def labels(row: dict, show: dict) -> list:
    """[(text, style)] for one movie, from what's in its file and .nfo."""
    info = row.get("info") or {}
    out = []
    if show.get("resolution"):
        h = _height(row)
        text = "4K" if h >= 2000 else f"{h}p" if h >= 720 else "SD" if h else ""
        if text:
            out.append((text, "light"))
    if show.get("hdr"):
        hdr = info.get("hdr") or ""
        text = {"Dolby Vision": "DV", "HDR10": "HDR", "HLG": "HLG"}.get(hdr, "")
        if text:
            out.append((text, "gold"))
    if show.get("audio"):
        a = info.get("audio") or ""
        text = ("ATMOS" if "Atmos" in a else "DTS:X" if "DTS:X" in a else "DTS-HD" if "DTS-HD" in a else
                "TRUEHD" if "TrueHD" in a else "7.1" if a.endswith("7.1") else "5.1" if a.endswith("5.1") else "")
        if text:
            out.append((text, "dark"))
    if show.get("rating") and info.get("rating"):
        out.append((f"{info['rating']:.1f}", "star"))
    return out


def signature(lbls: list, position: str) -> str:
    return "|".join(t for t, _ in lbls) + "@" + position


def render(src: Path, lbls: list, position: str = "top") -> bytes:
    """The poster with its badges drawn on (in the poster's own format)."""
    from PIL import Image, ImageDraw
    with Image.open(src) as im:
        fmt = "PNG" if (im.format or "").upper() == "PNG" or src.suffix.lower() == ".png" else "JPEG"
        base = im.convert("RGBA")
    W, H = base.size
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    ph = max(14, round(W * 0.082))
    pad, gap, margin, radius = round(ph * 0.36), round(ph * 0.2), round(W * 0.035), round(ph * 0.26)
    f = font(ph * 0.56)
    x = margin
    y = margin if position == "top" else H - margin - ph
    styles = {"light": ((246, 246, 246, 235), (16, 16, 18, 255)), "gold": ((18, 18, 22, 215), (245, 197, 66, 255)),
              "dark": ((18, 18, 22, 215), (255, 255, 255, 255)), "star": ((18, 18, 22, 215), (255, 255, 255, 255))}
    for text, style in lbls:
        bg, fg = styles.get(style, styles["dark"])
        star = style == "star"
        sw = round(ph * 0.5) if star else 0
        w = round(d.textlength(text, font=f)) + pad * 2 + (sw + round(ph * 0.12) if star else 0)
        if x + w > W - margin:
            break
        d.rounded_rectangle((x, y, x + w, y + ph), radius=radius, fill=bg, outline=(255, 255, 255, 46), width=max(1, ph // 28))
        tx = x + pad
        if star:
            import math
            cx, cy, r = tx + sw / 2, y + ph / 2, sw / 2
            pts = [(cx + (r if i % 2 == 0 else r * 0.45) * math.sin(math.pi * i / 5), cy - (r if i % 2 == 0 else r * 0.45) * math.cos(math.pi * i / 5))
                   for i in range(10)]
            d.polygon(pts, fill=(245, 197, 66, 255))
            tx += sw + round(ph * 0.12)
        d.text((tx, y + ph / 2), text, font=f, fill=fg, anchor="lm")
        x += w + gap
    out = io.BytesIO()
    img = Image.alpha_composite(base, layer)
    if fmt == "PNG":
        img.save(out, "PNG")
    else:
        img.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


# ---------------------------------------------------------------- badges on one poster

def _index() -> dict:
    return core.load_json(core.BADGES, {})


def _ours(path: Path, rec: dict) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return bool(rec) and rec.get("size") == st.st_size and rec.get("mtime") == int(st.st_mtime)


def original_of(path: Path) -> Path:
    """The poster as it was before badges (the poster itself when it has none)."""
    rec = _index().get(key_of(path))
    if rec and _ours(path, rec) and (store() / rec["orig"]).is_file():
        return store() / rec["orig"]
    return path


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".new")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def apply(path: Path, lbls: list, position: str) -> bool:
    """Draw these badges on this poster (from its original). True if the poster changed."""
    with _lock:
        idx = _index()
        k = key_of(path)
        rec = idx.get(k)
        ours = _ours(path, rec)
        if ours and rec.get("sig") == signature(lbls, position):
            return False
        if not lbls:
            return _remove(path, idx) if rec else False
        store().mkdir(parents=True, exist_ok=True)
        if ours:
            orig = store() / rec["orig"]
            if not orig.is_file():  # the original went missing: leave this poster alone from now on
                idx.pop(k, None)
                core.save_json(core.BADGES, idx)
                return False
        else:  # the first time, or the artwork was replaced since: this file is the original
            orig = store() / f"{hashlib.sha1(k.encode()).hexdigest()[:16]}{path.suffix.lower()}"
            shutil.copy2(path, orig)
        _write(path, render(orig, lbls, position))
        st = path.stat()
        idx[k] = {"orig": orig.name, "size": st.st_size, "mtime": int(st.st_mtime), "sig": signature(lbls, position), "path": str(path)}
        core.save_json(core.BADGES, idx)
    core.changed(path)
    return True


def _remove(path: Path, idx: dict) -> bool:
    k = key_of(path)
    rec = idx.pop(k, None)
    changed = False
    if rec:
        orig = store() / rec["orig"]
        if _ours(path, rec) and orig.is_file():
            _write(path, orig.read_bytes())
            changed = True
        try:
            orig.unlink(missing_ok=True)
        except OSError:
            pass
    core.save_json(core.BADGES, idx)
    if changed:
        core.changed(path)
    return changed


def remove(path: Path) -> bool:
    with _lock:
        return _remove(path, _index())


# ---------------------------------------------------------------- the whole library

def poster_of(row: dict, kind: str = "movies", own: bool = True):
    from . import artwork
    if kind == "tv":
        folder = Path(row["full"]) if row.get("full") else None
        return next((folder / n for n in artwork.KINDS["poster"][3] if folder and (folder / n).is_file()), None)
    video = Path(row["full"]) if row.get("full") else None
    return artwork.existing(video, own, "poster") if video else None


def _height(row: dict) -> int:
    res = str((row.get("info") or {}).get("resolution") or row.get("resolution") or "")
    return int(res[:-1]) if res[:-1].isdigit() else 0


def movie_posters(items: list, root: Path) -> dict:
    """poster file -> the library row whose details its badges show (the best copy when several share it)."""
    from . import library
    folders = library.by_folder(items, root)
    out = {}
    for it in items:
        video = Path(root, it["path"])
        from . import artwork
        p = artwork.existing(video, library.own_folder(it, folders, root), "poster")
        if not p:
            continue
        best = out.get(p)
        if best is None or _height(it) > _height(best):
            out[p] = it
    return out


def apply_all(job: dict = None, items: list = None, root: Path = None) -> dict:
    from . import library
    s = settings()
    if root is None:
        root = core.require_scan()
    if items is None:
        with core.lock:
            items = library.current_items()
    posters = movie_posters(items, root)
    if job:
        core.job_step(job, "Adding badges to posters", len(posters))
    res = {"changed": 0, "same": 0, "failed": []}
    for p, it in posters.items():
        if job and job["cancel"]:
            break
        try:
            if apply(p, labels(it, s["show"]), s["position"]):
                res["changed"] += 1
            else:
                res["same"] += 1
        except (OSError, ValueError) as err:  # (an image Pillow can't read)
            res["failed"].append({"name": p.name, "error": getattr(err, "strerror", None) or str(err)})
        finally:
            if job:
                core.tick(job)
    res["badged"] = len(_index())
    return res


def remove_all(job: dict = None) -> dict:
    idx = _index()
    if job:
        core.job_step(job, "Removing badges", len(idx))
    res = {"restored": 0, "failed": []}
    for k, rec in list(idx.items()):
        try:
            if remove(Path(rec.get("path") or k)):
                res["restored"] += 1
        except OSError as err:
            res["failed"].append({"name": Path(rec.get("path") or k).name, "error": err.strerror or str(err)})
        finally:
            if job:
                core.tick(job)
    res["badged"] = len(_index())
    return res


def keep_up(items: list, root: Path) -> None:
    """After new artwork: badges on the new posters too, when that's switched on."""
    if not settings()["on"]:
        return
    try:
        apply_all(items=items, root=root)
    except (UserError, OSError, ImportError):
        pass


def preview(item_id: str, show: dict, position: str, plain: bool = False):
    from . import library, movie
    with core.lock:
        it, items, root = movie.find(item_id)
        own = library.own_folder(it, library.by_folder(items, root), root)
    from . import artwork
    p = artwork.existing(Path(root, it["path"]), own, "poster")
    if not p:
        raise UserError("This movie has no poster yet.")
    orig = original_of(p)
    mime = "image/png" if orig.suffix.lower() == ".png" else "image/jpeg"
    return (orig.read_bytes() if plain else render(orig, labels(it, show), position)), mime


def count() -> int:
    return len(_index())
