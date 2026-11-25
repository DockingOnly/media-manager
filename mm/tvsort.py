"""Files in a show folder that aren't episodes on TMDB's list: DVD extras, web shorts, specials.

Two ways to make them show up properly in Jellyfin, offered as part of the TV rename (with preview and undo):
- a file whose title matches one of TMDB's specials becomes that special: Specials/Show - S00E04 - Title.mp4
- anything else moves into one of Jellyfin's extras folders inside the show folder ('deleted scenes', 'shorts',
  'trailers', ...), named by its own title. Jellyfin lists those under Extras on the show's page.
"""
import re

from . import tvmeta
from .core import norm_title

# Jellyfin's extras folder names, and words in a title that say which one fits
CATEGORIES = [
    (re.compile(r"(?i)\bdeleted[ ._-]*scenes?\b"), "deleted scenes"),
    (re.compile(r"(?i)\bbehind[ ._-]*the[ ._-]*scenes\b|\bmaking[ ._-]*of\b|\bbts\b"), "behind the scenes"),
    (re.compile(r"(?i)\binterviews?\b"), "interviews"),
    (re.compile(r"(?i)\bfeaturettes?\b"), "featurettes"),
    (re.compile(r"(?i)\b(?:promos?|trailers?|teasers?|tv[ ._-]*spots?)\b"), "trailers"),
    (re.compile(r"(?i)\b(?:fake|alternate|alternative)[ ._-]*endings?\b|\bmusic[ ._-]*videos?\b|\bbloopers?\b|"
                r"\bgag[ ._-]*reels?\b|\bouttakes?\b"), "other"),
    (re.compile(r"(?i)\b(?:pilots?|shorts?|webisodes?|web[ ._-]*series)\b"), "shorts"),
]
EXTRA_FOLDERS = {f for _, f in CATEGORIES} | {"extras", "scenes", "clips"}
# A folder whose name says it holds extras ('ATHF Xtras', 'Bonus', 'Special Features')
EXTRAS_HINT = re.compile(r"(?i)extras?\b|xtras?\b|\bbonus\b|special[ ._-]*features|featurettes")
DISC_CODE = re.compile(r"(?i)\b(?:S\d{1,2}M\d{1,2}(?:X\d{1,3})?|S\d{1,2}X\d{1,3}|X\d{1,3}|M\d{1,2}X\d{1,3})\b")
SXXEYY = re.compile(r"(?i)\bS\d{1,3}[ ._-]?E\d{1,4}(?:-?E\d{1,4})*\b")


def initials(show: str) -> str:
    """'Aqua Teen Hunger Force' -> 'ATHF' (release groups often abbreviate the show like this)."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", show) if w]
    return "".join(w[0] for w in words).upper() if len(words) >= 2 else ""


def bare(text: str, show: str) -> str:
    """Without disc and episode codes, but keeping the show's name (for matching TMDB's specials)."""
    t = DISC_CODE.sub(" ", SXXEYY.sub(" ", re.sub(r"[._]+", " ", text)))
    ab = initials(show)
    if ab:
        t = re.sub(rf"\b{re.escape(ab)}\b", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -")


def extra_title(text: str, show: str) -> str:
    """An extra's own title, without the show name, disc codes ('S04M01X03') or episode codes."""
    t = re.sub(r"[._]+", " ", text)
    t = SXXEYY.sub(" ", t)
    t = DISC_CODE.sub(" ", t)
    ab = initials(show)
    if ab:
        t = re.sub(rf"\b{re.escape(ab)}\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -")
    if show and norm_title(t).startswith(norm_title(show) + " ") and len(norm_title(t)) > len(norm_title(show)) + 3:
        t = t[len(show):].strip(" -") if t.lower().startswith(show.lower()) else t
    return re.sub(r"\s+", " ", t).strip(" -")


def compact(text: str) -> str:
    return norm_title(text).replace(" ", "")


def special_for(titles, data: dict):
    """The TMDB special (season 0) one of these titles names, or None. Strict: the whole of one title must
    appear in the other, and it must be long enough not to be a coincidence."""
    mine = [c for c in (compact(t) for t in ([titles] if isinstance(titles, str) else titles)) if c]
    best = None
    for e in (data or {}).get("episodes", {}).get("0", []):
        theirs = compact(e.get("name"))
        if theirs and any(theirs == x or (len(theirs) >= 10 and theirs in x) or (len(x) >= 15 and x in theirs) for x in mine):
            if best is None or len(theirs) > len(compact(best["name"])):
                best = e
    return best


def category(title: str):
    for rx, folder in CATEGORIES:
        if rx.search(title):
            return folder
    return None


def series_prefix(title: str) -> str:
    """The first two real words: 'Carl's Pissed Giants Win' -> 'carl pissed' (for spotting a run of shorts)."""
    words = [w for w in norm_title(title).split() if len(w) > 1]
    return " ".join(words[:2]) if len(words) >= 3 else ""


def place(titles: list) -> list:
    """The extras folder and file title for each extra of one show. Titles that share their first words with two
    or more others are a series of shorts ('Carl's Pissed ...', 'Soul Quest Overdrive ...')."""
    counts = {}
    for t in titles:
        p = series_prefix(t)
        if p:
            counts[p] = counts.get(p, 0) + 1
    out = []
    for t in titles:
        folder = category(t) or ("shorts" if counts.get(series_prefix(t), 0) >= 3 else "extras")
        name = t
        if folder == "deleted scenes":  # the folder already says it
            name = re.sub(r"(?i)^deleted[ ._-]*scenes?\s*[-:]?\s*", "", t) or t
        out.append((folder, name))
    return out


def in_extras_folder(rel_parts) -> bool:
    """Is the file inside a folder that holds extras (not a season folder)?"""
    return any(EXTRAS_HINT.search(p) for p in rel_parts[1:-1])


def not_on_list(data: dict, season: int, episodes: list) -> bool:
    """TMDB lists this season but not these episode numbers (S01E19 when season 1 has 18)."""
    eps = (data or {}).get("episodes", {})
    return str(season) in eps and not all(tvmeta.episode(data, season, n) for n in episodes)
