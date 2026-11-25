"""One movie: its details, files and artwork, your edits, and opening its folder."""
import os
from pathlib import Path
from urllib.parse import quote

from . import artwork, core, library, nfo, tmdbapi
from .core import UserError, key_of

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def find(item_id: str):
    root = core.require_scan()
    items = library.annotate(library.with_matches(core.state["items"]), root)
    it = next((x for x in items if x["id"] == item_id), None)
    if not it:
        raise UserError("That movie is no longer in the list. Scan again.")
    return it, items, root


def confirmed_id(it: dict):
    m = it.get("match") or {}
    return m.get("tmdb_id") if m.get("status") in core.MATCHED else None


def image_file(it: dict, folders: dict, root: Path, kind: str):
    return artwork.existing(Path(root, it["path"]), library.own_folder(it, folders, root), kind)


def file_kind(p: Path) -> str:
    ext, low = p.suffix.lower(), p.name.lower()
    if ext in core.VIDEO_EXTS:
        return "trailer" if "trailer" in low or p.parent.name.lower() == "trailers" else "video"
    if ext in core.SUB_EXTS:
        return "subtitle"
    if ext == ".nfo":
        return "nfo"
    if ext in IMAGE_TYPES:
        return "image"
    return "other"


def movie_files(it: dict, folders: dict, root: Path) -> list:
    """Everything that belongs to this movie: its own files, and in its own folder everything else too."""
    video = Path(root, it["path"])
    files = [video] + library.sidecars(video)
    if library.own_folder(it, folders, root):
        seen = {key_of(f) for f in files}
        try:
            for e in sorted(video.parent.iterdir()):
                if e.is_file() and key_of(e) not in seen:
                    files.append(e)
                elif e.is_dir() and e.name.lower() == "trailers":
                    files += sorted(f for f in e.iterdir() if f.is_file())
        except OSError:
            pass
    out = []
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        out.append({"name": core.rel(f, video.parent), "kind": file_kind(f), "size": size,
                    "other_version": f.suffix.lower() in core.VIDEO_EXTS and f != video and file_kind(f) == "video"})
    return out


def details_from_tmdb(tmdb_id) -> dict:
    d = tmdbapi.tmdb(f"/movie/{tmdb_id}", append_to_response="credits,release_dates,external_ids,videos")
    info = nfo.read_nfo_root(nfo.parse_xml(nfo.build_nfo(d)))
    info["poster_url"] = tmdbapi.image_url(d.get("poster_path"), "w500")
    info["fanart_url"] = tmdbapi.image_url(d.get("backdrop_path"), "w1280")
    return info


def detail(item_id: str) -> dict:
    with core.lock:
        it, items, root = find(item_id)
    folders = library.by_folder(items, root)
    video = Path(root, it["path"])
    own = library.own_folder(it, folders, root)
    path = nfo.nfo_path(video, own)
    info, source = (nfo.read_nfo(path), "nfo") if path else (None, None)
    tmdb_id = confirmed_id(it)
    if not info and tmdb_id:
        try:
            info, source = details_from_tmdb(tmdb_id), "tmdb"
        except (core.TmdbError, KeyError, TypeError, ValueError):  # no details rather than no window
            info = None
    images, art = {}, []
    for kind, (label, *_rest) in artwork.KINDS.items():
        p = image_file(it, folders, root, kind)
        if p:
            images[kind] = f"/api/image?id={quote(item_id)}&kind={kind}&v={int(p.stat().st_mtime)}"
        art.append({"kind": kind, "label": label, "file": p.name if p else None, "url": images.get(kind)})
    if info:
        images.setdefault("poster", info.get("poster_url") or (it.get("match") or {}).get("poster"))
        if info.get("fanart_url"):
            images.setdefault("fanart", info["fanart_url"])
    return {"item": dict(it), "info": info, "source": source, "nfo": core.rel(path, video.parent) if path else None,
            "edits": nfo.load_edits(tmdb_id) if tmdb_id else {}, "editable": bool(tmdb_id),
            "files": movie_files(it, folders, root), "images": images, "artwork": art,
            "folder": core.rel(video.parent, root), "own": own}


def image(item_id: str, kind: str):
    """A local artwork file of this movie (only ever one of its own image files)."""
    if kind not in artwork.KINDS:
        raise UserError("Unknown kind of artwork.")
    with core.lock:
        it, items, root = find(item_id)
    p = image_file(it, library.by_folder(items, root), root, kind)
    if not p:
        raise UserError("This movie has no such artwork.")
    return p, IMAGE_TYPES.get(p.suffix.lower(), "application/octet-stream")


def art_options(item_id: str, kind: str) -> list:
    if kind not in artwork.KINDS:
        raise UserError("Unknown kind of artwork.")
    with core.lock:
        it, _, _ = find(item_id)
    tmdb_id = confirmed_id(it)
    if not tmdb_id:
        raise UserError("Match this movie on TMDB (and confirm the match) first.")
    return artwork.options(tmdb_id, kind)


def set_art(item_id: str, kind: str, url: str) -> dict:
    """Use the chosen image; the one it replaces goes to the Recycle Bin."""
    if kind not in artwork.KINDS:
        raise UserError("Unknown kind of artwork.")
    with core.lock:
        it, items, root = find(item_id)
        folders = library.by_folder(items, root)
        artwork.save(Path(root, it["path"]), library.own_folder(it, folders, root), kind, url, replace=True)
    return detail(item_id)


def clean_edits(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k not in nfo.EDITABLE:
            continue
        if k == "genres":
            items = v if isinstance(v, list) else str(v or "").split(",")
            out[k] = [g.strip() for g in items if g.strip()]
        elif k == "year":
            v = str(v or "").strip()
            if v and not (v.isdigit() and 1880 <= int(v) <= 2100):
                raise UserError("The year should be a four-digit year, like 1995.")
            out[k] = int(v) if v else ""
        else:
            out[k] = " ".join(str(v or "").split()) if k != "plot" else str(v or "").strip()
    if "title" in out and not out["title"]:
        raise UserError("The title can't be empty.")
    return out


def edit(item_id: str, fields: dict, reset: bool = False) -> dict:
    """Save your edits: remembered by TMDB id, written into the movie's .nfo files right away, and
    marked so Jellyfin won't overwrite them. 'reset' goes back to TMDB's data."""
    with core.lock:
        it, items, root = find(item_id)
        tmdb_id = confirmed_id(it)
        if not tmdb_id:
            raise UserError("Match this movie on TMDB (and confirm the match) before editing it.")
        all_edits = core.load_json(core.EDITS, {})
        if reset:
            all_edits.pop(str(tmdb_id), None)
        else:
            all_edits[str(tmdb_id)] = {**all_edits.get(str(tmdb_id), {}), **clean_edits(fields)}
        core.save_json(core.EDITS, all_edits)
        edits = all_edits.get(str(tmdb_id), {})
        folders = library.by_folder(items, root)
        group = [x for x in items if confirmed_id(x) == tmdb_id]

        fresh = None
        for x in group:
            video = Path(root, x["path"])
            path = nfo.nfo_path(video, library.own_folder(x, folders, root)) or video.with_suffix(".nfo")
            existing = nfo.parse_xml(path.read_bytes()) if path.exists() else None
            if existing is not None and not reset:
                nfo.apply_edits(existing, edits)  # change just what you edited; keep everything else
                path.write_bytes(nfo.to_bytes(existing))
            else:  # no .nfo yet, or going back to TMDB's data: refresh from TMDB, keeping unmanaged parts
                if fresh is None:
                    d = tmdbapi.tmdb(f"/movie/{tmdb_id}", append_to_response="credits,release_dates,external_ids,videos")
                    fresh = nfo.build_nfo(d)
                nfo.write_nfo(path, fresh, tmdb_id, overwrite=True)
                if reset:
                    root_el = nfo.parse_xml(path.read_bytes())
                    for el in root_el.findall("lockdata"):
                        root_el.remove(el)
                    path.write_bytes(nfo.to_bytes(root_el))

        # The Jellyfin name (used by the renamer) follows an edited title or year
        matches = core.load_json(core.MATCHES, {})
        for x in group:
            m = matches.get(x["id"])
            if m:
                m["name"] = core.jellyfin_name(edits.get("title") or m["title"], edits.get("year") or m["year"])
        core.save_json(core.MATCHES, matches)
    return detail(item_id)


def open_folder(item_id: str) -> None:
    with core.lock:
        it, _, root = find(item_id)
    folder = Path(root, it["path"]).parent
    if os.name != "nt":
        raise UserError("Opening folders only works on Windows.")
    os.startfile(str(folder))  # opens Explorer on this computer
