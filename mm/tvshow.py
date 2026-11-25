"""One show: its details, seasons and episodes (including missing ones), artwork, edits, deleting, its folder."""
import datetime
import os
from pathlib import Path
from urllib.parse import quote

from . import core, movie, nfo, tvart, tvlib, tvmeta, tvnfo
from .core import UserError
from .tmdbapi import image_url


def find(key: str):
    root = core.require_tv_scan()
    items = tvlib.annotate(tvlib.with_matches(core.tv_state["items"], root), root)
    eps = [i for i in items if i["key"] == key]
    if not eps:
        raise UserError("That show is no longer in the list. Scan again.")
    show_dir = root / eps[0]["show_dir"] if eps[0].get("show_dir") else None
    return eps, root, show_dir


def img_url(key: str, kind: str, **extra) -> str:
    q = "&".join(f"{k}={quote(str(v))}" for k, v in extra.items())
    return f"/api/tv/image?key={quote(key)}&kind={kind}" + (f"&{q}" if q else "")


def detail(key: str) -> dict:
    with core.lock:
        eps, root, show_dir = find(key)
    m = eps[0]["match"]
    data = None
    if tvlib.confirmed(m):
        try:
            data = tvmeta.data_for(m)
        except (core.TmdbError, UserError):
            data = tvmeta.cached_for(m) or tvmeta.cached(m["tmdb_id"])
    nfo_info = None
    if show_dir and (show_dir / "tvshow.nfo").is_file():
        root_el = nfo.parse_xml((show_dir / "tvshow.nfo").read_bytes(), "tvshow")
        nfo_info = nfo.read_nfo_root(root_el) if root_el is not None else None
    s = (data or {}).get("show") or {}
    edits = nfo.load_edits(f"tv:{m['tmdb_id']}") if tvlib.confirmed(m) else {}
    info = {
        "title": edits.get("title") or (nfo_info or {}).get("title") or s.get("name") or eps[0]["series"],
        "year": edits.get("year") or (nfo_info or {}).get("year") or (int(s["first_air_date"][:4]) if s.get("first_air_date") else None),
        "plot": edits.get("plot") or (nfo_info or {}).get("plot") or s.get("overview", ""),
        "genres": edits.get("genres") or (nfo_info or {}).get("genres") or s.get("genres", []),
        "tags": edits.get("tags") or (nfo_info or {}).get("tags") or [],
        "mpaa": edits.get("mpaa") or (nfo_info or {}).get("mpaa") or s.get("mpaa", ""),
        "originaltitle": edits.get("originaltitle") or (nfo_info or {}).get("originaltitle") or s.get("original_name", ""),
        "sorttitle": edits.get("sorttitle") or (nfo_info or {}).get("sorttitle") or "",
        "networks": s.get("networks", []), "status": s.get("status", ""), "runtime": s.get("runtime"),
        "rating": s.get("vote_average"), "cast": s.get("cast") or (nfo_info or {}).get("cast", []),
        "tmdb_id": s.get("id") or (m or {}).get("tmdb_id"), "tvdb_id": s.get("tvdb_id"), "imdb_id": s.get("imdb_id"),
        "first_aired": s.get("first_air_date", ""),
    }
    # artwork
    art, images = [], {}
    for kind, (label, *_rest) in tvart.SHOW_KINDS.items():
        p = tvart.show_existing(show_dir, kind) if show_dir else None
        url = img_url(key, kind, v=int(p.stat().st_mtime)) if p else None
        art.append({"kind": kind, "label": label, "file": p.name if p else None, "url": url})
        if url:
            images[kind] = url
    images.setdefault("poster", image_url(s.get("poster_path"), "w500") or (m or {}).get("poster"))
    if s.get("backdrop_path"):
        images.setdefault("fanart", image_url(s["backdrop_path"], "w1280"))
    # seasons and episodes, with what's missing
    today = datetime.date.today().isoformat()
    by_ep = {}
    for it in eps:
        if it["kind"] == "episode":
            for n in it["episodes"]:
                by_ep.setdefault((it["season"], n), it)
    numbers = {int(x) for x in (data or {}).get("episodes", {})} | {it["season"] for it in eps if it["kind"] == "episode"}
    seasons = []
    for season in sorted(numbers, key=lambda n: (n == 0, n)):
        rows, seen = [], set()
        for e in (data or {}).get("episodes", {}).get(str(season), []):
            it = by_ep.get((season, e["episode"]))
            seen.add(e["episode"])
            if it and len(it["episodes"]) > 1 and it["episodes"][0] != e["episode"]:
                rows.append({"episode": e["episode"], "name": e["name"], "aired": e["air_date"], "same_file": True})
                continue
            rows.append({"episode": e["episode"], "name": e["name"], "aired": e["air_date"],
                         "future": bool(e["air_date"]) and e["air_date"] > today, "missing": it is None,
                         **(file_row(it, root) if it else {})})
        for (s_no, n), it in sorted(by_ep.items()):
            if s_no == season and n not in seen and it["episodes"][0] == n:
                rows.append({"episode": n, "name": it["ep_title"] or "(not on TMDB)", "aired": "", "unknown": True, **file_row(it, root)})
        info_s = next((x for x in (data or {}).get("show", {}).get("seasons", []) if x["season"] == season), {})
        poster = tvart.season_existing(show_dir, season) if show_dir else None
        have = sum(1 for r in rows if r.get("file") and not r.get("unknown"))
        unknown = sum(1 for r in rows if r.get("unknown"))
        aired = sum(1 for r in rows if not r.get("future") and not r.get("unknown"))
        seasons.append({"season": season, "name": info_s.get("name") or tvlib.season_folder(season),
                        "poster": img_url(key, "season", season=season, v=int(poster.stat().st_mtime)) if poster else image_url(info_s.get("poster_path"), "w342"),
                        "local_poster": bool(poster), "have": have, "aired": aired, "unknown": unknown,
                        "missing": sum(1 for r in rows if r.get("missing") and not r.get("future") and season != 0),
                        "episodes": sorted(rows, key=lambda r: r["episode"])})
    extras = [{"file": core.rel(Path(root, it["path"]), show_dir or root), "id": it["id"], "size": it["size"]}
              for it in eps if it["kind"] == "extra"]
    order = (m or {}).get("order")
    return {"key": key, "match": m, "order": order, "info": info, "edits": edits, "editable": tvlib.confirmed(m), "images": images,
            "artwork": art, "seasons": seasons, "extras": extras, "folder": eps[0]["show_dir"] or "(loose files)",
            "nfo": "tvshow.nfo" if nfo_info else None, "by_tmm": bool(nfo_info and nfo_info.get("by_tmm")),
            "fetched": (data or {}).get("fetched")}


def order_fit(key: str) -> dict:
    """Each episode order this show can follow, scored by how many of your files' episode titles (from their
    names and .nfo files) agree with it, for the whole show and season by season. Also suggests the best order
    for each season (a show's files can follow TMDB for most seasons and TVDB/TVmaze for others)."""
    with core.lock:
        eps, root, _ = find(key)
    m = eps[0]["match"]
    if not tvlib.confirmed(m):
        raise UserError("Match this show on TMDB (and confirm the match) first.")
    samples = []
    for it in eps:
        if it["kind"] != "episode":
            continue
        titles = [it["ep_title"]] if it.get("ep_title") else []
        nfo_path = Path(root, it["path"]).with_suffix(".nfo")
        if nfo_path.is_file():
            titles += [r.findtext("title") for r in nfo.parse_all(nfo_path.read_bytes()) if r.findtext("title")]
        if titles:
            samples.append((it, titles))
    orders, per_season = [], {}
    for opt in tvmeta.order_options(m["tmdb_id"]):
        try:
            data = tvmeta.data_for({"tmdb_id": m["tmdb_id"], "order": opt["id"]})
        except (UserError, core.TmdbError) as err:
            orders.append({**opt, "error": str(err), "agree": 0, "compared": len(samples)})
            continue
        agree = 0
        for it, titles in samples:
            ok = any(tvlib.title_agrees({**it, "ep_title": t}, data) and
                     any((tvmeta.episode(data, it["season"], n) or {}).get("name") for n in it["episodes"]) for t in titles)
            agree += ok
            cell = per_season.setdefault(str(it["season"]), {}).setdefault(opt["id"] or "", [0, 0])
            cell[0] += ok
            cell[1] += 1
        orders.append({**opt, "agree": agree, "compared": len(samples)})
    whole = m.get("order")
    current = {str(s): o for s, o in (m.get("season_orders") or {}).items()}
    for o in orders:
        o["current"] = o["id"] == whole and not current
    # the best order for each season; on a tie, the show's main order (or TMDB's)
    suggested, mix_agree = {}, 0
    for season, cells in per_season.items():
        best = max(cells.items(), key=lambda kv: (kv[1][0], kv[0] == (whole or "")))
        mix_agree += best[1][0]
        if best[0] != (whole or ""):
            suggested[season] = best[0] or None
    best_whole = max(orders, key=lambda o: (o["agree"], o["id"] == whole)) if orders else None
    names = {o["id"] or "": o["name"] for o in orders}
    mix = None
    if suggested and best_whole and mix_agree > best_whole["agree"]:
        mix = {"agree": mix_agree, "compared": len(samples), "season_orders": suggested,
               "description": "; ".join(f"{'specials' if s == '0' else 'season ' + s}: {names.get(o or '', o).split(' (')[0]}"
                                        for s, o in sorted(suggested.items(), key=lambda kv: int(kv[0]))),
               "current": current == {k: v for k, v in suggested.items()}}
    elif best_whole:
        best_whole["best"] = True
    return {"orders": orders, "mix": mix, "main": whole, "season_orders": current}


def set_order(key: str, order, season_orders: dict = None) -> dict:
    with core.lock:
        eps, _, _ = find(key)
        m = eps[0]["match"]
        if not tvlib.confirmed(m):
            raise UserError("Match this show on TMDB (and confirm the match) first.")
        allowed = [o["id"] for o in tvmeta.order_options(m["tmdb_id"])]
        season_orders = {str(int(s)): (o or None) for s, o in (season_orders or {}).items()}
        if order not in allowed or any(o not in allowed for o in season_orders.values()):
            raise UserError("That episode order isn't available for this show.")
        trial = {**m, "order": order, "season_orders": season_orders}
        tvmeta.data_for(trial)  # fetch it first, so a failure leaves the show as it was
        matches = core.load_json(core.TV_MATCHES, {})
        matches[key]["order"] = order
        matches[key]["season_orders"] = season_orders
        core.save_json(core.TV_MATCHES, matches)
    return detail(key)


def file_row(it: dict, root: Path) -> dict:
    return {"file": Path(it["path"]).name, "id": it["id"], "size": it["size"], "named": it["named"], "mismatch": it.get("mismatch"),
            "has": it["files"], "multi": len(it["episodes"]) > 1, "season_guessed": it.get("season_guessed")}


def image(key: str, kind: str, season=None, item_id: str = None):
    with core.lock:
        eps, root, show_dir = find(key)
    p = None
    if kind in tvart.SHOW_KINDS and show_dir:
        p = tvart.show_existing(show_dir, kind)
    elif kind == "season" and show_dir and str(season).lstrip("-").isdigit():
        p = tvart.season_existing(show_dir, int(season))
    elif kind == "thumb":
        it = next((i for i in eps if i["id"] == item_id), None)
        p = tvart.thumb_path(Path(root, it["path"])) if it else None
    if not p or not p.is_file():
        raise UserError("This show has no such artwork.")
    return p, movie.IMAGE_TYPES.get(p.suffix.lower(), "application/octet-stream")


def art_options(key: str, kind: str, season=None) -> list:
    with core.lock:
        eps, _, _ = find(key)
    m = eps[0]["match"]
    if not tvlib.confirmed(m):
        raise UserError("Match this show on TMDB (and confirm the match) first.")
    if kind not in tvart.SHOW_KINDS and kind != "season":
        raise UserError("Unknown kind of artwork.")
    return tvart.options(tvmeta.show_data(m["tmdb_id"]), kind, int(season) if kind == "season" else None)


def set_art(key: str, kind: str, url: str, season=None) -> dict:
    with core.lock:
        eps, _, show_dir = find(key)
        m = eps[0]["match"]
        if not show_dir or not tvlib.confirmed(m):
            raise UserError("Rename this show into its own folder (and confirm its match) first.")
        tvart.choose(show_dir, tvmeta.show_data(m["tmdb_id"]), kind, url, int(season) if kind == "season" else None)
    return detail(key)


def edit(key: str, fields: dict, reset: bool = False) -> dict:
    with core.lock:
        eps, root, show_dir = find(key)
        m = eps[0]["match"]
        if not tvlib.confirmed(m):
            raise UserError("Match this show on TMDB (and confirm the match) before editing it.")
        edit_key = f"tv:{m['tmdb_id']}"
        all_edits = core.load_json(core.EDITS, {})
        if reset:
            all_edits.pop(edit_key, None)
        else:
            cleaned = movie.clean_edits({k: v for k, v in fields.items() if k not in ("tagline", "set")})
            all_edits[edit_key] = {**all_edits.get(edit_key, {}), **cleaned}
        core.save_json(core.EDITS, all_edits)
        if show_dir:
            data = tvmeta.show_data(m["tmdb_id"])
            path = show_dir / "tvshow.nfo"
            existing = nfo.parse_xml(path.read_bytes(), "tvshow") if path.exists() else None
            if existing is not None and not reset:
                nfo.apply_edits(existing, all_edits.get(edit_key, {}))
                path.write_bytes(nfo.to_bytes(existing))
                core.changed(path)
            else:
                tvnfo.write_show(show_dir, data, m["tmdb_id"], overwrite=True)
                if reset:
                    el = nfo.parse_xml(path.read_bytes(), "tvshow")
                    for lock in el.findall("lockdata"):
                        el.remove(lock)
                    path.write_bytes(nfo.to_bytes(el))
                    core.changed(path)
    return detail(key)


def delete_preview(key: str, what: str, season=None, item_id: str = None) -> dict:
    with core.lock:
        targets, ids = tvlib.delete_targets(key, what, int(season) if season not in (None, "") else None, item_id)
        root = core.tv_state["root"]
    files = []
    for t in targets:
        files += [f for f in t.rglob("*") if f.is_file()] if t.is_dir() else [t]
    return {"what": what, "targets": [core.rel(t, root) for t in targets], "count": len(files),
            "size": sum(f.stat().st_size for f in files if f.exists()), "episodes": len(ids),
            "permanent": core.is_network_path(targets[0]) if targets else False}


def delete(key: str, what: str, season=None, item_id: str = None) -> dict:
    if core.job_running():
        raise UserError("Wait for the current task to finish before deleting.")
    with core.lock:
        targets, _ = tvlib.delete_targets(key, what, int(season) if season not in (None, "") else None, item_id)
        failed = []
        for t in targets:
            try:
                core.discard(t)
            except UserError:
                raise
            except Exception as err:
                failed.append({"name": t.name, "error": getattr(err, "strerror", None) or str(err)})
        if what == "show" and not failed:
            matches = core.load_json(core.TV_MATCHES, {})
            matches.pop(key, None)
            core.save_json(core.TV_MATCHES, matches)
        tvlib.rescan()
    return {**tvlib.current(), "removed": len(targets) - len(failed), "failed": failed}


def open_folder(key: str) -> None:
    with core.lock:
        eps, root, show_dir = find(key)
    if os.name != "nt":
        raise UserError("Opening folders only works on Windows.")
    os.startfile(str(show_dir or root))
