# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Watches one or more Spotify playlists and adds newly added tracks to a
wishlist each, the same way Watch Folder adds songs from exported .txt
files -- just sourced from Spotify playlists instead.

Uses the third-party "spotifyscraper" package (optional dependency --
`pip install nicotine-plus[spotify]`) to read PUBLIC playlist data
anonymously, the same way Spotify's own web player embed widgets do --
no Spotify Developer app, no Client ID/Secret, no OAuth login, and no
Spotify account of any kind required. This deliberately trades away two
things the official Web API would otherwise offer: reading a PRIVATE
playlist (not possible without logging in as its owner), and any
guarantee of stability (spotifyscraper reads endpoints Spotify hasn't
published or versioned for third-party use, and can break without
warning whenever Spotify changes something internal -- see
https://github.com/AliAkhtari78/SpotifyScraper). In exchange, it sidesteps
entirely the official API's Development Mode restriction that blocks
reading the contents of any playlist the connected account doesn't own or
collaborate on -- confirmed live, the exact playlists that 403'd (or hung)
through the official OAuth-based flow read back correctly and quickly
through this one instead."""

import re
import threading

from pynicotine.config import config
from pynicotine.events import events
from pynicotine.logfacility import log

try:
    from spotify_scraper import NotFoundError as SpotifyNotFoundError
    from spotify_scraper import SpotifyClient
    from spotify_scraper import SpotifyScraperError
    SPOTIFY_SCRAPER_AVAILABLE = True

except ImportError:
    SpotifyNotFoundError = None
    SpotifyClient = None
    SpotifyScraperError = None
    SPOTIFY_SCRAPER_AVAILABLE = False


class SpotifyWatch:

    POLL_INTERVAL = 120  # seconds between playlist checks
    REQUEST_TIMEOUT = 15  # seconds, per HTTP request spotifyscraper makes

    # Qualifiers meaning "shortened for radio play", e.g. "Song (Radio Edit)" or
    # "Song - Radio Edit" -- stripped from the search term when watch_ignore_radio_edit
    # is enabled, so normal matching (and the "prefer longer" download setting) finds
    # the Extended/Original version instead of specifically requiring the short one
    RADIO_EDIT_PATTERN = re.compile(
        r"\s*[(\[]\s*radio\s*(?:edit|mix|version)?\s*[)\]]|\s*-\s*radio\s*(?:edit|mix|version)\b",
        re.IGNORECASE
    )

    __slots__ = ("_poll_timer_id",)

    def __init__(self):

        self._poll_timer_id = None

        for event_name, callback in (
            ("quit", self._quit),
            ("start", self._start)
        ):
            events.connect(event_name, callback)

    # Lifecycle #

    def _start(self):

        if self.has_watched_playlists():
            self._poll_timer_id = events.schedule(
                delay=self.POLL_INTERVAL, callback=self._poll_playlists, repeat=True)

            # Also check shortly after startup, rather than waiting a full POLL_INTERVAL
            events.schedule(delay=5, callback=self._poll_playlists)

    def _quit(self):
        events.cancel_scheduled(self._poll_timer_id)

    def _ensure_polling(self):
        """(Re)start the periodic poll timer if there's now at least one
        watched playlist and it isn't already running; called after any
        change that could take the watched list from empty to non-empty."""

        if self._poll_timer_id is not None or not self.has_watched_playlists():
            return

        self._poll_timer_id = events.schedule(
            delay=self.POLL_INTERVAL, callback=self._poll_playlists, repeat=True)

    @staticmethod
    def unavailable_message():
        return _(
            "The optional \"spotifyscraper\" package isn't installed -- Spotify Watch needs it "
            "to read playlist data. Install it with: pip install spotifyscraper"
        )

    def update_ignore_radio_edit(self, ignore_radio_edit):
        config.sections["spotify"]["watch_ignore_radio_edit"] = bool(ignore_radio_edit)
        config.write_configuration()

    # Watched playlists #

    @staticmethod
    def has_watched_playlists():
        return bool(config.sections["spotify"]["watched_playlists"])

    @staticmethod
    def get_watched_playlists():
        """A read-only-in-spirit snapshot -- [{"playlist_id", "list_name"}, ...],
        in the order they were added. Callers should treat this as a copy;
        use add_watched_playlist/remove_watched_playlist to make changes."""

        return [
            {"playlist_id": entry["playlist_id"], "list_name": entry["list_name"]}
            for entry in config.sections["spotify"]["watched_playlists"]
        ]

    def remove_watched_playlist(self, playlist_id):

        watched = config.sections["spotify"]["watched_playlists"]
        removed_entry = next((entry for entry in watched if entry["playlist_id"] == playlist_id), None)

        if removed_entry is None:
            return

        new_watched = [entry for entry in watched if entry["playlist_id"] != playlist_id]
        config.sections["spotify"]["watched_playlists"] = new_watched
        config.write_configuration()

        if not new_watched:
            events.cancel_scheduled(self._poll_timer_id)
            self._poll_timer_id = None

        # The wishlist itself isn't touched -- unwatching just stops it being
        # auto-imported into. Tell the GUI to re-check this list's row, so a
        # sidebar that categorizes by watched-playlist status (Folders vs.
        # Spotify Playlists) moves it back out of the Spotify category.
        events.emit("update-download-list", removed_entry["list_name"])

    def add_watched_playlist(self, playlist_url_or_id, result_callback):
        """Starts watching a playlist -- public, anyone's, by URL or ID; no
        login of any kind required (see this module's docstring for why
        that also means a PRIVATE playlist can't be read). Creates a
        wishlist named after the playlist (if one by that name doesn't
        already exist) and does an immediate poll to import its current
        contents. result_callback(success, message_or_list_name) is invoked
        on the main thread once the playlist's name has been looked up (or
        the attempt has failed) -- safe to update GTK widgets from directly."""

        if not SPOTIFY_SCRAPER_AVAILABLE:
            result_callback(False, self.unavailable_message())
            return

        playlist_id = self._extract_playlist_id(playlist_url_or_id)

        if not playlist_id:
            result_callback(False, _("That doesn't look like a Spotify playlist URL or ID."))
            return

        if any(entry["playlist_id"] == playlist_id
               for entry in config.sections["spotify"]["watched_playlists"]):
            result_callback(False, _("Already watching this playlist."))
            return

        thread = threading.Thread(
            target=self._add_watched_playlist_thread, args=(playlist_id, result_callback), daemon=True)
        thread.start()

    def _add_watched_playlist_thread(self, playlist_id, result_callback):

        try:
            with SpotifyClient(timeout=self.REQUEST_TIMEOUT) as client:
                playlist = client.get_playlist(playlist_id, max_tracks=1)

        except SpotifyNotFoundError:
            events.invoke_main_thread(
                result_callback, False,
                _("Couldn't find that playlist -- double check the link, and that it's public "
                  "(a private playlist can't be read without logging in as its owner)."))
            return

        except SpotifyScraperError as error:
            log.add(_('Spotify: looking up playlist "%(id)s" failed: %(error)s'), {
                "id": playlist_id, "error": error
            })
            events.invoke_main_thread(result_callback, False, _("Couldn't reach Spotify: %s") % error)
            return

        list_name = self._unique_list_name((playlist.name or playlist_id).strip())

        entry = {
            "playlist_id": playlist_id,
            "list_name": list_name,
            "seen_track_ids": []
        }
        config.sections["spotify"]["watched_playlists"].append(entry)
        config.write_configuration()

        events.invoke_main_thread(self._ensure_polling)

        # Create the wishlist immediately, even if this playlist turns out
        # to have zero importable tracks right now -- without this, watching
        # an empty (or momentarily all-already-seen) playlist gives no
        # visible confirmation at all that anything happened
        events.invoke_main_thread(self._ensure_list_exists, list_name)
        events.invoke_main_thread(result_callback, True, list_name)

        # In case this list already existed (e.g. re-watching one that was
        # previously unwatched) -- if it's brand new, add-download-list /
        # update-download-list-item below (via _poll_single_playlist) covers
        # it instead, but a re-watch with nothing new to import wouldn't
        # otherwise tell the GUI its category just changed back to Spotify
        events.invoke_main_thread(events.emit, "update-download-list", list_name)

        # Import whatever's already in the playlist right away, rather than
        # waiting up to POLL_INTERVAL for the first check. We're already on
        # a background thread here, so call the network-bound method
        # directly instead of _poll_playlists (which would spawn another one)
        self._poll_single_playlist(entry)

    @staticmethod
    def _ensure_list_exists(list_name):
        """Create the wishlist if it doesn't already exist -- called right
        after successfully starting to watch a playlist (see
        _add_watched_playlist_thread), separately from _apply_new_tracks,
        so the list shows up immediately even for a playlist with nothing
        currently importable."""

        from pynicotine.core import core

        if list_name not in core.download_lists.lists:
            core.download_lists.add_list(list_name)

    @staticmethod
    def _unique_list_name(base_name):
        """Avoid silently merging a newly watched playlist into an unrelated
        pre-existing list that just happens to share its name -- each
        watched playlist gets its own list, disambiguated with a numbered
        suffix if the plain name is already taken by anything else."""

        from pynicotine.core import core

        existing_lists = core.download_lists.lists if core.download_lists is not None else {}
        name = base_name
        suffix = 2

        while name in existing_lists:
            name = f"{base_name} ({suffix})"
            suffix += 1

        return name

    @staticmethod
    def _extract_playlist_id(playlist_url_or_id):
        """Accepts a bare playlist ID, a spotify:playlist:<id> URI, or an
        open.spotify.com/playlist/<id> URL, and returns just the ID."""

        text = (playlist_url_or_id or "").strip()

        if not text:
            return None

        if text.startswith("spotify:playlist:"):
            return text.rsplit(":", maxsplit=1)[-1]

        match = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", text)

        if match:
            return match.group(1)

        if re.fullmatch(r"[A-Za-z0-9]+", text):
            return text

        return None

    # Searching Spotify for a playlist #

    def search_playlists(self, query, result_callback):
        """Anonymous, public search across all of Spotify (not just any
        particular account's library) -- the picker UI's alternative to
        pasting a URL directly, for finding a playlist by name. No login
        required, same as everything else in this module.
        result_callback(playlists, error_message) is invoked on the main
        thread; exactly one of the two arguments is None. playlists is
        [{"id", "name", "owner"}, ...]."""

        if not SPOTIFY_SCRAPER_AVAILABLE:
            result_callback(None, self.unavailable_message())
            return

        query = query.strip()

        if not query:
            result_callback([], None)
            return

        thread = threading.Thread(target=self._search_playlists_thread, args=(query, result_callback), daemon=True)
        thread.start()

    def _search_playlists_thread(self, query, result_callback):

        try:
            with SpotifyClient(timeout=self.REQUEST_TIMEOUT) as client:
                results = client.search(query, types=("playlist",), limit=20)

        except SpotifyScraperError as error:
            log.add(_('Spotify: searching for "%(query)s" failed: %(error)s'), {"query": query, "error": error})
            events.invoke_main_thread(result_callback, None, _("Couldn't reach Spotify: %s") % error)
            return

        playlists = [
            {
                "id": playlist.id,
                "name": playlist.name or playlist.id,
                "owner": playlist.owner.name if playlist.owner else ""
            }
            for playlist in results.playlists
        ]
        events.invoke_main_thread(result_callback, playlists, None)

    # Playlist polling #

    def _poll_playlists(self):
        """Scheduled (see _start/_ensure_polling) callbacks always run on the
        main thread (events.schedule -> invoke_main_thread), but the actual
        polling below makes blocking network calls -- doing that here would
        freeze the whole UI for however long Spotify takes to respond, once
        per playlist, every POLL_INTERVAL. Hand the real work off to
        background threads instead; _poll_single_playlist marshals back to
        the main thread only for the one part that actually needs it
        (updating a wishlist, which touches GTK via events)."""

        if not SPOTIFY_SCRAPER_AVAILABLE:
            return

        thread = threading.Thread(target=self._poll_playlists_thread, daemon=True)
        thread.start()

    def _poll_playlists_thread(self):
        """Dispatches one independent thread per watched playlist, rather
        than checking them one after another on this single thread -- a
        single playlist's request hanging or being unusually slow must
        never be able to starve every other watched playlist of ever being
        checked again. Each playlist's poll is already fully self-contained
        (_poll_single_playlist only touches its own entry dict), so running
        them concurrently is safe."""

        for entry in list(config.sections["spotify"]["watched_playlists"]):
            threading.Thread(target=self._poll_single_playlist, args=(entry,), daemon=True).start()

    def _poll_single_playlist(self, entry):
        """Runs on a background thread (see _poll_playlists/
        _add_watched_playlist_thread) -- must not touch GTK directly."""

        playlist_id = entry["playlist_id"]
        list_name = entry["list_name"]
        seen_track_ids = set(entry.get("seen_track_ids", []))

        try:
            with SpotifyClient(timeout=self.REQUEST_TIMEOUT) as client:
                playlist = client.get_playlist(playlist_id, max_tracks=None)

        except SpotifyScraperError as error:
            log.add(_('Spotify: checking playlist "%(playlist)s" failed: %(error)s'), {
                "playlist": list_name, "error": error
            })
            return

        new_terms = []
        current_track_ids = []

        for playlist_track in playlist.tracks:
            track = playlist_track.track

            if not track or not track.id:
                continue

            current_track_ids.append(track.id)

            if track.id in seen_track_ids:
                continue

            term = self._build_search_term(track)

            if term:
                new_terms.append(term)

        entry["seen_track_ids"] = current_track_ids
        config.write_configuration()

        if not new_terms:
            # Not an error -- the request succeeded, there's just nothing
            # new to import right now (an empty playlist, or every track
            # already seen). Debug-level only so a normal, working playlist
            # doesn't spam the log every POLL_INTERVAL; the list itself
            # already exists (see _ensure_list_exists) so there's no
            # visibility gap for the user even when this says nothing
            log.add_debug(
                'Spotify: checked playlist "%(playlist)s" -- %(count)s track(s), nothing new to import',
                {"playlist": list_name, "count": len(current_track_ids)}
            )
            return

        events.invoke_main_thread(self._apply_new_tracks, list_name, new_terms)

    @staticmethod
    def _apply_new_tracks(list_name, new_terms):
        """The one part of polling that must run on the main thread: adding
        to (and possibly creating) a wishlist goes through core.download_lists,
        which emits events the GUI listens to and updates widgets from."""

        from pynicotine.core import core

        if list_name not in core.download_lists.lists:
            core.download_lists.add_list(list_name)

        core.download_lists.add_list_items(list_name, new_terms)
        log.add(_('Spotify: added %(num)s new track(s) from "%(playlist)s" to the wishlist'), {
            "num": len(new_terms), "playlist": list_name
        })

    def _build_search_term(self, track):

        artist_names = ", ".join(artist.name for artist in track.artists if artist.name)
        title = track.name or ""

        if not artist_names or not title:
            return None

        if config.sections["spotify"]["watch_ignore_radio_edit"]:
            title = self.RADIO_EDIT_PATTERN.sub("", title).strip()

        return f"{title} - {artist_names}"
