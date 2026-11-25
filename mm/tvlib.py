"""The TV library: finding episodes, reading their names, grouping them into shows, and what each show has.

Layout (Jellyfin, and what tinyMediaManager writes):  Show (Year)\\Season 1\\Show - S01E02 - Title.mkv
Season 0 lives in "Specials". Show artwork sits in the show folder; season posters beside it (season01-poster.jpg).
"""
import os
import re
from pathlib import Path

from guessit import guessit

from . import core, library
from .core import key_of, norm_title

TV_VERSION = 4  # bump when name reading changes, so saved scans are re-read
SXXEYY = re.compile(r"(?<![a-z0-9])S(\d{1,3})[ ._]?E(\d{1,4})((?:[ ._-]*(?:S\1[ ._]?)?E\d{1,4})*)", re.I)
# Release information that can follow an episode code ('Loki.S01E01.1080p.DSNP.WEB-DL.x265'): not a title
RELEASE_WORDS = re.compile(r"(?i)\b(?:\d{3,4}p|4k|uhd|hdr\d*|dv|sdr|web(?:[ .-]?(?:dl|rip))?|webrip|bluray|blu[ .-]?ray|brrip|bdrip|"
                           r"hdtv|dvdrip|dvd|remux|proper|repack|internal|x26[45]|h[ .]?26[45]|hevc|avc|xvid|divx|10bit|8bit|"
                           r"aac\d?(?:[ .]\d)?|ac3|eac3|ddp?\d?(?:[ .]\d)?|dts(?:[ .-]?hd)?|truehd|atmos|flac|mp3|"
                           r"dsnp|amzn|hmax|atvp|pcok|pmtp|eng|ita)\b")  # (not words that also appear in titles)
SEASON_DIR = re.compile(r"^(?:season|series|staffel|saison|temporada|s)[ ._-]*(\d{1,3})$", re.I)
SPECIALS = {"specials", "special", "season 0", "season 00"}
SKIP_DIRS = library.SKIP_DIRS | {"extrathumbs", "extrafanart", "theme-music", "backdrops"}
SIDE_EXTS = core.SUB_EXTS | {".nfo", ".jpg", ".jpeg", ".png"}


def season_folder(season: int) -> str:
    """'Season 1' (or your pattern); season 0 is always 'Specials', which Jellyfin expects."""
    return "Specials" if season == 0 else core.render(core.pattern("tv_season_folder"), {"season": season})


def episode_code(season: int, episodes: list) -> str:
    """S01E02, or S07E14-E15 for a file holding two episodes (the form Jellyfin reads as a range)."""
    code = f"S{season:02d}E{episodes[0]:02d}"
    return code + (f"-E{episodes[-1]:02d}" if len(episodes) > 1 else "")


def folder_season(path: Path, show_dir) -> int | None:
    """The season a file's folder says it belongs to ('Season 2', 'Specials')."""
    p = path.parent
    while show_dir is not None and p != show_dir and p != p.parent:
        name = p.name.strip()
        if name.lower() in SPECIALS:
            return 0
        m = SEASON_DIR.match(name)
        if m:
            return int(m.group(1))
        p = p.parent
    return None


def parse(path: Path, root: Path) -> dict:
    rel_parts = path.relative_to(root).parts
    show_dir = root / rel_parts[0] if len(rel_parts) > 1 else None
    stem = path.stem
    season = episodes = None
    m = SXXEYY.search(stem)
    if m:  # an explicit S01E02 beats everything else in the name
        season = int(m.group(1))
        episodes = [int(m.group(2))] + [int(x) for x in re.findall(r"E(\d{1,4})", m.group(3) or "", re.I)]
    guess = guessit(path.name, {"type": "episode"})
    if episodes is None:
        e = guess.get("episode")
        episodes = [int(x) for x in (e if isinstance(e, list) else [e])] if e is not None else None
        s = guess.get("season")
        season = s if isinstance(s, int) else None
        if season == 0 and not re.search(r"(?i)(?<![a-z0-9])(s0+(?![0-9])|0x\d|season[ ._-]*0+\b|specials?\b)", stem):
            season = None  # 'Show - 001' is absolute episode 1, not season 0 (the reader takes the leading 0 as a season)
    from_folder = folder_season(path, show_dir)
    if season is None:
        season = from_folder
    guessed = bool(episodes) and season is None
    if guessed:
        season = 1  # absolute numbering without seasons: assume season 1 (shown as a guess)
    series = str(core.first(guess.get("title")) or "")
    if show_dir is not None:
        series = show_title_year(show_dir.name)[0] or series
    episodes = sorted(set(episodes)) if episodes else None
    return {
        "kind": "episode" if episodes else "extra",
        "season": season if episodes else None,
        "episodes": episodes,
        "season_guessed": guessed,
        "series": series,
        # after an explicit S01E02, the episode title is simply what follows it ('Show - S01E02 - Title')
        "ep_title": ((clean_title(stem[m.end():]) if m else "") or str(guess.get("episode_title") or "")) if episodes else "",
        "resolution": str(core.first(guess.get("screen_size")) or ""),
        "codec": str(core.first(guess.get("video_codec")) or ""),
        "source": str(core.first(guess.get("source")) or ""),
        "show_dir": rel_parts[0] if show_dir is not None else None,
    }


def clean_title(text: str) -> str:
    """The episode title part of a file name, without release information."""
    text = re.sub(r"[._]", " ", text)
    text = RELEASE_WORDS.sub(" ", text)
    text = re.sub(r"\[[^\]]*\]|-[A-Za-z0-9]+$", " ", text)  # [tags] and a trailing -GROUP
    text = re.sub(r"\s+", " ", text).strip(" -")
    return text if re.search(r"[A-Za-z]{2}", text) else ""


def show_title_year(folder_name: str):
    m = re.match(r"^(.*?)[ ._]*\((\d{4})\)\s*$", folder_name)
    if m:
        return m.group(1).strip(), int(m.group(2))
    g = guessit(folder_name, {"type": "episode"})
    year = g.get("year")
    return str(core.first(g.get("title")) or folder_name).strip(), year if isinstance(year, int) else None


def show_key(item: dict, root: Path) -> str:
    """Shows are identified by their folder (or, for loose files, by the series name)."""
    if item.get("show_dir"):
        return key_of(root / item["show_dir"])
    return "loose:" + norm_title(item.get("series") or "")


# ---------------------------------------------------------------- scanning

def scan(root: Path, job: dict = None, label: str = "Reading episode names") -> list:
    videos = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not library.skip_dir(d) and d.lower() not in SKIP_DIRS]
        for name in files:
            path = Path(dirpath) / name
            if path.suffix.lower() in core.VIDEO_EXTS and not library.SKIP_FILE.search(path.stem):
                videos.append(path)
    if job:
        core.job_step(job, label, len(videos))
    cache = core.load_scan("tv", root)
    known = ({it["id"]: it for it in cache.get("items", [])}
             if cache.get("root") == key_of(root) and cache.get("v") == TV_VERSION else {})
    items = []
    for path in videos:
        try:
            st = path.stat()
        except OSError:
            continue
        finally:
            if job:
                core.tick(job)
        old = known.get(key_of(path))
        if old and old.get("size") == st.st_size and old.get("mtime") == int(st.st_mtime):
            item = {k: v for k, v in old.items() if k not in ("match", "files", "named", "key")}
        else:
            item = parse(path, root)
        item.update(size=st.st_size, mtime=int(st.st_mtime), path=str(path.relative_to(root)), id=key_of(path))
        items.append(item)
    try:
        core.save_scan("tv", root, {"root": key_of(root), "folder": str(root), "items": items, "v": TV_VERSION})
    except OSError:
        pass
    return [dict(it) for it in items]


def with_matches(items: list, root: Path) -> list:
    matches = core.load_json(core.TV_MATCHES, {})
    for it in items:
        it["key"] = show_key(it, root)
        it["match"] = matches.get(it["key"])
    return items


def rescan(job: dict = None, label: str = "Reading episode names") -> list:
    root = core.tv_state["root"]
    if root is None:
        return []
    core.tv_state["items"] = scan(root, job, label)
    return with_matches(core.tv_state["items"], root)


# ---------------------------------------------------------------- shows and what they have

def group(items: list) -> dict:
    shows = {}
    for it in items:
        shows.setdefault(it["key"], []).append(it)
    return shows


def confirmed(match) -> bool:
    return bool(match) and match.get("status") in core.MATCHED


def show_name_parts(match: dict, edits: dict = None):
    """The show's folder name ('Title (Year)', or your pattern) and the title used in episode file names."""
    edits = edits or {}
    title = edits.get("title") or match["title"]
    year = edits.get("year") or match.get("year")
    from . import tvmeta
    tvdb = ((tvmeta.cached(match["tmdb_id"]) or {}).get("show") or {}).get("tvdb_id")
    values = {"title": title, "year": year, "tmdb": match["tmdb_id"], "tvdb": tvdb}
    return core.render(core.pattern("tv_show_folder"), values), core.safe_name(title)


def episode_values(title: str, season: int, episodes: list, episode_title: str = "", resolution: str = "") -> dict:
    return {"show": title, "code": episode_code(season, episodes), "season": season, "episode": episodes[0],
            "episode_title": episode_title, "resolution": resolution}


def target_show_dir(root: Path, match: dict) -> Path:
    from . import nfo
    edits = nfo.load_edits(f"tv:{match['tmdb_id']}")
    return root / show_name_parts(match, edits)[0]


def same_name(a: str, b: str) -> bool:
    """Names that differ only in punctuation ('Sean Combs The Reckoning' vs 'Sean Combs - The Reckoning') count as the same."""
    return a == b or norm_title(a) == norm_title(b)


def starts_right(stem: str, title: str, season: int, episodes: list, resolution: str = "") -> bool:
    """Does the file name follow the episode pattern up to the episode title ('Show - S01E02 ...')? Whatever the
    episode title says, and ignoring punctuation-only differences."""
    pat = core.pattern("tv_episode_file")
    head = pat.split("{episode_title}")[0]
    want = core.render(head, episode_values(title, season, episodes, "", resolution)) if head.strip() else ""
    code = episode_code(season, episodes)
    m = SXXEYY.search(stem)
    if not m or not stem[m.start():].upper().startswith(code.upper()):
        return False
    if "{episode_title}" not in pat:
        return same_name(stem, core.render(pat, episode_values(title, season, episodes, "", resolution)))
    a, b = norm_title(stem), norm_title(want)
    return a == b or a.startswith(b + " ")


IGNORED_WORDS = {"part", "pt", "chapter"}


def titles_agree(a: str, b: str) -> bool:
    """Do two episode titles name the same episode? Word-based, so 'The Cadillac (Part 1)' agrees with
    'The Cadillac (1)', and release junk ('Ouroboros DSNP WEB') doesn't get in the way."""
    na, nb = norm_title(a), norm_title(b)
    ca, cb = na.replace(" ", ""), nb.replace(" ", "")  # 'P.R. Pickles' = 'PR Pickles', 'Strong Box' = 'Strongbox'
    if ca and cb and (ca == cb or (min(len(ca), len(cb)) >= 4 and (ca in cb or cb in ca))):
        return True
    wa = set(na.split()) - IGNORED_WORDS
    wb = set(nb.split()) - IGNORED_WORDS
    if not wa or not wb:
        return True
    return wa <= wb or wb <= wa or len(wa & wb) / len(wa | wb) >= 0.5


def title_agrees(it: dict, data) -> bool:
    """Does the episode title in the file name agree with TMDB's title for that number? When it doesn't, the
    file may be an extra numbered as an episode, or numbered differently (TVDB vs TMDB), so it's left alone.
    A two-episode file's title ('A - B') agrees when either half matches either episode."""
    if not it.get("ep_title") or not data:
        return True
    from . import tvmeta
    import re as _re
    mine = [t for t in _re.split(r" - | & | / ", it["ep_title"]) if t.strip()] or [it["ep_title"]]
    theirs = [(tvmeta.episode(data, it["season"], n) or {}).get("name", "") for n in it["episodes"]]
    return any(titles_agree(a, b) for a in mine for b in theirs) or any(titles_agree(it["ep_title"], b) for b in theirs)


def episode_named(it: dict, root: Path) -> bool:
    """Named for Jellyfin: 'Show (Year)/Season N/Show - S0NE0M...', whatever the episode title says."""
    m = it.get("match")
    if it["kind"] != "episode" or not confirmed(m):
        return False
    from . import nfo
    folder_name, title = show_name_parts(m, nfo.load_edits(f"tv:{m['tmdb_id']}"))
    path = Path(root, it["path"])
    return (path.parent.name == season_folder(it["season"]) and path.parent.parent.parent == root
            and same_name(path.parent.parent.name, folder_name)
            and starts_right(path.stem, title, it["season"], it["episodes"], it.get("resolution", "")))


def sidecars(video: Path) -> list:
    """Subtitles, .nfo and '-thumb.jpg' named after the episode file, and Jellyfin's '.trickplay' folder."""
    out = []
    try:
        for f in video.parent.iterdir():
            if f.is_dir() and f.name.lower() == f"{video.stem}.trickplay".lower():
                out.append(f)  # Jellyfin's seek-bar previews: they move with the episode, or Jellyfin rebuilds them
                continue
            if f.is_file() and f != video and f.name.startswith(video.stem) and f.name[len(video.stem)] in ".-" and (
                    f.suffix.lower() in SIDE_EXTS
                    or (f.suffix.lower() in core.VIDEO_EXTS and f.name[len(video.stem):].lower().startswith("-trailer"))):
                out.append(f)
    except OSError:
        pass
    return out


def annotate(items: list, root: Path) -> list:
    listings = {}
    for it in items:
        video = Path(root, it["path"])
        names = listings.get(key_of(video.parent))
        if names is None:
            try:
                names = {e.name.lower() for e in os.scandir(video.parent)}
            except OSError:
                names = set()
            listings[key_of(video.parent)] = names
        stem = video.stem.lower()
        it["files"] = {"nfo": f"{stem}.nfo" in names, "thumb": f"{stem}-thumb.jpg" in names,
                       "subs": library.sub_languages({n: False for n in names}, stem)}
        it["named"] = episode_named(it, root)
        it["mismatch"] = False
        if it["kind"] == "episode" and confirmed(it.get("match")):
            from . import tvmeta
            eps = it["episodes"]
            # A file holding episodes that aren't next to each other (S07E01 + S07E12) can't be written as
            # Jellyfin's range (S07E01-E12 would claim 1 to 12), so it's left as it is, like a title mismatch.
            gap = len(eps) > 1 and eps != list(range(eps[0], eps[-1] + 1))
            data = tvmeta.cached_for(it["match"])
            from . import tvsort  # S01E19 when TMDB's season 1 has 18 episodes: not an episode TMDB knows
            it["mismatch"] = gap or not title_agrees(it, data) or tvsort.not_on_list(data, it["season"], eps)
    return items


def summaries(items: list, root: Path) -> list:
    """One row per show for the library table."""
    from . import artwork, tvmeta
    out = []
    for key, eps in group(items).items():
        m = eps[0].get("match")
        show_dir = root / eps[0]["show_dir"] if eps[0].get("show_dir") else None
        episodes = [e for e in eps if e["kind"] == "episode"]
        have = {(e["season"], n) for e in episodes for n in e["episodes"]}
        names = {n.lower() for n in os.listdir(show_dir)} if show_dir and show_dir.is_dir() else set()
        title, year = show_title_year(show_dir.name) if show_dir else (eps[0]["series"], None)
        data = tvmeta.cached_for(m) if confirmed(m) else None
        missing = tvmeta.missing_episodes(data, have) if data else None
        subs = {}
        for e in episodes:
            for lang in e["files"]["subs"]:
                subs[lang] = subs.get(lang, 0) + 1
        out.append({
            "key": key, "id": key, "folder": eps[0]["show_dir"] or "(loose files)", "title": title, "year": year,
            "match": m, "episodes": len(have), "files": len(episodes), "extras": len(eps) - len(episodes),
            "seasons": sorted({e["season"] for e in episodes}), "size": sum(e["size"] for e in eps),
            "missing": len(missing) if missing is not None else None,
            "named": bool(episodes) and all(e["named"] or e["mismatch"] for e in episodes),
            "to_rename": sum(1 for e in episodes if not e["named"] and not e["mismatch"]),
            "mismatch": sum(1 for e in episodes if e["mismatch"]),
            "has": {"nfo": "tvshow.nfo" in names, "ep_nfo": sum(e["files"]["nfo"] for e in episodes),
                    "art": any(n in names for n in artwork.KINDS["poster"][3]),
                    "trailer": "trailers" in names or any(n.endswith(tuple(f"-trailer{x}" for x in core.VIDEO_EXTS)) for n in names),
                    "thumbs": sum(e["files"]["thumb"] for e in episodes), "subs": subs},
            "season_guessed": any(e.get("season_guessed") for e in episodes),
        })
    return out


def current() -> dict:
    root = core.tv_state["root"]
    if root is None:
        return {"shows": [], "items": []}
    items = annotate(with_matches(core.tv_state["items"], root), root)
    return {"shows": summaries(items, root)}


# ---------------------------------------------------------------- deleting

def delete_targets(key: str, what: str, season=None, item_id: str = None):
    """What a delete would remove: one episode file (with its subtitles, .nfo and thumbnail), a season folder,
    or a whole show folder."""
    root = core.require_tv_scan()
    items = with_matches(core.tv_state["items"], root)
    eps = [i for i in items if i["key"] == key]
    if not eps:
        raise core.UserError("That show is no longer in the list. Scan again.")
    show_dir = root / eps[0]["show_dir"] if eps[0].get("show_dir") else None
    if what == "episode":
        it = next((i for i in eps if i["id"] == item_id), None)
        if not it:
            raise core.UserError("That episode is no longer in the list. Scan again.")
        video = Path(root, it["path"])
        return [video] + sidecars(video), [it["id"]]
    if what == "season":
        in_season = [i for i in eps if i["kind"] == "episode" and i["season"] == season]
        folders = {key_of(Path(root, i["path"]).parent) for i in in_season}
        folder = Path(root, in_season[0]["path"]).parent if in_season else None
        others = [i for i in items if folder and key_of(Path(root, i["path"]).parent) == key_of(folder) and i["season"] != season]
        if not in_season or len(folders) > 1 or folder == show_dir or others:
            # episodes aren't neatly in one season folder: delete them file by file
            targets = []
            for i in in_season:
                v = Path(root, i["path"])
                targets += [v] + sidecars(v)
            return targets, [i["id"] for i in in_season]
        return [folder], [i["id"] for i in in_season]
    if what == "show":
        if not show_dir:
            raise core.UserError("These files aren't in a show folder; delete them one by one.")
        return [show_dir], [i["id"] for i in eps]
    raise core.UserError("Choose what to delete.")
