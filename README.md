# Media manager

A local web app that gets movie and TV libraries ready for **Jellyfin**: it matches files on TMDB, renames them the
way Jellyfin expects, writes `.nfo` files, and downloads artwork, trailers and subtitles. It is built as a safer,
Jellyfin-first alternative to tinyMediaManager.

Everything runs on your own computer at `http://127.0.0.1:8765`. Your files, keys and library data stay there.

## Starting it

Double-click **`run.bat`**. The first run sets up Python libraries (Python 3.9 or newer must be installed). The
page opens in your browser; closing the tab stops the app, unless watching for new files is set to keep running.

Open **Settings** and paste a free TMDB API key (themoviedb.org → Settings → API). Everything else is optional.

## What it does

| | |
|---|---|
| **Match** | Movies and shows on TMDB, using IDs already in `.nfo` files first. Episode orders (aired, DVD, absolute) per show or per season. |
| **Rename** | `Title (Year)/Title (Year).mkv`, `Show (Year)/Season 1/Show - S01E02 - Title.mkv`, or your own patterns. Always a preview first, then undo. Version labels (`- Theatrical`) are kept; movies whose files sit in different folders are held back for you to check. |
| **DVD extras** | Extras numbered as episodes go to TMDB's specials (season 0) when their titles match, otherwise to Jellyfin's extras folders. |
| **.nfo files** | Kodi/Jellyfin `.nfo` with technical details (codecs, HDR, audio, languages), IMDb / Rotten Tomatoes / Metacritic ratings (OMDb key), and your own edits, which survive rewrites. |
| **Artwork** | Posters, backdrops, logos, thumbnails, banners, clear art, disc art, season posters and episode thumbnails (TMDB, plus fanart.tv with a key). |
| **Trailers and subtitles** | Trailers from TMDB/YouTube; subtitles from OpenSubtitles.com, skipping languages already inside the video. |
| **Trakt** | Watched status from Trakt into the `.nfo` files. |
| **Collections** | Which films of a series you have and which are missing. |
| **Jellyfin** | Tells Jellyfin which folders changed after every task, so it refreshes just those. Checks Jellyfin's library settings for anything that fights the app's files, with one-click fixes. |
| **Watch for new files** | Every few minutes, new files that have finished copying are matched and get `.nfo` files and artwork. Renames always wait for you. |
| **Tools** | Playback check (which files Jellyfin will have to convert, per kind of player, and which can't be read), duplicates (copies compared side by side), leftovers (folders with nothing left to play). |
| **History** | Everything the app moved, recycled or wrote, task by task. **Restore** brings things back from the Recycle Bin; **Move back** undoes a task's moves when every file is still where the app put it. |
| **Export** | Movies, episodes, missing episodes and collections as CSV or a web page. |

## Safety

* Nothing is renamed, moved or deleted without a preview and your choice.
* Deleting means the **Recycle Bin**, and the app checks first that Windows will really keep it there (paths too
  long or items too big for the bin would otherwise be deleted for good).
* The last copy of a movie or episode is never recycled by the duplicates tool.
* Every change is listed under Tools → History, with Restore and Move back.
* API keys and passwords live only in `config.json`, which is never committed.

## Command line

```
.venv\Scripts\python app.py --help
.venv\Scripts\python app.py movies D:\Media\Movies scan match nfo artwork
.venv\Scripts\python app.py tv D:\Media\Shows scan match rename --apply
.venv\Scripts\python app.py watch
```

## Tests

```
.venv\Scripts\python tests\run_all.py
```
