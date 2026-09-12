# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Keeps Lexicon DJ in sync with Download Lists, via the Lexicon Local API
(http://localhost:48624 -- enable it in Lexicon under Settings > Integrations).

Four jobs, each toggleable from the API Integrations tab:

1. Playlist mirroring (sync_enabled): every download list gets a matching
   regular playlist in Lexicon, kept inside a playlist folder ("nicotine"
   by default) and managed track-by-track by this module -- regular rather
   than a location-smartlist so that songs the user already owned elsewhere
   on disk (see 4.) can be members too. Smartlists left over from the
   earlier design are converted in place, keeping their tracks.

2. Auto-import (auto_import): every finished download is added to the
   Lexicon library right away (POST /tracks) and appended to its list's
   playlist, with no manual import step in Lexicon.

3. Duplicate replacement (dedupe_enabled): right after importing a track,
   the library is searched for another version of the same song (same
   artist + same title ignoring "(Extended Mix)"-style qualifiers). The
   preferred version wins -- significantly longer first if
   dedupe_prefer_longer (extended over radio edit), then lossless/higher
   bitrate if dedupe_prefer_lossless -- and the loser is removed from the
   Lexicon library only (never from disk), after its playlist memberships
   are moved over to the winner and, when the newcomer wins, the old
   track's rating/energy/color/tags are copied across. Cue points are NOT
   copied: with different durations the positions would land in the wrong
   places, which is worse than no cues. Exception: a duplicate that IS the
   same recording (same length and quality) is treated as a relocation --
   the new copy wins and cues/beatgrid come along.

4. Library-first (library_first): a song newly added to a download list is
   looked up in the Lexicon library BEFORE any Soulseek search. If any
   version of it is already there, the existing track goes straight into
   the list's playlist and the item is marked In Library instead of
   downloading a copy the user already owns. While waiting for a check,
   items hold in a Checking Library state; if Lexicon is unreachable, a
   one-per-outage prompt (see the API Integrations tab) offers retrying or
   downloading without the check.

Lexicon only serves its API while the app is running, so everything still
to be done (unmirrored lists, unimported finished downloads) sits in a
pending state that is retried on a timer and persisted in
lexicon_sync.json -- whenever Lexicon is (re)opened, the next pass goes
through, surviving restarts of either app. The list-name ->
Lexicon-playlist-id mapping is persisted too, so renames follow the
existing smartlist instead of creating a duplicate.

Removing a download list deliberately does NOT delete its smartlist:
deleting playlists from someone's DJ library is not something a sync job
should ever do on its own.

Can also be run standalone (python -m pynicotine.lexiconsync) for a single
sync pass while Nicotine+ is closed, e.g. from a LaunchAgent."""

import json
import os
import re
import threading
import time
import unicodedata

from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

from pynicotine.config import config
from pynicotine.events import events
from pynicotine.logfacility import log
from pynicotine.utils import safe_path_join

LOSSLESS_EXTENSIONS = {".flac", ".wav", ".aiff", ".aif", ".ape"}

# Files considered when backfilling a list folder's existing downloads
AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma", ".ape", ".aiff", ".aif", ".mp4"
}

# A duplicate this much longer than the other is a different (extended) cut,
# which outranks any quality difference when dedupe_prefer_longer is on
SIGNIFICANT_LENGTH_SECONDS = 30
SIGNIFICANT_LENGTH_RATIO = 1.10

BRACKETED_CONTENT_PATTERN = re.compile(r"[(\[][^)\]]*[)\]]")
WHITESPACE_PATTERN = re.compile(r"\s+")
NON_WORD_PATTERN = re.compile(r"[^\w]+")

# Qualifier words that do NOT make a different version of a song: an
# extended mix, radio edit and original mix are all the same recording for
# matching purposes (which one to *keep* is the prefer-longer/lossless
# preferences' job). Anything else left in a bracketed qualifier -- a
# remixer's name, "remix", "bootleg", "acoustic", "live" -- marks a
# genuinely different version that must only match itself.
NEUTRAL_QUALIFIER_WORDS = {
    "original", "mix", "extended", "version", "radio", "edit", "club", "album", "single",
    "remaster", "remastered", "clean", "dirty", "explicit", "intro", "outro", "official",
    "full", "length", "mono", "stereo", "bonus", "deluxe", "digital", "audio", "hq", "hd"
}

FEATURING_QUALIFIER_PATTERN = re.compile(r"^(feat|ft|featuring|with|w)\b", re.IGNORECASE)

# Words that, appearing in a qualifier, mark a distinct version outright --
# used to recognize Spotify's bracket-less "Song - Artist Remix" style
STRONG_VERSION_WORDS = {
    "remix", "rmx", "bootleg", "rework", "reworked", "flip", "vip", "mashup", "cover",
    "acoustic", "live", "instrumental", "acapella", "dub", "unofficial"
}


def _split_dash_qualifiers(title):
    """Split "Song - Artist Remix" / "Song - Radio Edit" style titles into
    (base text, [qualifier chunks]). A trailing " - X" segment counts as a
    qualifier when it names a version (contains a strong version word) or is
    made up purely of neutral words like "Radio Edit"."""

    segments = (title or "").split(" - ")
    qualifiers = []

    while len(segments) > 1:
        tokens = [word for word in NON_WORD_PATTERN.split(segments[-1].casefold()) if word]

        if not tokens:
            segments.pop()
            continue

        if (any(word in STRONG_VERSION_WORDS for word in tokens)
                or all(word in NEUTRAL_QUALIFIER_WORDS for word in tokens)):
            qualifiers.append(segments.pop())
            continue

        break

    return " - ".join(segments), qualifiers


class LexiconAPIError(Exception):
    pass


class LexiconClient:
    """Minimal Lexicon Local API client (urllib only, no dependencies)."""

    REQUEST_TIMEOUT = 10

    # Lexicon playlist types
    TYPE_FOLDER = 1
    TYPE_PLAYLIST = 2
    TYPE_SMARTLIST = 3

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def _request(self, method, path, payload=None, query=None):

        data = None
        headers = {"Accept": "application/json"}

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        url = f"{self.base_url}/v1{path}"

        if query:
            url += "?" + "&".join(
                f"{quote(str(key))}={quote(str(value))}" for key, value in query.items())

        request = Request(url, data=data, headers=headers, method=method)

        try:
            with urlopen(request, timeout=self.REQUEST_TIMEOUT) as response:
                body = response.read()

        except HTTPError as error:
            # Lexicon answered with an error: surface its own message, it's
            # far more diagnosable than "400: Bad Request"
            try:
                detail = error.read().decode("utf-8", "replace")[:200]
            except OSError:
                detail = ""

            raise LexiconAPIError(f"{error}{' — ' + detail if detail else ''}") from error

        except (OSError, URLError) as error:
            # Includes connection refused, i.e. Lexicon not running
            raise LexiconAPIError(error) from error

        if not body:
            return {}

        try:
            return json.loads(body)

        except ValueError as error:
            raise LexiconAPIError(f"invalid JSON response: {error}") from error

    # Playlists #

    def get_playlist_tree(self):
        """Every playlist/folder/smartlist as a flat list of node dicts."""

        response = self._request("GET", "/playlists")
        playlists = response.get("data", {}).get("playlists", [])

        nodes = []
        remaining = list(playlists)

        while remaining:
            node = remaining.pop()

            if not isinstance(node, dict):
                continue

            nodes.append(node)

            # The tree nests children under a list-valued key whose exact name
            # is undocumented, so accept any of them
            for children_key in ("playlists", "children", "items"):
                children = node.get(children_key)

                if isinstance(children, list):
                    remaining += children

        return nodes

    def get_playlist(self, playlist_id):
        response = self._request("GET", "/playlist", query={"id": playlist_id})
        return response.get("data", {}).get("playlist") or {}

    def create_playlist(self, name, playlist_type, parent_id=None, smartlist=None):

        payload = {"name": name, "type": playlist_type}

        if parent_id is not None:
            payload["parentId"] = parent_id

        if smartlist is not None:
            payload["smartlist"] = smartlist

        response = self._request("POST", "/playlist", payload)
        playlist_id = response.get("data", {}).get("id")

        if playlist_id is None:
            raise LexiconAPIError(f"no playlist ID returned when creating {name!r}")

        return playlist_id

    def update_playlist(self, playlist_id, name=None, smartlist=None):

        payload = {"id": playlist_id}

        if name is not None:
            payload["name"] = name

        if smartlist is not None:
            payload["smartlist"] = smartlist

        self._request("PATCH", "/playlist", payload)

    def delete_playlists(self, playlist_ids):
        self._request("DELETE", "/playlists", {"ids": playlist_ids})

    def add_playlist_tracks(self, playlist_id, track_ids):
        self._request("PATCH", "/playlist-tracks", {"id": playlist_id, "trackIds": track_ids})

    def remove_playlist_tracks(self, playlist_id, track_ids):
        self._request("DELETE", "/playlist-tracks", {"id": playlist_id, "trackIds": track_ids})

    # Tracks #

    def get_all_tracks(self, fields):
        """Every track in the library, restricted to the given fields.
        Paginated at the API's 1000-track cap; one bulk sweep like this is
        FAR cheaper than hundreds of per-file requests."""

        tracks = []
        offset = 0
        limit = 1000

        for _page in range(200):  # hard stop far above any real library
            response = self._request("GET", "/tracks", {"limit": limit, "offset": offset,
                                                        "fields": fields})
            data = response.get("data", {})
            page_tracks = [track for track in data.get("tracks", []) if isinstance(track, dict)]
            tracks += page_tracks
            offset += len(page_tracks)

            total = data.get("total")

            if not page_tracks or (isinstance(total, int) and offset >= total):
                break

        return tracks

    def add_tracks(self, locations):
        """Import files into the Lexicon library. Returns the created/existing
        track dicts (shape observed: data.tracks is one track or a list)."""

        response = self._request("POST", "/tracks", {"locations": locations})
        tracks = response.get("data", {}).get("tracks")

        if isinstance(tracks, dict):
            return [tracks]

        if isinstance(tracks, list):
            return [track for track in tracks if isinstance(track, dict)]

        return []

    def search_tracks(self, filters):
        """Case-insensitive substring search, e.g. {"title": "amman"}."""

        query = {f"filter[{field}]": value for field, value in filters.items()}
        response = self._request("GET", "/search/tracks", query=query)
        tracks = response.get("data", {}).get("tracks", [])

        return [track for track in tracks if isinstance(track, dict)]

    def update_track(self, track_id, edits):
        self._request("PATCH", "/track", {"id": track_id, "edits": edits})

    def delete_tracks(self, track_ids):
        """Removes tracks from the Lexicon library. Never touches the files
        themselves -- nothing in this module ever deletes from disk."""
        self._request("DELETE", "/track", {"ids": track_ids})

    # Helpers #

    @staticmethod
    def node_type(node):

        try:
            return int(node.get("type"))

        except (TypeError, ValueError):
            return None

    @staticmethod
    def location_smartlist(folder_path):
        """Smartlist rule set: file location contains this folder."""

        # Trailing separator so "House" can't also match "House Classics"
        rule_value = folder_path.rstrip(os.sep) + os.sep

        return {
            "matchAll": True,
            "rules": [{
                "field": "location",
                "operator": "StringContains",
                "values": [rule_value],
                "or": False
            }]
        }


# With this many files queued, one bulk sweep of the library's locations is
# cheaper than checking files one by one
BULK_PREFILTER_THRESHOLD = 20


def unique_location_suffix(path):
    """A file path in the comparable form of Lexicon's locationUnique field
    (unicode-normalized, lowercased, macOS drive prefix like "/Volumes/
    Macintosh HD" included on their side) -- cut down to the "/users/..."
    suffix both sides share, so local paths and library entries can be
    matched with a plain dict lookup."""

    normalized = unicodedata.normalize("NFC", str(path or "")).casefold()
    index = normalized.find("/users/")
    return normalized[index:] if index >= 0 else normalized


def effective_list_folder(name, download_folder_path=None, use_name_subfolder=None):
    """Where downloads for a list land, mirroring
    DownloadList.effective_download_folder_path but computable from the raw
    download_lists.json data (for standalone runs)."""

    base_folder_path = download_folder_path or os.path.normpath(
        os.path.expandvars(config.sections["transfers"]["downloaddir"]))

    if use_name_subfolder is None:
        use_name_subfolder = config.sections["transfers"]["downloadlistdefaultnamesubfolder"]

    if not use_name_subfolder:
        return base_folder_path

    return safe_path_join(base_folder_path, name)


# Duplicate comparison #

def base_title(title):
    """Title with "(Extended Mix)"/"[Remaster]"-style qualifiers stripped --
    bracketed or in Spotify's trailing "- Radio Edit"/"- Artist Remix" dash
    style -- for matching versions of the same song against each other.
    Which version a qualifier names is version_signature's job."""

    base, _qualifiers = _split_dash_qualifiers(title)
    stripped = BRACKETED_CONTENT_PATTERN.sub(" ", base)
    return WHITESPACE_PATTERN.sub(" ", stripped).strip().casefold()


def version_signature(title):
    """What VERSION of the song a title names, distilled from its bracketed
    qualifiers: "Song", "Song (Extended Mix)" and "Song (Radio Edit)" all
    give "" (same version, different cuts), while "Song (Arlane Extended
    Bootleg)" gives "arlane bootleg" and only matches other Arlane bootlegs.
    Featuring credits are ignored -- they name collaborators, not versions."""

    base, chunks = _split_dash_qualifiers(title)
    chunks += BRACKETED_CONTENT_PATTERN.findall(base)
    words = []

    for chunk in chunks:
        chunk = chunk.strip("([)]").strip()

        if FEATURING_QUALIFIER_PATTERN.match(chunk):
            continue

        for word in NON_WORD_PATTERN.split(chunk.casefold()):
            if word and word not in NEUTRAL_QUALIFIER_WORDS:
                words.append(word)

    return " ".join(sorted(set(words)))


def is_lossless(location):
    _root, extension = os.path.splitext(location or "")
    return extension.lower() in LOSSLESS_EXTENSIONS


def _duration(track):
    try:
        return float(track.get("duration") or 0)
    except (TypeError, ValueError):
        return 0


def _bitrate(track):
    try:
        return int(track.get("bitrate") or 0)
    except (TypeError, ValueError):
        return 0


def is_same_song(track_a, track_b):
    """Same song in the same VERSION: same artist, same base title, and the
    same version signature -- so a remix never counts as a duplicate of the
    original (or of a different remix), while extended/radio/original cuts
    of one version do. Matching stays deliberately strict: a false
    "duplicate" removes a track from the library, a false negative just
    leaves both."""

    if not base_title(track_a.get("title")) or not base_title(track_b.get("title")):
        return False

    return (base_title(track_a.get("title")) == base_title(track_b.get("title"))
            and version_signature(track_a.get("title")) == version_signature(track_b.get("title"))
            and str(track_a.get("artist") or "").strip().casefold()
            == str(track_b.get("artist") or "").strip().casefold())


def is_same_audio(track_a, track_b):
    """Two library entries that are, for all practical purposes, the same
    recording: same duration (within a second) and same quality. Used to
    treat a re-downloaded copy as a relocation rather than a version choice
    -- and since the audio matches, cue points transfer safely."""

    return (abs(_duration(track_a) - _duration(track_b)) <= 1
            and _bitrate(track_a) == _bitrate(track_b)
            and is_lossless(track_a.get("location")) == is_lossless(track_b.get("location")))


def preferred_track(new_track, old_track, prefer_longer=True, prefer_lossless=True):
    """Which of two duplicate tracks to keep. Ties keep the OLD track: it may
    carry cues, play history and playlist placements worth preserving."""

    if prefer_longer:
        new_duration = _duration(new_track)
        old_duration = _duration(old_track)
        threshold = max(SIGNIFICANT_LENGTH_SECONDS,
                        min(new_duration, old_duration) * (SIGNIFICANT_LENGTH_RATIO - 1))

        if new_duration - old_duration > threshold:
            return new_track

        if old_duration - new_duration > threshold:
            return old_track

    if prefer_lossless:
        new_lossless = is_lossless(new_track.get("location"))
        old_lossless = is_lossless(old_track.get("location"))

        if new_lossless != old_lossless:
            return new_track if new_lossless else old_track

        if _bitrate(new_track) > _bitrate(old_track):
            return new_track

        if _bitrate(old_track) > _bitrate(new_track):
            return old_track

    return old_track


# Sync passes (worker thread / standalone process only -- these block on HTTP) #

def sync_playlists(client, jobs, playlist_ids, parent_folder_name):
    """Ensure a regular (track-by-track) playlist exists for every
    (list_name, folder_path) job, inside the parent folder. Regular rather
    than location-smartlists so tracks the user already owned elsewhere on
    disk (see find_library_match) can be members too; the sync pipeline
    itself adds each imported download. A leftover smartlist from the
    earlier smartlist-based design is converted in place: its currently
    matched tracks carry over into the new playlist and the smartlist is
    deleted. Mutates playlist_ids in place. Returns the set of list names
    successfully synced.

    Raises LexiconAPIError if the playlist tree can't be fetched at all;
    per-job failures are logged and skipped instead, so one bad list can't
    block the rest."""

    nodes = client.get_playlist_tree()
    nodes_by_id = {node.get("id"): node for node in nodes}

    # Ensure the parent playlist folder
    parent_id = None

    for node in nodes:
        if (client.node_type(node) == client.TYPE_FOLDER
                and str(node.get("name", "")).casefold() == parent_folder_name.casefold()
                and node.get("folderType") is None):  # never adopt Lexicon's special root folders
            parent_id = node.get("id")
            break

    if parent_id is None:
        parent_id = client.create_playlist(parent_folder_name, client.TYPE_FOLDER)

    synced = set()

    for name, _folder_path in jobs:
        try:
            node = nodes_by_id.get(playlist_ids.get(name))

            if node is None:
                # Adopt an existing playlist/smartlist with this name under
                # our folder, e.g. state file lost or created by hand
                node = next(
                    (candidate for candidate in nodes
                     if client.node_type(candidate) in (client.TYPE_PLAYLIST, client.TYPE_SMARTLIST)
                     and candidate.get("parentId") == parent_id
                     and str(candidate.get("name", "")) == name),
                    None
                )

            if node is not None and client.node_type(node) == client.TYPE_SMARTLIST:
                # Convert the old location-smartlist into a regular playlist,
                # keeping whatever tracks its rule currently matches
                track_ids = client.get_playlist(node.get("id")).get("trackIds") or []
                client.delete_playlists([node.get("id")])

                playlist_id = client.create_playlist(name, client.TYPE_PLAYLIST, parent_id=parent_id)

                if track_ids:
                    client.add_playlist_tracks(playlist_id, track_ids)

                playlist_ids[name] = playlist_id
                log.add_debug("Lexicon: converted smartlist for list %s into a playlist "
                              "(%s tracks carried over)", (name, len(track_ids)))

            elif node is None:
                playlist_ids[name] = client.create_playlist(name, client.TYPE_PLAYLIST, parent_id=parent_id)
                log.add_debug("Lexicon: created playlist for list %s", name)

            else:
                playlist_ids[name] = node.get("id")

                if node.get("name") != name:
                    client.update_playlist(playlist_ids[name], name=name)
                    log.add_debug("Lexicon: renamed playlist for list %s", name)

            synced.add(name)

        except LexiconAPIError as error:
            log.add(_('Lexicon: syncing playlist for "%(name)s" failed: %(error)s'), {
                "name": name, "error": error
            })

    return synced


def find_library_match(client, term):
    """Look up a download-list search term (usually "Artist - Title" or
    "Title - Artist") in the Lexicon library. Different CUTS of the wanted
    version count (extended/radio/original mix), but a different VERSION
    never does: asking for the original won't match a remix the user
    happens to own, and asking for a remix won't match the original or a
    different remix (see version_signature). Returns the matched track
    dict, or None."""

    segments = [part.strip() for part in term.split(" - ") if part.strip()]

    if len(segments) < 2:
        orderings = [(None, term.strip())]
    else:
        # Try every dash as the title/artist divider, in both orders --
        # Spotify terms are "Title - Artists" where the title itself can
        # contain a dash qualifier ("Impossible - &ME Remix - Röyksopp")
        orderings = []

        for index in range(1, len(segments)):
            left = " - ".join(segments[:index])
            right = " - ".join(segments[index:])
            orderings += [(left, right), (right, left)]

    for artist_part, title_part in orderings:
        if artist_part and any(
                word in STRONG_VERSION_WORDS for word in NON_WORD_PATTERN.split(artist_part.casefold())):
            # A "remix"/"bootleg"/... in the supposed artist half means this
            # split put version info on the wrong side -- matching on it
            # would let a remix request match the original
            continue

        search_title = base_title(title_part)

        if not search_title:
            continue

        for track in client.search_tracks({"title": search_title[:80]}):
            if base_title(track.get("title")) != search_title:
                continue

            if version_signature(track.get("title")) != version_signature(title_part):
                continue

            if artist_part is None:
                return track

            track_artist = str(track.get("artist") or "").strip().casefold()
            wanted_artist = artist_part.strip().casefold()

            # Artist fields often carry extra collaborators, so containment
            # either way counts
            if wanted_artist and (wanted_artist in track_artist or track_artist in wanted_artist):
                return track

    return None


def _replace_duplicate(client, winner, loser, copy_metadata, copy_cues=False):
    """Move the loser's normal-playlist memberships over to the winner, copy
    its user metadata across if requested (rating/energy/color/tags -- plus
    cue points and beatgrid when copy_cues says the audio is identical, see
    is_same_audio), then remove the loser from the library."""

    winner_id = winner.get("id")
    loser_id = loser.get("id")

    for node in client.get_playlist_tree():
        if client.node_type(node) != client.TYPE_PLAYLIST:
            continue

        track_ids = client.get_playlist(node.get("id")).get("trackIds") or []

        if loser_id in track_ids:
            if winner_id not in track_ids:
                client.add_playlist_tracks(node.get("id"), [winner_id])

            client.remove_playlist_tracks(node.get("id"), [loser_id])

    if copy_metadata:
        edits = {}

        for field in ("rating", "energy", "color", "comment"):
            value = loser.get(field)

            if value not in (None, "", 0):
                edits[field] = value

        tags = loser.get("tags")

        if isinstance(tags, list) and tags:
            edits["tags"] = [tag["id"] if isinstance(tag, dict) else tag for tag in tags]

        if copy_cues:
            for field in ("cuepoints", "tempomarkers"):
                value = loser.get(field)

                if isinstance(value, list) and value:
                    edits[field] = value

        if edits:
            client.update_track(winner_id, edits)

    client.delete_tracks([loser_id])


def import_finished_file(client, file_path, dedupe_enabled, prefer_longer, prefer_lossless):
    """Import one finished download into the Lexicon library and, if enabled,
    resolve it against an existing duplicate. Returns a (outcome, track_id)
    tuple -- outcome is a short human-readable string -- or (None, None) when
    the file couldn't be imported. track_id is the library track that ended up
    representing this file (kept-existing outcomes return the survivor)."""

    tracks = client.add_tracks([file_path])

    if not tracks:
        log.add_debug("Lexicon: importing %s returned no track", file_path)
        return None, None

    new_track = tracks[0]

    if not dedupe_enabled:
        return "imported", new_track.get("id")

    search_title = base_title(new_track.get("title"))

    if not search_title:
        return "imported", new_track.get("id")

    candidates = client.search_tracks({"title": search_title[:80]})
    duplicates = [
        track for track in candidates
        if track.get("id") != new_track.get("id")
        and track.get("locationUnique") != new_track.get("locationUnique")
        and is_same_song(track, new_track)
    ]

    if not duplicates:
        return "imported", new_track.get("id")

    for old_track in duplicates:
        if is_same_audio(new_track, old_track):
            # Same recording in a new place (e.g. re-downloaded into a list's
            # folder): the new copy wins, and since the audio is identical,
            # cues/beatgrid come along too
            _replace_duplicate(
                client, winner=new_track, loser=old_track, copy_metadata=True, copy_cues=True)
            continue

        winner = preferred_track(
            new_track, old_track, prefer_longer=prefer_longer, prefer_lossless=prefer_lossless)

        if winner is old_track:
            # The library already has the better version: withdraw the new
            # import again (library only, the file stays on disk)
            _replace_duplicate(client, winner=old_track, loser=new_track, copy_metadata=False)
            return (f"kept existing version of \"{new_track.get('artist')} - {new_track.get('title')}\"",
                    old_track.get("id"))

        _replace_duplicate(client, winner=new_track, loser=old_track, copy_metadata=True)

    return (f"replaced older version of \"{new_track.get('artist')} - {new_track.get('title')}\"",
            new_track.get("id"))


def process_pending_files(client, pending_files, playlist_ids, dedupe_enabled, prefer_longer,
                          prefer_lossless, progress_callback=None):
    """Import queued finished downloads, adding each imported track to its
    list's Lexicon playlist. Mutates pending_files in place, keeping entries
    whose import failed on a (presumably transient) API error. Returns a list
    of outcome strings for what got processed. progress_callback, if given,
    is called as (done, total) after every entry."""

    outcomes = []
    total = len(pending_files)
    done = 0

    # playlist id -> set of track ids already in it, fetched lazily
    member_cache = {}

    if total > BULK_PREFILTER_THRESHOLD:
        # Fast path for big queues (e.g. a Sync Now backfill): fetch the
        # library's locations once, instantly recognize every file already
        # imported, and only ensure its playlist membership -- one bulk add
        # per playlist. Skipping this on failure just means the per-file
        # path below does the same work the slow way.
        try:
            known_locations = {
                unique_location_suffix(track.get("locationUnique")): track.get("id")
                for track in client.get_all_tracks(["id", "locationUnique"])
                if track.get("locationUnique") and track.get("id") is not None
            }

            new_members = {}  # playlist id -> track ids to ensure

            for entry in list(pending_files):
                list_name, file_path = entry
                track_id = known_locations.get(unique_location_suffix(file_path))

                if track_id is None:
                    continue  # genuinely new: full import below

                playlist_id = playlist_ids.get(list_name)

                if playlist_id is not None:
                    new_members.setdefault(playlist_id, set()).add(track_id)

                pending_files.remove(entry)
                done += 1

                if progress_callback is not None:
                    progress_callback(done, total)

            for playlist_id, track_ids in new_members.items():
                existing = set(client.get_playlist(playlist_id).get("trackIds") or [])
                missing = sorted(track_ids - existing)

                if missing:
                    client.add_playlist_tracks(playlist_id, missing)

                member_cache[playlist_id] = existing | track_ids

            if done:
                outcomes.append(f"recognized {done} already-imported file(s) without re-importing")

        except LexiconAPIError as error:
            log.add_debug("Lexicon: bulk pre-check failed, falling back to per-file imports: %s", error)

    for entry in list(pending_files):
        done += 1

        if progress_callback is not None:
            progress_callback(done, total)

        list_name, file_path = entry

        if not os.path.exists(file_path):
            # Moved or renamed since it finished: nothing to import anymore
            pending_files.remove(entry)
            continue

        try:
            outcome, track_id = import_finished_file(
                client, file_path,
                dedupe_enabled=dedupe_enabled,
                prefer_longer=prefer_longer,
                prefer_lossless=prefer_lossless
            )

        except LexiconAPIError as error:
            if isinstance(error.__cause__, HTTPError):
                # Lexicon answered and said no (e.g. an unreadable file):
                # retrying won't change its mind, so drop it rather than
                # hammering the API once a minute forever
                log.add(_('Lexicon: could not import "%(file)s", skipping it: %(error)s'), {
                    "file": file_path, "error": error
                })
                pending_files.remove(entry)
                continue

            log.add_debug("Lexicon: importing %s failed, will retry: %s", (file_path, error))
            continue

        # The import itself succeeded; a failure adding it to the playlist
        # (e.g. a stale playlist ID) is NOT the file's fault -- keep the
        # entry queued so the next pass, with the playlist re-ensured,
        # finishes the job
        try:
            playlist_id = playlist_ids.get(list_name)

            if track_id is not None and playlist_id is not None:
                if playlist_id not in member_cache:
                    member_cache[playlist_id] = set(client.get_playlist(playlist_id).get("trackIds") or [])

                if track_id not in member_cache[playlist_id]:
                    client.add_playlist_tracks(playlist_id, [track_id])
                    member_cache[playlist_id].add(track_id)

        except LexiconAPIError as error:
            log.add_debug("Lexicon: adding %s to playlist failed, will retry: %s", (file_path, error))
            continue

        pending_files.remove(entry)

        if outcome:
            outcomes.append(outcome)

    return outcomes


class LexiconSync:

    STATE_FILE_BASENAME = "lexicon_sync.json"

    # How often pending work is retried against the Lexicon API. Cheap: when
    # Lexicon is closed, a pass costs one refused localhost connection.
    POLL_INTERVAL = 60

    # How long Lexicon may stay unreachable (after the prompt about it went
    # unanswered) before waiting songs are searched for without the library
    # check, instead of sitting at "Checking…" indefinitely
    LIBRARY_CHECK_GRACE = 5 * 60

    def __init__(self):

        self.state_file_path = os.path.join(config.data_folder_path, self.STATE_FILE_BASENAME)

        # list name -> Lexicon playlist ID
        self._playlist_ids = {}

        # List names whose smartlist still needs creating/updating
        self._pending_lists = set()

        # Finished downloads waiting to be imported: (list_name, file_path)
        self._pending_files = []

        self._lock = threading.Lock()

        # Serializes all Lexicon WRITE traffic: the sync worker and the
        # library-check worker both ensure playlists exist, and running those
        # concurrently once raced the smartlist conversion into stale IDs
        self._api_mutex = threading.Lock()

        self._sync_thread = None
        self._check_thread = None
        self._poll_timer_id = None

        # Whether the "Lexicon unreachable, items waiting" prompt has already
        # been shown for the current outage, so it doesn't reappear on every
        # background retry (a user-initiated Retry resets it)
        self._unreachable_notified = False
        self._unreachable_since = None

        for event_name, callback in (
            ("start", self._start),
            ("quit", self._quit),
            ("add-download-list", self._add_download_list),
            ("rename-download-list", self._rename_download_list),
            ("remove-download-list", self._remove_download_list),
            ("download-list-item-finished", self._download_list_item_finished),
            ("update-download-list", self._update_download_list)
        ):
            events.connect(event_name, callback)

    @property
    def enabled(self):
        return config.sections["lexicon"]["sync_enabled"]

    def _start(self):

        self._load_state()

        # Reconcile every existing list once per run: adopts smartlists that
        # already exist, creates missing ones, repairs drifted names/rules
        with self._lock:
            self._pending_lists = set(self._known_list_names())

        self._poll_timer_id = events.schedule(delay=self.POLL_INTERVAL, callback=self._poll, repeat=True)
        events.schedule(delay=5, callback=self._poll)

    def _quit(self):

        if self._poll_timer_id is not None:
            events.cancel_scheduled(self._poll_timer_id)
            self._poll_timer_id = None

    def debug_snapshot(self):
        """Current internal state, for the Copy Debug Report button."""

        with self._lock:
            pending_files = list(self._pending_files)
            pending_lists = sorted(self._pending_lists)
            playlist_ids = dict(self._playlist_ids)

        return {
            "settings": dict(config.sections["lexicon"]),
            "playlist_ids": playlist_ids,
            "pending_lists": pending_lists,
            "pending_files_count": len(pending_files),
            "pending_files_sample": [file_path for _list_name, file_path in pending_files[:10]],
            "waiting_library_checks": self._waiting_library_check_items(),
            "unreachable_notified": self._unreachable_notified,
            "sync_thread_alive": bool(self._sync_thread is not None and self._sync_thread.is_alive()),
            "check_thread_alive": bool(self._check_thread is not None and self._check_thread.is_alive()),
            "state_file": self.state_file_path
        }

    def sync_now(self):
        """Queue every list for a fresh reconcile and kick off a pass right
        away. With auto-import on, this also backfills: every audio file
        already sitting in a list's folder is queued for import, catching
        downloads that finished before this feature existed (or while it was
        off). Re-importing a file Lexicon already has just returns its
        existing track, so pressing the button repeatedly is safe. Used by
        the "Sync Now" button in the API Integrations tab."""

        from pynicotine.core import core

        with self._lock:
            self._pending_lists.update(self._known_list_names())

        if config.sections["lexicon"]["auto_import"] and core.download_lists is not None:
            queued = 0

            for name, download_list in core.download_lists.lists.items():
                folder_path = download_list.effective_download_folder_path

                if not os.path.isdir(folder_path):
                    continue

                for root, _folders, files in os.walk(folder_path):
                    for basename in files:
                        _stem, extension = os.path.splitext(basename)

                        if extension.lower() not in AUDIO_EXTENSIONS:
                            continue

                        entry = (name, os.path.join(root, basename))

                        with self._lock:
                            if entry not in self._pending_files:
                                self._pending_files.append(entry)
                                queued += 1

            if queued:
                self._save_state()
                log.add(_("Lexicon: queued %(num)s existing file(s) for import"), {"num": queued})

        events.schedule(delay=1, callback=self._poll)
        events.schedule(delay=1, callback=self._library_check_poll)

    def check_connection(self, callback):
        """Probe the Lexicon API from a worker thread and report back via
        callback(reachable, detail) on the main thread."""

        api_url = config.sections["lexicon"]["api_url"]

        def probe():
            try:
                LexiconClient(api_url).get_playlist_tree()

            except LexiconAPIError as error:
                events.invoke_main_thread(callback, False, str(error))
                return

            events.invoke_main_thread(callback, True, api_url)

        threading.Thread(target=probe, name="LexiconProbeThread", daemon=True).start()

    def update_settings(self, sync_enabled=None, api_url=None, parent_folder=None, auto_import=None,
                        dedupe_enabled=None, dedupe_prefer_longer=None, dedupe_prefer_lossless=None,
                        library_first=None):
        """Apply settings from the GUI. Anything affecting where/whether
        playlists live re-queues every list for a fresh reconcile."""

        from pynicotine.core import core

        section = config.sections["lexicon"]
        resync_needed = False

        for key, value in (
            ("sync_enabled", sync_enabled),
            ("api_url", api_url),
            ("parent_folder", parent_folder),
            ("auto_import", auto_import),
            ("dedupe_enabled", dedupe_enabled),
            ("dedupe_prefer_longer", dedupe_prefer_longer),
            ("dedupe_prefer_lossless", dedupe_prefer_lossless),
            ("library_first", library_first)
        ):
            if value is None or section[key] == value:
                continue

            section[key] = value

            if key in ("sync_enabled", "api_url", "parent_folder"):
                resync_needed = True

            if key in ("sync_enabled", "library_first") and not value and core.download_lists is not None:
                # Nothing will check waiting items anymore: let them download
                core.download_lists.release_library_check_items()

        if resync_needed and self.enabled:
            with self._lock:
                self._pending_lists.update(self._known_list_names())

            events.schedule(delay=2, callback=self._poll)

    # State file #

    def _load_state(self):

        try:
            with open(self.state_file_path, encoding="utf-8") as file_handle:
                state = json.load(file_handle)

        except FileNotFoundError:
            return

        except (OSError, ValueError) as error:
            log.add_debug("Lexicon: could not load state file %s: %s", (self.state_file_path, error))
            return

        playlist_ids = state.get("playlist_ids", {})

        if isinstance(playlist_ids, dict):
            self._playlist_ids = {
                str(name): playlist_id for name, playlist_id in playlist_ids.items()
                if isinstance(playlist_id, int)
            }

        pending_files = state.get("pending_files", [])

        if isinstance(pending_files, list):
            self._pending_files = [
                (str(entry[0]), str(entry[1])) for entry in pending_files
                if isinstance(entry, (list, tuple)) and len(entry) == 2
            ]

    def _save_state(self):

        with self._lock:
            state = {
                "playlist_ids": dict(self._playlist_ids),
                "pending_files": [list(entry) for entry in self._pending_files]
            }

        try:
            with open(self.state_file_path, "w", encoding="utf-8") as file_handle:
                json.dump(state, file_handle, indent=4)

        except OSError as error:
            log.add_debug("Lexicon: could not save state file %s: %s", (self.state_file_path, error))

    # Download list events #

    @staticmethod
    def _known_list_names():

        from pynicotine.core import core

        if core.download_lists is None:
            return []

        return list(core.download_lists.lists)

    def _add_download_list(self, name):

        if not self.enabled:
            return

        with self._lock:
            self._pending_lists.add(name)

        # Sync soon rather than waiting out the poll interval
        events.schedule(delay=2, callback=self._poll)

    def _rename_download_list(self, old_name, new_name):

        with self._lock:
            playlist_id = self._playlist_ids.pop(old_name, None)

            if playlist_id is not None:
                self._playlist_ids[new_name] = playlist_id

            self._pending_lists.discard(old_name)
            self._pending_lists.add(new_name)

            self._pending_files = [
                (new_name if list_name == old_name else list_name, file_path)
                for list_name, file_path in self._pending_files
            ]

        events.schedule(delay=2, callback=self._poll)

    def _remove_download_list(self, name):
        """Forget the mapping, but leave the smartlist alone in Lexicon --
        deleting playlists from a DJ library is the user's call, never ours."""

        with self._lock:
            self._playlist_ids.pop(name, None)
            self._pending_lists.discard(name)
            self._pending_files = [
                entry for entry in self._pending_files if entry[0] != name
            ]

        self._save_state()

    def _download_list_item_finished(self, list_name, _term, file_path):

        if not self.enabled or not config.sections["lexicon"]["auto_import"]:
            return

        with self._lock:
            if (list_name, file_path) not in self._pending_files:
                self._pending_files.append((list_name, file_path))

        self._save_state()
        events.schedule(delay=5, callback=self._poll)

    # Library-first checks #

    def _update_download_list(self, _name):
        """New items may have arrived in the Checking Library state -- look
        soon (debounced by the thread-already-running guard)."""

        if self.enabled and config.sections["lexicon"]["library_first"]:
            events.schedule(delay=2, callback=self._library_check_poll)

    def retry_library_check(self):
        """The user pressed Retry on the unreachable prompt: allow the prompt
        to reappear if Lexicon is still down, and check again right away."""

        self._unreachable_notified = False
        events.schedule(delay=1, callback=self._library_check_poll)

    def _waiting_library_check_items(self):

        from pynicotine.core import core
        from pynicotine.downloadlists import DownloadListItemStatus

        if core.download_lists is None:
            return []

        return [
            (list_name, term)
            for list_name, download_list in core.download_lists.lists.items()
            for term, item in download_list.items.items()
            if item.status == DownloadListItemStatus.LIBRARY_CHECK
        ]

    def _library_check_poll(self):

        if not self.enabled or not config.sections["lexicon"]["library_first"]:
            return

        if self._check_thread is not None and self._check_thread.is_alive():
            return

        waiting = self._waiting_library_check_items()

        if not waiting:
            return

        self._check_thread = threading.Thread(
            target=self._run_library_check_thread, args=(waiting,),
            name="LexiconLibraryCheckThread", daemon=True
        )
        self._check_thread.start()

    def _run_library_check_thread(self, waiting):
        with self._api_mutex:
            self._run_library_check(waiting)

    def _run_library_check(self, waiting):

        from pynicotine.core import core

        section = config.sections["lexicon"]
        client = LexiconClient(section["api_url"])

        # Make sure the involved lists' playlists exist first -- this doubles
        # as the reachability probe
        jobs = []

        for name in {list_name for list_name, _term in waiting}:
            download_list = core.download_lists.lists.get(name)

            if download_list is not None:
                jobs.append((name, download_list.effective_download_folder_path))

        try:
            sync_playlists(client, jobs, self._playlist_ids, section["parent_folder"])

        except LexiconAPIError as error:
            log.add_debug("Lexicon: library check blocked, Lexicon unreachable: %s", error)
            now = time.time()

            if self._unreachable_since is None:
                self._unreachable_since = now

            if not self._unreachable_notified:
                self._unreachable_notified = True
                events.invoke_main_thread(events.emit, "lexicon-unreachable", len(waiting))

            elif now - self._unreachable_since >= self.LIBRARY_CHECK_GRACE:
                # The prompt went unanswered (or unseen) and Lexicon has stayed down:
                # don't leave the songs at "Checking…" forever. They're searched for
                # without the check (so one already owned may be re-downloaded); the
                # next outage starts a fresh grace period
                log.add(_("Lexicon: still not reachable after %(minutes)i minutes, searching for "
                          "%(num)s waiting song(s) without checking the library first"),
                        {"minutes": self.LIBRARY_CHECK_GRACE // 60, "num": len(waiting)})
                self._unreachable_since = None
                events.invoke_main_thread(core.download_lists.release_library_check_items)

            return

        self._unreachable_notified = False
        self._unreachable_since = None
        self._save_state()

        member_cache = {}
        num_found = 0

        for list_name, term in waiting:
            try:
                match = find_library_match(client, term)

                if match is None:
                    events.invoke_main_thread(
                        core.download_lists.resolve_library_check, list_name, term, False, None)
                    continue

                playlist_id = self._playlist_ids.get(list_name)

                if playlist_id is not None:
                    if playlist_id not in member_cache:
                        member_cache[playlist_id] = set(
                            client.get_playlist(playlist_id).get("trackIds") or [])

                    if match.get("id") not in member_cache[playlist_id]:
                        client.add_playlist_tracks(playlist_id, [match.get("id")])
                        member_cache[playlist_id].add(match.get("id"))

                events.invoke_main_thread(
                    core.download_lists.resolve_library_check, list_name, term, True,
                    match.get("location"))
                num_found += 1

            except LexiconAPIError as error:
                # Lexicon dropped away mid-check: whatever's left stays
                # waiting for the next poll
                log.add_debug("Lexicon: library check interrupted: %s", error)
                return

        if num_found:
            log.add(_("Lexicon: found %(num)s song(s) already in the library, skipping their "
                      "downloads"), {"num": num_found})

        # Anything that arrived while this batch ran gets its own batch right
        # away instead of waiting for the next event or poll. Only genuinely
        # NEW items count -- items from this batch may still show as waiting
        # until the main thread applies their resolutions, and rescheduling
        # for those would spin
        processed = set(waiting)

        if any(entry not in processed for entry in self._waiting_library_check_items()):
            events.schedule(delay=2, callback=self._library_check_poll)

    # Syncing #

    def _poll(self):

        if not self.enabled:
            return

        # Waiting library checks retry on the same cadence (silently -- the
        # unreachable prompt only shows once per outage)
        self._library_check_poll()

        if self._sync_thread is not None and self._sync_thread.is_alive():
            return

        from pynicotine.core import core

        if core.download_lists is None:
            return

        with self._lock:
            pending_lists = set(self._pending_lists)
            has_pending_files = bool(self._pending_files)

            # Also (re-)ensure the playlist of every list with files waiting
            # to import, so their playlist IDs are fresh before members are
            # added -- a list can have queued files without itself pending
            pending_lists.update(list_name for list_name, _file_path in self._pending_files)

        # Resolve each pending list's desired name/folder on this side, so the
        # worker thread never touches core state
        jobs = []

        for name in pending_lists:
            download_list = core.download_lists.lists.get(name)

            if download_list is None:
                with self._lock:
                    self._pending_lists.discard(name)
                continue

            jobs.append((name, download_list.effective_download_folder_path))

        if not jobs and not has_pending_files:
            return

        self._sync_thread = threading.Thread(
            target=self._run_sync_thread, args=(jobs,), name="LexiconSyncThread", daemon=True
        )
        self._sync_thread.start()

    def _run_sync_thread(self, jobs):
        with self._api_mutex:
            self._run_sync(jobs)

    def _run_sync(self, jobs):

        section = config.sections["lexicon"]
        client = LexiconClient(section["api_url"])

        try:
            synced = sync_playlists(client, jobs, self._playlist_ids, section["parent_folder"])

        except LexiconAPIError as error:
            # Most commonly: Lexicon isn't running. Everything stays pending,
            # the next poll retries.
            log.add_debug("Lexicon: sync pass failed, will retry: %s", error)
            return

        with self._lock:
            self._pending_lists.difference_update(synced)
            snapshot = list(self._pending_files)

        # process_pending_files mutates its argument, removing what it
        # handled -- work on a copy so the untouched snapshot can tell a
        # handled entry apart from one that arrived while this pass ran
        remaining = list(snapshot)

        def report_progress(done, total):
            events.invoke_main_thread(events.emit, "lexicon-import-progress", done, total)

        outcomes = process_pending_files(
            client, remaining, self._playlist_ids,
            dedupe_enabled=section["dedupe_enabled"],
            prefer_longer=section["dedupe_prefer_longer"],
            prefer_lossless=section["dedupe_prefer_lossless"],
            progress_callback=report_progress
        )

        if snapshot:
            # Whatever is left failed transiently and stays queued; tell the
            # bar this pass is over either way
            events.invoke_main_thread(events.emit, "lexicon-import-progress", len(snapshot), len(snapshot))

        with self._lock:
            arrivals = [entry for entry in self._pending_files if entry not in snapshot]
            self._pending_files = remaining + arrivals

        self._save_state()

        if synced:
            log.add(_('Lexicon: synced %(num)s smartlist(s) into the "%(folder)s" playlist folder'), {
                "num": len(synced),
                "folder": section["parent_folder"]
            })

        for outcome in outcomes:
            log.add(_("Lexicon: %s"), outcome)


def run_standalone():
    """Single sync pass without a running Nicotine+ core, reading the download
    lists straight from download_lists.json. Used by `python -m
    pynicotine.lexiconsync`, e.g. from a LaunchAgent that fires periodically so
    Lexicon picks up new smartlists and imports even while Nicotine+ is closed."""

    config.load_config()
    section = config.sections["lexicon"]

    if not section["sync_enabled"]:
        print("Lexicon sync is disabled (lexicon.sync_enabled)")
        return 0

    lists_file_path = os.path.join(config.data_folder_path, "download_lists.json")

    try:
        with open(lists_file_path, encoding="utf-8") as file_handle:
            list_entries = json.load(file_handle)

    except FileNotFoundError:
        list_entries = []

    jobs = []

    for entry in list_entries:
        name = entry.get("name")

        if not name:
            continue

        jobs.append((name, effective_list_folder(
            name,
            download_folder_path=entry.get("download_folder_path"),
            use_name_subfolder=entry.get("use_name_subfolder")
        )))

    state_file_path = os.path.join(config.data_folder_path, LexiconSync.STATE_FILE_BASENAME)
    playlist_ids = {}
    pending_files = []

    try:
        with open(state_file_path, encoding="utf-8") as file_handle:
            state = json.load(file_handle)

        playlist_ids = {name: playlist_id for name, playlist_id
                        in state.get("playlist_ids", {}).items() if isinstance(playlist_id, int)}
        pending_files = [
            (str(entry[0]), str(entry[1])) for entry in state.get("pending_files", [])
            if isinstance(entry, (list, tuple)) and len(entry) == 2
        ]

    except (OSError, ValueError):
        pass

    client = LexiconClient(section["api_url"])

    try:
        synced = sync_playlists(client, jobs, playlist_ids, section["parent_folder"])

    except LexiconAPIError as error:
        print(f"Lexicon is not reachable ({error}), nothing synced")
        return 1

    outcomes = process_pending_files(
        client, pending_files, playlist_ids,
        dedupe_enabled=section["dedupe_enabled"],
        prefer_longer=section["dedupe_prefer_longer"],
        prefer_lossless=section["dedupe_prefer_lossless"]
    )

    try:
        with open(state_file_path, "w", encoding="utf-8") as file_handle:
            json.dump({
                "playlist_ids": playlist_ids,
                "pending_files": [list(entry) for entry in pending_files]
            }, file_handle, indent=4)

    except OSError as error:
        print(f"Warning: could not save state file: {error}")

    print(f"Synced {len(synced)} of {len(jobs)} download list(s)")

    for outcome in outcomes:
        print(f"Lexicon: {outcome}")

    return 0


if __name__ == "__main__":
    raise SystemExit(run_standalone())
