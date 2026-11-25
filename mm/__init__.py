"""Media manager: a Jellyfin-focused movie library manager.

core       settings, storage, background tasks, file helpers
library    scanning, reading file names, folder rules, what each movie has, deleting
tmdbapi    TMDB search and matching (using IDs from existing .nfo files first)
movie      one movie: details, files, artwork, metadata edits, open folder
renamer    Jellyfin renaming with preview and undo
nfo        .nfo files (read, write, merge, edit)
leftovers  folders left with only artwork/.nfo/tracker files, and tracker junk: to the Recycle Bin
export     movies, episodes, missing episodes and collections as CSV or HTML
cli        command-line mode (python app.py --help)
trakt      Trakt: watched status into the .nfo files (device login)
bulk       bulk editing of genres, tags, collection and age rating
collections  collections (movie sets): which you have, which films are missing
artwork    posters, backdrops, logos, thumbnails, banners, clear art, disc art (TMDB + fanart.tv)
trailers   trailer downloads (TMDB + YouTube)
subtitles  subtitle downloads (OpenSubtitles.com)
web        the local web app and its API
tvlib      TV: scanning, reading episode names, grouping into shows, what each show has
tvmeta     TV: TMDB show matching (tvshow.nfo IDs first) and episode details, missing episodes
tvrename   TV: Show (Year)/Season N/Show - S01E02 - Title, with preview and undo
tvsort     TV: DVD extras and specials in a show folder: to TMDB's specials or Jellyfin's extras folders
tvnfo      TV: tvshow.nfo, season.nfo and episode .nfo files
tvart      TV: show artwork, season posters, episode thumbnails
tvextras   TV: subtitles per episode, trailers per show
tvshow     TV: one show's details, seasons, episodes, edits, deleting
tvweb      TV: the /api/tv/... endpoints
"""
