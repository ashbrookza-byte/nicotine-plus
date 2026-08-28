# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Keeps Lexicon DJ in sync with Download Lists, via the Lexicon Local API
(http://localhost:48624 -- enable it in Lexicon under Settings > Integrations).

Three jobs, each toggleable from Wishlist Settings:

1. Smartlist mirroring (sync_enabled): every download list gets a matching
   smartlist in Lexicon, kept inside a playlist folder ("nicotine" by
   default), with one rule: file location contains the list's download
   folder. Lexicon then keeps the smartlist's contents current on its own
   as imported files land in that folder.

2. Auto-import (auto_import): every finished download is added to the
   Lexicon library right away (POST /tracks), so smartlists fill up without
   a manual import step in Lexicon.

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
   places, which is worse than no cues.

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

from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

from pynicotine.config import config
from pynicotine.events import events
from pynicotine.logfacility import log
from pynicotine.utils import safe_path_join

LOSSLESS_EXTENSIONS = {".flac", ".wav", ".aiff", ".aif", ".ape"}

# A duplicate this much longer than the other is a different (extended) cut,
# which outranks any quality difference when dedupe_prefer_longer is on
SIGNIFICANT_LENGTH_SECONDS = 30
SIGNIFICANT_LENGTH_RATIO = 1.10

BRACKETED_CONTENT_PATTERN = re.compile(r"[(\[][^)\]]*[)\]]")
WHITESPACE_PATTERN = re.compile(r"\s+")


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

    def add_playlist_tracks(self, playlist_id, track_ids):
        self._request("PATCH", "/playlist-tracks", {"id": playlist_id, "trackIds": track_ids})

    def remove_playlist_tracks(self, playlist_id, track_ids):
        self._request("DELETE", "/playlist-tracks", {"id": playlist_id, "trackIds": track_ids})

    # Tracks #

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
    """Title with "(Extended Mix)"/"[Remaster]"-style qualifiers stripped, for
    matching different cuts of the same song against each other."""

    stripped = BRACKETED_CONTENT_PATTERN.sub(" ", title or "")
    return WHITESPACE_PATTERN.sub(" ", stripped).strip().casefold()


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
    """Same song in a different (or identical) version: same artist and same
    base title. Matching stays deliberately strict -- a false "duplicate"
    removes a track from the library, a false negative just leaves both."""

    if not base_title(track_a.get("title")) or not base_title(track_b.get("title")):
        return False

    return (base_title(track_a.get("title")) == base_title(track_b.get("title"))
            and str(track_a.get("artist") or "").strip().casefold()
            == str(track_b.get("artist") or "").strip().casefold())


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

def sync_smartlists(client, jobs, playlist_ids, parent_folder_name):
    """Ensure a smartlist exists (and is up to date) for every
    (list_name, folder_path) job. Mutates playlist_ids in place. Returns the
    set of list names that were successfully synced.

    Raises LexiconAPIError if the playlist tree can't be fetched at all;
    per-job failures are logged and skipped instead, so one bad list can't
    block the rest."""

    folder_counts = {}

    for _name, folder_path in jobs:
        folder_counts[folder_path] = folder_counts.get(folder_path, 0) + 1

    for name, folder_path in jobs:
        if folder_counts[folder_path] > 1:
            log.add(_('Lexicon: list "%(name)s" shares its download folder with another list, so their '
                      'smartlists will show the same files — enable "Save into a subfolder named after '
                      'the list" to keep them apart'), {"name": name})
            break

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

    for name, folder_path in jobs:
        smartlist = client.location_smartlist(folder_path)

        try:
            node = nodes_by_id.get(playlist_ids.get(name))

            if node is not None and client.node_type(node) != client.TYPE_SMARTLIST:
                node = None

            if node is None:
                # Adopt an existing smartlist with this name under our folder,
                # e.g. state file lost or smartlist created by hand
                node = next(
                    (candidate for candidate in nodes
                     if client.node_type(candidate) == client.TYPE_SMARTLIST
                     and candidate.get("parentId") == parent_id
                     and str(candidate.get("name", "")) == name),
                    None
                )

            if node is None:
                playlist_ids[name] = client.create_playlist(
                    name, client.TYPE_SMARTLIST, parent_id=parent_id, smartlist=smartlist)
                log.add_debug("Lexicon: created smartlist for list %s (%s)", (name, folder_path))

            else:
                playlist_ids[name] = node.get("id")

                if node.get("name") != name or node.get("smartlist") != smartlist:
                    client.update_playlist(playlist_ids[name], name=name, smartlist=smartlist)
                    log.add_debug("Lexicon: updated smartlist for list %s (%s)", (name, folder_path))

            synced.add(name)

        except LexiconAPIError as error:
            log.add(_('Lexicon: syncing smartlist for "%(name)s" failed: %(error)s'), {
                "name": name, "error": error
            })

    return synced


def _replace_duplicate(client, winner, loser, copy_metadata):
    """Move the loser's normal-playlist memberships over to the winner, copy
    its user metadata across if requested (rating/energy/color/tags -- not
    cues, see module docstring), then remove the loser from the library."""

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

        if edits:
            client.update_track(winner_id, edits)

    client.delete_tracks([loser_id])


def import_finished_file(client, file_path, dedupe_enabled, prefer_longer, prefer_lossless):
    """Import one finished download into the Lexicon library and, if enabled,
    resolve it against an existing duplicate. Returns a short human-readable
    outcome string, or None when the file couldn't be imported."""

    tracks = client.add_tracks([file_path])

    if not tracks:
        log.add_debug("Lexicon: importing %s returned no track", file_path)
        return None

    new_track = tracks[0]

    if not dedupe_enabled:
        return "imported"

    search_title = base_title(new_track.get("title"))

    if not search_title:
        return "imported"

    candidates = client.search_tracks({"title": search_title[:80]})
    duplicates = [
        track for track in candidates
        if track.get("id") != new_track.get("id")
        and track.get("locationUnique") != new_track.get("locationUnique")
        and is_same_song(track, new_track)
    ]

    if not duplicates:
        return "imported"

    for old_track in duplicates:
        winner = preferred_track(
            new_track, old_track, prefer_longer=prefer_longer, prefer_lossless=prefer_lossless)

        if winner is old_track:
            # The library already has the better version: withdraw the new
            # import again (library only, the file stays on disk)
            _replace_duplicate(client, winner=old_track, loser=new_track, copy_metadata=False)
            return f"kept existing version of \"{new_track.get('artist')} - {new_track.get('title')}\""

        _replace_duplicate(client, winner=new_track, loser=old_track, copy_metadata=True)

    return f"replaced older version of \"{new_track.get('artist')} - {new_track.get('title')}\""


def process_pending_files(client, pending_files, dedupe_enabled, prefer_longer, prefer_lossless):
    """Import queued finished downloads. Mutates pending_files in place,
    keeping entries whose import failed on a (presumably transient) API error.
    Returns a list of outcome strings for what got processed."""

    outcomes = []

    for entry in list(pending_files):
        _list_name, file_path = entry

        if not os.path.exists(file_path):
            # Moved or renamed since it finished: nothing to import anymore
            pending_files.remove(entry)
            continue

        try:
            outcome = import_finished_file(
                client, file_path,
                dedupe_enabled=dedupe_enabled,
                prefer_longer=prefer_longer,
                prefer_lossless=prefer_lossless
            )

        except LexiconAPIError as error:
            log.add_debug("Lexicon: importing %s failed, will retry: %s", (file_path, error))
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

    def __init__(self):

        self.state_file_path = os.path.join(config.data_folder_path, self.STATE_FILE_BASENAME)

        # list name -> Lexicon playlist ID
        self._playlist_ids = {}

        # List names whose smartlist still needs creating/updating
        self._pending_lists = set()

        # Finished downloads waiting to be imported: (list_name, file_path)
        self._pending_files = []

        self._lock = threading.Lock()
        self._sync_thread = None
        self._poll_timer_id = None

        for event_name, callback in (
            ("start", self._start),
            ("quit", self._quit),
            ("add-download-list", self._add_download_list),
            ("rename-download-list", self._rename_download_list),
            ("remove-download-list", self._remove_download_list),
            ("download-list-item-finished", self._download_list_item_finished)
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

    def sync_now(self):
        """Queue every list for a fresh reconcile and kick off a pass right
        away (pending file imports ride along automatically). Used by the
        "Sync Now" button in the API Integrations tab."""

        with self._lock:
            self._pending_lists.update(self._known_list_names())

        events.schedule(delay=1, callback=self._poll)

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
                        dedupe_enabled=None, dedupe_prefer_longer=None, dedupe_prefer_lossless=None):
        """Apply settings from the GUI. Anything affecting where/whether
        smartlists live re-queues every list for a fresh reconcile."""

        section = config.sections["lexicon"]
        resync_needed = False

        for key, value in (
            ("sync_enabled", sync_enabled),
            ("api_url", api_url),
            ("parent_folder", parent_folder),
            ("auto_import", auto_import),
            ("dedupe_enabled", dedupe_enabled),
            ("dedupe_prefer_longer", dedupe_prefer_longer),
            ("dedupe_prefer_lossless", dedupe_prefer_lossless)
        ):
            if value is None or section[key] == value:
                continue

            section[key] = value

            if key in ("sync_enabled", "api_url", "parent_folder"):
                resync_needed = True

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

    # Syncing #

    def _poll(self):

        if not self.enabled:
            return

        if self._sync_thread is not None and self._sync_thread.is_alive():
            return

        from pynicotine.core import core

        if core.download_lists is None:
            return

        with self._lock:
            pending_lists = set(self._pending_lists)
            has_pending_files = bool(self._pending_files)

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

        section = config.sections["lexicon"]
        client = LexiconClient(section["api_url"])

        try:
            synced = sync_smartlists(client, jobs, self._playlist_ids, section["parent_folder"])

        except LexiconAPIError as error:
            # Most commonly: Lexicon isn't running. Everything stays pending,
            # the next poll retries.
            log.add_debug("Lexicon: sync pass failed, will retry: %s", error)
            return

        with self._lock:
            self._pending_lists.difference_update(synced)
            pending_files = list(self._pending_files)

        outcomes = process_pending_files(
            client, pending_files,
            dedupe_enabled=section["dedupe_enabled"],
            prefer_longer=section["dedupe_prefer_longer"],
            prefer_lossless=section["dedupe_prefer_lossless"]
        )

        with self._lock:
            # Keep anything that arrived while this pass ran
            newly_queued = [entry for entry in self._pending_files if entry not in pending_files]
            self._pending_files = pending_files + newly_queued

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
        synced = sync_smartlists(client, jobs, playlist_ids, section["parent_folder"])

    except LexiconAPIError as error:
        print(f"Lexicon is not reachable ({error}), nothing synced")
        return 1

    outcomes = process_pending_files(
        client, pending_files,
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
