"""IMDb, Rotten Tomatoes and Metacritic ratings from OMDb (free key from omdbapi.com, 1,000 lookups a day).

Written into .nfo files as a <ratings> block (IMDb as the default rating Jellyfin shows) plus <criticrating>
(the Rotten Tomatoes score, which Jellyfin shows as the critic rating).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree as ET

from . import core
from .core import UserError

OMDB = "https://www.omdbapi.com/"
REFRESH_AFTER = 7 * 24 * 3600
RATINGS_CACHE = "ratings_cache.json"


def cache_path():
    return core.MATCHES.with_name(RATINGS_CACHE)  # beside the other data files


def omdb_key() -> str:
    return (core.load_config().get("omdb_key") or "").strip()


def request(params: dict, key: str = None) -> dict:
    key = key or omdb_key()
    url = OMDB + "?" + urllib.parse.urlencode({**params, "apikey": key})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": core.USER_AGENT}), timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as err:
        if err.code == 401:
            raise UserError("OMDb rejected the API key. Check it in Settings (new keys must be activated from OMDb's email).") from None
        raise UserError(f"OMDb returned error {err.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UserError("Couldn't reach OMDb. Check your internet connection.") from None
    if data.get("Response") == "False" and "limit" in (data.get("Error") or "").lower():
        raise UserError("OMDb's daily limit is used up. It resets at midnight (US time).")
    if data.get("Response") == "False" and "key" in (data.get("Error") or "").lower():
        raise UserError("OMDb rejected the API key. Check it in Settings (new keys must be activated from OMDb's email).")
    return data


def check_key(key: str) -> None:
    data = request({"i": "tt0113277"}, key)  # Heat (1995)
    if data.get("Response") != "True":
        raise UserError(data.get("Error") or "OMDb didn't accept the key.")


def for_imdb(imdb_id: str):
    """{'imdb': (rating, votes), 'rt': percent, 'metacritic': score} for an IMDb id, remembered for a week.
    None without a key or an id."""
    if not imdb_id or not omdb_key():
        return None
    cache = core.load_json(cache_path(), {})
    entry = cache.get(imdb_id)
    if entry is None or time.time() - entry.get("fetched", 0) > REFRESH_AFTER:
        data = request({"i": imdb_id})
        out = {}
        if data.get("Response") == "True":
            r = data.get("imdbRating")
            v = (data.get("imdbVotes") or "").replace(",", "")
            if r and r != "N/A":
                out["imdb"] = (float(r), int(v) if v.isdigit() else None)
            for s in data.get("Ratings") or []:
                val = s.get("Value") or ""
                if s.get("Source") == "Rotten Tomatoes" and val.endswith("%"):
                    out["rt"] = int(val[:-1])
                if s.get("Source") == "Metacritic" and "/" in val:
                    out["metacritic"] = int(val.split("/")[0])
        entry = {"fetched": time.time(), "ratings": out}
        with core.lock:
            cache = core.load_json(cache_path(), {})
            cache[imdb_id] = entry
            core.save_json(cache_path(), cache)
    return entry["ratings"] or None


def add_to(root, ratings: dict, tmdb_rating=None, tmdb_votes=None) -> None:
    """Write the <ratings> block and <criticrating>, replacing what was there. IMDb becomes the default rating."""
    if not ratings:
        return
    for old in root.findall("ratings") + root.findall("criticrating"):
        root.remove(old)
    block = ET.Element("ratings")

    def one(name, value, top, votes=None, default=False):
        el = ET.SubElement(block, "rating", {"name": name, "max": str(top), "default": "true" if default else "false"})
        ET.SubElement(el, "value").text = str(value)
        if votes:
            ET.SubElement(el, "votes").text = str(votes)

    if "imdb" in ratings:
        one("imdb", ratings["imdb"][0], 10, ratings["imdb"][1], default=True)
        rating = root.find("rating")
        if rating is None:
            rating = ET.SubElement(root, "rating")
        rating.text = str(ratings["imdb"][0])  # the simple <rating> too, for readers that only know that
    if tmdb_rating:
        one("themoviedb", round(tmdb_rating, 1), 10, tmdb_votes, default="imdb" not in ratings)
    if "rt" in ratings:
        one("tomatometerallcritics", ratings["rt"], 100)
        ET.SubElement(root, "criticrating").text = str(ratings["rt"])  # Jellyfin's critic rating
    if "metacritic" in ratings:
        one("metacritic", ratings["metacritic"], 100)
    root.append(block)
