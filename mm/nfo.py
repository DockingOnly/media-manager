"""Jellyfin/Kodi .nfo files, plus poster and fanart images."""
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

from . import core, library, tmdbapi, trailers
from .tmdbapi import image_url, year_of


def sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text not in (None, ""):
        el.text = str(text)
    return el


def build_nfo(d: dict) -> bytes:
    movie = ET.Element("movie")
    sub(movie, "title", d.get("title"))
    sub(movie, "originaltitle", d.get("original_title"))
    sub(movie, "plot", d.get("overview"))
    sub(movie, "outline", d.get("overview"))
    sub(movie, "tagline", d.get("tagline"))
    sub(movie, "runtime", d.get("runtime") or None)
    sub(movie, "year", year_of(d.get("release_date")))
    sub(movie, "premiered", d.get("release_date"))
    if d.get("vote_average"):
        sub(movie, "rating", round(d["vote_average"], 1))
    cert = next((r.get("certification") for c in d.get("release_dates", {}).get("results", [])
                 if c.get("iso_3166_1") == "US" for r in c.get("release_dates", []) if r.get("certification")), None)
    sub(movie, "mpaa", cert)
    imdb = d.get("imdb_id") or d.get("external_ids", {}).get("imdb_id")
    sub(movie, "uniqueid", d["id"], type="tmdb", default="true")
    if imdb:
        sub(movie, "uniqueid", imdb, type="imdb")
        sub(movie, "imdbid", imdb)
    sub(movie, "tmdbid", d["id"])
    keys = trailers.youtube_trailers(d.get("videos", {}).get("results", []))
    if keys:  # lets Jellyfin play the trailer from YouTube when there is no local file
        sub(movie, "trailer", f"plugin://plugin.video.youtube/?action=play_video&videoid={keys[0]}")
    if d.get("belongs_to_collection"):
        coll = sub(movie, "set")
        sub(coll, "name", d["belongs_to_collection"].get("name"))
    for g in d.get("genres", []):
        sub(movie, "genre", g.get("name"))
    for c in d.get("production_countries", []):
        sub(movie, "country", c.get("name"))
    for s in d.get("production_companies", [])[:5]:
        sub(movie, "studio", s.get("name"))
    crew = d.get("credits", {}).get("crew", [])
    for name in dict.fromkeys(c["name"] for c in crew if c.get("job") == "Director"):
        sub(movie, "director", name)
    for name in dict.fromkeys(c["name"] for c in crew if c.get("department") == "Writing"):
        sub(movie, "credits", name)
    for i, a in enumerate(d.get("credits", {}).get("cast", [])[:20]):
        actor = sub(movie, "actor")
        sub(actor, "name", a.get("name"))
        sub(actor, "role", a.get("character"))
        sub(actor, "order", i)
        sub(actor, "type", "Actor")
        sub(actor, "thumb", image_url(a.get("profile_path"), "w300"))
    ET.indent(movie, "  ")
    return b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + ET.tostring(movie, encoding="utf-8")


def download(url: str, dst: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


# ---------------------------------------------------------------- reading, editing and merging .nfo files

XML_HEAD = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
EDITABLE = ("title", "originaltitle", "sorttitle", "year", "tagline", "plot", "genres", "set", "mpaa")
# Elements this app writes. When an existing .nfo is updated, only these are replaced; everything else
# (watched status, play count, tags, ratings, file details from tinyMediaManager...) is kept.
MANAGED = {"title", "originaltitle", "plot", "outline", "tagline", "runtime", "year", "premiered", "rating", "mpaa",
           "uniqueid", "imdbid", "tmdbid", "trailer", "set", "genre", "country", "studio", "director", "credits", "actor"}


def nfo_path(video: Path, own: bool = False):
    """The .nfo Jellyfin reads for a movie: '<file name>.nfo', or 'movie.nfo' in the movie's own folder."""
    for p in [video.with_suffix(".nfo")] + ([video.parent / "movie.nfo"] if own else []):
        if p.is_file():
            return p
    return None


def parse_xml(data: bytes, tag: str = "movie"):
    """Parse an .nfo, keeping comments (tinyMediaManager notes when and how it wrote the file)."""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    try:
        parser.feed(data)
        root = parser.close()
    except ET.ParseError:
        return None
    return root if root.tag == tag else None


def parse_all(data: bytes, tag: str = "episodedetails") -> list:
    """An episode .nfo can hold several entries (one per episode in a two-episode file)."""
    import re
    body = re.sub(rb"^\s*<\?xml[^>]*\?>", b"", data)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    try:
        parser.feed(b"<root>" + body + b"</root>")
        wrapper = parser.close()
    except ET.ParseError:
        return []
    return [el for el in wrapper if el.tag == tag]


def to_bytes_all(roots: list) -> bytes:
    for r in roots:
        ET.indent(r, "  ")
    return XML_HEAD + b"\n".join(ET.tostring(r, encoding="utf-8") for r in roots) + b"\n"


def to_bytes(root) -> bytes:
    ET.indent(root, "  ")
    return XML_HEAD + ET.tostring(root, encoding="utf-8")


def text(root, tag):
    el = root.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


def read_nfo_root(root) -> dict:
    """The parts of an .nfo the app shows, from either this app's or tinyMediaManager's format."""
    ids = {u.get("type", "").lower(): (u.text or "").strip() for u in root.findall("uniqueid")}
    tmdb_id = ids.get("tmdb") or text(root, "tmdbid")
    imdb_id = ids.get("imdb") or text(root, "imdbid") or (text(root, "id") if text(root, "id").startswith("tt") else "")
    ratings = {}
    for r in root.findall("ratings/rating"):
        v = r.findtext("value")
        if v:
            ratings[r.get("name", "rating")] = v
    if not ratings and text(root, "rating"):
        ratings["tmdb"] = text(root, "rating")
    year = text(root, "year") or text(root, "premiered")[:4]
    return {
        "title": text(root, "title"), "originaltitle": text(root, "originaltitle"), "sorttitle": text(root, "sorttitle"),
        "year": int(year) if year.isdigit() else None, "tagline": text(root, "tagline"), "plot": text(root, "plot"),
        "runtime": text(root, "runtime"), "mpaa": text(root, "mpaa") or text(root, "certification"),
        "premiered": text(root, "premiered"), "genres": [g.text.strip() for g in root.findall("genre") if g.text],
        "set": (root.findtext("set/name") or root.findtext("set") or "").strip(),
        "directors": [d.text.strip() for d in root.findall("director") if d.text],
        "studios": [s.text.strip() for s in root.findall("studio") if s.text][:3],
        "cast": [{"name": a.findtext("name") or "", "role": a.findtext("role") or "", "thumb": a.findtext("thumb") or ""}
                 for a in root.findall("actor")[:12]],
        "ratings": ratings, "tmdb_id": int(tmdb_id) if str(tmdb_id).isdigit() else None, "imdb_id": imdb_id,
        "watched": text(root, "watched").lower() == "true", "locked": text(root, "lockdata").lower() == "true",
        "by_tmm": any("tinyMediaManager" in (n.text or "") for n in root.iter() if n.tag is ET.Comment),
    }


def read_nfo(path: Path):
    try:
        root = parse_xml(path.read_bytes())
    except OSError:
        return None
    return read_nfo_root(root) if root is not None else None


def set_text(root, tag: str, value) -> None:
    el = root.find(tag)
    if value in (None, ""):
        if el is not None and tag not in ("title",):
            root.remove(el)
        return
    if el is None:
        el = ET.SubElement(root, tag)
    el.text = str(value)


def apply_edits(root, edits: dict) -> None:
    """Write your edits into an .nfo and tell Jellyfin not to overwrite them (lockdata)."""
    if not edits:
        return
    for field in ("title", "originaltitle", "sorttitle", "year", "tagline"):
        if field in edits:
            set_text(root, field, edits[field])
    if "plot" in edits:
        set_text(root, "plot", edits["plot"])
        if root.find("outline") is not None:
            set_text(root, "outline", edits["plot"])
    if "mpaa" in edits:
        set_text(root, "mpaa", edits["mpaa"])
        if root.find("certification") is not None:
            set_text(root, "certification", edits["mpaa"])
    if "genres" in edits:
        old = root.findall("genre")
        at = list(root).index(old[0]) if old else len(root)
        for g in old:
            root.remove(g)
        for i, name in enumerate(edits["genres"]):
            el = ET.Element("genre")
            el.text = name
            root.insert(at + i, el)
    if "set" in edits:
        for s in root.findall("set"):
            root.remove(s)
        if edits["set"]:
            s = ET.SubElement(root, "set")
            ET.SubElement(s, "name").text = edits["set"]
    set_text(root, "lockdata", "true")


def merge(existing, fresh) -> None:
    """Update an existing .nfo with fresh TMDB data, keeping everything this app doesn't manage."""
    for el in [e for e in list(existing) if e.tag in MANAGED]:
        existing.remove(el)
    for el in list(fresh):
        existing.append(el)


def load_edits(tmdb_id) -> dict:
    return core.load_json(core.EDITS, {}).get(str(tmdb_id), {})


def write_nfo(path: Path, fresh_xml, tmdb_id, overwrite: bool) -> bool:
    """Create or update one .nfo. Returns False when it exists and overwrite is off."""
    edits = load_edits(tmdb_id)
    existing = parse_xml(path.read_bytes()) if path.exists() else None
    if existing is not None and not overwrite:
        return False
    if existing is not None:
        merge(existing, parse_xml(fresh_xml))
        root = existing
    else:
        root = parse_xml(fresh_xml)
    apply_edits(root, edits)
    path.write_bytes(to_bytes(root))
    return True


def write_movie_files(tmdb_id, group: list, folders: dict, root: Path, overwrite: bool, artwork: bool) -> dict:
    result = {"nfo": 0, "images": 0, "skipped": 0}
    d = tmdbapi.tmdb(f"/movie/{tmdb_id}", append_to_response="credits,release_dates,external_ids,videos")
    xml = build_nfo(d)
    for it in group:
        if write_nfo(Path(root, it["path"]).with_suffix(".nfo"), xml, tmdb_id, overwrite):
            result["nfo"] += 1
        else:
            result["skipped"] += 1
    if not artwork:
        return result
    from . import artwork  # (artwork uses tmdbapi, like this module)
    video = Path(root, group[0]["path"])
    own = library.own_folder(group[0], folders, root)
    for kind in ("poster", "fanart"):
        if artwork.existing(video, own, kind) and not overwrite:
            result["skipped"] += 1
            continue
        opts = artwork.options(tmdb_id, kind)
        if opts:
            artwork.save(video, own, kind, opts[0]["url"], replace=overwrite)
            result["images"] += 1
    return result


def write_all(job: dict, root: Path, overwrite: bool, artwork: bool) -> dict:
    with core.lock:  # pick up renames made outside the app first, like the other tasks
        items = library.rescan(job, "Checking for changes")
    groups = {}
    for it in items:
        if it["match"] and it["match"]["status"] in core.MATCHED:
            groups.setdefault(it["match"]["tmdb_id"], []).append(it)
    if not groups:
        raise core.UserError("Match some movies on TMDB first. Only confirmed matches get .nfo files.")
    folders = library.by_folder(items, root)
    core.job_step(job, "Writing .nfo files" + (" and artwork" if artwork else ""), len(groups))
    totals = {"nfo": 0, "images": 0, "skipped": 0}
    failed = []

    def one(entry):
        tmdb_id, group = entry
        if job["cancel"]:
            return
        try:
            r = write_movie_files(tmdb_id, group, folders, root, overwrite, artwork)
            with core.job_lock:
                for k in totals:
                    totals[k] += r[k]
        except (core.TmdbError, OSError, urllib.error.URLError) as err:
            failed.append({"name": group[0]["match"]["name"], "error": str(err)})
        finally:
            core.tick(job)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(one, groups.items()))
    with core.lock:
        items = [dict(i) for i in library.rescan()]  # refresh the .nfo/artwork checkmarks
    return {"movies": len(groups), "failed": failed, "cancelled": job["cancel"], "items": items, **totals}
