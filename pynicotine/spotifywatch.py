# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Watches one or more Spotify playlists and adds newly added tracks to a
wishlist each, the same way Watch Folder adds songs from exported .txt
files -- just sourced from Spotify playlists instead.

Requires the user's own Spotify Developer app (client ID/secret from
https://developer.spotify.com/dashboard) -- there's no way around that,
since only the user can create that app and log into their own Spotify
account. Reading a private or collaborative playlist additionally requires
a one-time OAuth login (see begin_authorization): the user's browser opens
Spotify's own login/consent page (Nicotine+ never sees their Spotify
password), and a short-lived local HTTP server catches the resulting
redirect so the login flow can complete without a browser extension or
manual copy-pasting. Once connected, any playlist can be watched -- the
user's own (browsable via fetch_own_playlists) or someone else's, by URL."""

import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer

from pynicotine.config import config
from pynicotine.events import events
from pynicotine.logfacility import log


class _AuthorizationCallbackHandler(BaseHTTPRequestHandler):
    """Handles exactly one GET request: the redirect Spotify sends back to
    our temporary local server after the user logs in and approves access
    in their own browser."""

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler's own naming convention)

        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        self.server.auth_code = query.get("code", [None])[0]
        self.server.auth_state = query.get("state", [None])[0]
        self.server.auth_error = query.get("error", [None])[0]

        if self.server.auth_error:
            body = _("Spotify authorization failed: %s. You can close this window.") % self.server.auth_error
        else:
            body = _("Spotify authorization complete. You can close this window and return to Nicotine+.")

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, _format, *_args):
        """Silence BaseHTTPRequestHandler's default per-request console log."""


class SpotifyAPIError(Exception):
    """A Spotify API request failed. message is already formatted for
    display; status is the HTTP status code, or None for a connection-level
    failure (DNS, timeout, offline, ...) that never got a response at all."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.message = message
        self.status = status


class SpotifyWatch:

    AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API_BASE_URL = "https://api.spotify.com/v1"
    SCOPES = "playlist-read-private playlist-read-collaborative"

    # Must be added to the Spotify app's own "Redirect URIs" setting
    # (developer.spotify.com/dashboard) for authorization to work at all
    REDIRECT_PORT = 8888
    REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"

    AUTH_CALLBACK_TIMEOUT = 120  # seconds to wait for the user to finish logging in
    POLL_INTERVAL = 120  # seconds between playlist checks

    # Qualifiers meaning "shortened for radio play", e.g. "Song (Radio Edit)" or
    # "Song - Radio Edit" -- stripped from the search term when watch_ignore_radio_edit
    # is enabled, so normal matching (and the "prefer longer" download setting) finds
    # the Extended/Original version instead of specifically requiring the short one
    RADIO_EDIT_PATTERN = re.compile(
        r"\s*[(\[]\s*radio\s*(?:edit|mix|version)?\s*[)\]]|\s*-\s*radio\s*(?:edit|mix|version)\b",
        re.IGNORECASE
    )

    __slots__ = ("_poll_timer_id", "_access_token", "_access_token_expires_at", "_pending_server")

    def __init__(self):

        self._poll_timer_id = None
        self._access_token = None
        self._access_token_expires_at = 0
        self._pending_server = None

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
        self._shutdown_pending_server()

    def _ensure_polling(self):
        """(Re)start the periodic poll timer if there's now at least one
        watched playlist and it isn't already running; called after any
        change that could take the watched list from empty to non-empty."""

        if self._poll_timer_id is not None or not self.has_watched_playlists():
            return

        self._poll_timer_id = events.schedule(
            delay=self.POLL_INTERVAL, callback=self._poll_playlists, repeat=True)

    # Credentials #

    @staticmethod
    def has_credentials():
        section = config.sections["spotify"]
        return bool(section["client_id"] and section["client_secret"])

    @staticmethod
    def is_authorized():
        return bool(config.sections["spotify"]["refresh_token"])

    def update_credentials(self, client_id, client_secret):
        config.sections["spotify"]["client_id"] = client_id.strip()
        config.sections["spotify"]["client_secret"] = client_secret.strip()
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

    def update_ignore_radio_edit(self, ignore_radio_edit):
        config.sections["spotify"]["watch_ignore_radio_edit"] = bool(ignore_radio_edit)
        config.write_configuration()

    def remove_watched_playlist(self, playlist_id):

        watched = config.sections["spotify"]["watched_playlists"]
        new_watched = [entry for entry in watched if entry["playlist_id"] != playlist_id]

        if len(new_watched) == len(watched):
            return

        config.sections["spotify"]["watched_playlists"] = new_watched
        config.write_configuration()

        if not new_watched:
            events.cancel_scheduled(self._poll_timer_id)
            self._poll_timer_id = None

    def add_watched_playlist(self, playlist_url_or_id, result_callback):
        """Starts watching a playlist -- the user's own, or anyone else's, as
        long as it's readable with the scopes this app requested. Creates a
        wishlist named after the playlist (if one by that name doesn't
        already exist) and does an immediate poll to import its current
        contents. result_callback(success, message_or_list_name) is invoked
        on the main thread once the playlist's name has been looked up (or
        the attempt has failed) -- safe to update GTK widgets from directly."""

        if not self.is_authorized():
            result_callback(False, _("Connect to Spotify first."))
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
            playlist = self._api_get(f"/playlists/{playlist_id}", params={"fields": "name"})

        except SpotifyAPIError as error:
            events.invoke_main_thread(result_callback, False, error.message)
            return

        list_name = playlist.get("name") or playlist_id

        entry = {
            "playlist_id": playlist_id,
            "list_name": list_name,
            "seen_track_ids": []
        }
        config.sections["spotify"]["watched_playlists"].append(entry)
        config.write_configuration()

        events.invoke_main_thread(self._ensure_polling)
        events.invoke_main_thread(result_callback, True, list_name)

        # Import whatever's already in the playlist right away, rather than
        # waiting up to POLL_INTERVAL for the first check. We're already on
        # a background thread here, so call the network-bound method
        # directly instead of _poll_playlists (which would spawn another one)
        self._poll_single_playlist(entry)

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

    # Browsing the user's own playlists #

    def fetch_own_playlists(self, result_callback):
        """Fetches every playlist in the connected account's own library
        (owned or followed), for a picker UI to choose from -- a lower-
        friction alternative to pasting a URL for the common case of
        watching one of the user's own playlists. result_callback(playlists,
        error_message) is invoked on the main thread; exactly one of the two
        arguments is None. playlists is [{"id", "name", "owner"}, ...]."""

        if not self.is_authorized():
            result_callback(None, _("Connect to Spotify first."))
            return

        thread = threading.Thread(target=self._fetch_own_playlists_thread, args=(result_callback,), daemon=True)
        thread.start()

    def _fetch_own_playlists_thread(self, result_callback):

        playlists = []
        path = "/me/playlists"
        params = {"limit": 50}

        try:
            while path is not None:
                page = self._api_get(path, params=params)

                for item in page.get("items", []):
                    if not item or not item.get("id"):
                        continue

                    owner = (item.get("owner") or {}).get("display_name") or ""
                    playlists.append({"id": item["id"], "name": item.get("name") or item["id"], "owner": owner})

                path = page.get("next")
                params = None

        except SpotifyAPIError as error:
            events.invoke_main_thread(result_callback, None, error.message)
            return

        playlists.sort(key=lambda playlist: playlist["name"].lower())
        events.invoke_main_thread(result_callback, playlists, None)

    # OAuth #

    def begin_authorization(self, result_callback):
        """Opens the user's browser to Spotify's own login/consent page, and
        starts a short-lived local HTTP server to catch the redirect
        afterwards. result_callback(success, message) is always invoked back
        on the main thread (see events.invoke_main_thread) once the flow
        finishes, times out, or fails -- safe to update GTK widgets from
        directly."""

        if not self.has_credentials():
            result_callback(False, _("Enter a Client ID and Client Secret first."))
            return

        state = format(int(time.time() * 1000), "x")
        authorize_url = self.AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": config.sections["spotify"]["client_id"],
            "response_type": "code",
            "redirect_uri": self.REDIRECT_URI,
            "scope": self.SCOPES,
            "state": state
        })

        try:
            server = HTTPServer(("127.0.0.1", self.REDIRECT_PORT), _AuthorizationCallbackHandler)

        except OSError as error:
            result_callback(False, _("Couldn't start local server on port %(port)s: %(error)s") % {
                "port": self.REDIRECT_PORT, "error": error
            })
            return

        server.auth_code = None
        server.auth_state = None
        server.auth_error = None
        server.timeout = self.AUTH_CALLBACK_TIMEOUT
        self._shutdown_pending_server()  # Supersede any still-pending previous attempt
        self._pending_server = server

        webbrowser.open(authorize_url)

        thread = threading.Thread(
            target=self._wait_for_authorization, args=(server, state, result_callback), daemon=True)
        thread.start()

    def _wait_for_authorization(self, server, expected_state, result_callback):

        try:
            # Blocks this (background) thread until the redirect request arrives, or times out
            server.handle_request()

            if self._pending_server is not server:
                return  # Superseded by a newer attempt; that one owns the result callback now

            if server.auth_error:
                message = _("Spotify denied access: %s") % server.auth_error
                events.invoke_main_thread(result_callback, False, message)
                return

            if not server.auth_code:
                events.invoke_main_thread(
                    result_callback, False, _("Timed out waiting for Spotify login. Please try again."))
                return

            if server.auth_state != expected_state:
                events.invoke_main_thread(
                    result_callback, False,
                    _("Authorization response didn't match this request. Please try again."))
                return

            self._exchange_code_for_tokens(server.auth_code, result_callback)

        finally:
            self._shutdown_pending_server(server)

    def _shutdown_pending_server(self, server=None):

        target = server or self._pending_server

        if target is None:
            return

        try:
            target.server_close()

        except OSError:
            pass

        if self._pending_server is target:
            self._pending_server = None

    def _exchange_code_for_tokens(self, code, result_callback):

        data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.REDIRECT_URI
        }).encode("utf-8")

        try:
            response = self._token_request(data)

        except SpotifyAPIError as error:
            events.invoke_main_thread(
                result_callback, False, _("Couldn't complete Spotify login: %s") % error.message)
            return

        refresh_token = response.get("refresh_token")

        if not refresh_token:
            events.invoke_main_thread(result_callback, False, _("Spotify didn't return a refresh token."))
            return

        config.sections["spotify"]["refresh_token"] = refresh_token
        config.write_configuration()

        self._access_token = response.get("access_token")
        self._access_token_expires_at = time.time() + response.get("expires_in", 3600) - 30

        events.invoke_main_thread(result_callback, True, _("Connected to Spotify."))
        events.invoke_main_thread(self._ensure_polling)

    def _token_request(self, data):
        """Raises SpotifyAPIError on any failure -- never returns None, so
        callers don't need a None-check on top of the except clause."""

        client_id = config.sections["spotify"]["client_id"]
        client_secret = config.sections["spotify"]["client_secret"]
        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")

        request = urllib.request.Request(
            self.TOKEN_URL, data=data, method="POST",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )

        return self._send_request(request)

    def _ensure_access_token(self):

        if self._access_token and time.time() < self._access_token_expires_at:
            return self._access_token

        refresh_token = config.sections["spotify"]["refresh_token"]

        if not refresh_token or not self.has_credentials():
            return None

        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }).encode("utf-8")

        try:
            response = self._token_request(data)

        except SpotifyAPIError as error:
            log.add(_("Spotify: couldn't refresh access token: %s"), error.message)
            return None

        self._access_token = response.get("access_token")
        self._access_token_expires_at = time.time() + response.get("expires_in", 3600) - 30

        # Spotify occasionally rotates the refresh token itself when refreshing
        if response.get("refresh_token"):
            config.sections["spotify"]["refresh_token"] = response["refresh_token"]
            config.write_configuration()

        return self._access_token

    @staticmethod
    def _send_request(request):
        """Performs an HTTP request and returns the parsed JSON body, or
        raises SpotifyAPIError with a message that includes Spotify's own
        explanation (its error responses are JSON: {"error": {"status",
        "message"}}), not just the bare HTTP status -- needed to tell apart
        e.g. a bad/expired token from a playlist Spotify's API restricts
        third-party apps from reading at all, which are both plain 403s
        otherwise."""

        try:
            with urllib.request.urlopen(request, timeout=15) as handle:  # noqa: S310 (fixed https:// URLs only)
                return json.loads(handle.read().decode("utf-8"))

        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            detail = body

            try:
                parsed = json.loads(body)
                detail = (
                    parsed.get("error", {}).get("message")
                    or parsed.get("error_description")
                    or parsed.get("error")
                    or body
                )
            except (ValueError, AttributeError):
                pass

            log.add(_("Spotify: request failed (%(status)s): %(detail)s"), {"status": error.code, "detail": body})
            raise SpotifyAPIError(
                _("Spotify error %(status)s: %(detail)s") % {"status": error.code, "detail": detail},
                status=error.code
            ) from error

        except (urllib.error.URLError, OSError, ValueError) as error:
            raise SpotifyAPIError(_("Couldn't reach Spotify: %s") % error) from error

    def _api_get(self, path, params=None):
        """Raises SpotifyAPIError on any failure, including "not logged in"
        (no access token available) -- callers that loop over multiple
        playlists catch this per-playlist so one failure doesn't abort the
        rest; callers doing a single lookup let it propagate."""

        access_token = self._ensure_access_token()

        if access_token is None:
            raise SpotifyAPIError(_("Not connected to Spotify."))

        url = path if path.startswith("http") else self.API_BASE_URL + path

        if params:
            url += "?" + urllib.parse.urlencode(params)

        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        return self._send_request(request)

    # Playlist polling #

    def _poll_playlists(self):
        """Scheduled (see _start/_ensure_polling) callbacks always run on the
        main thread (events.schedule -> invoke_main_thread), but the actual
        polling below makes blocking network calls -- doing that here would
        freeze the whole UI for however long Spotify takes to respond, once
        per playlist, every POLL_INTERVAL. Hand the real work off to a
        background thread instead; _poll_single_playlist marshals back to
        the main thread only for the one part that actually needs it
        (updating a wishlist, which touches GTK via events)."""

        if not self.is_authorized():
            return

        thread = threading.Thread(target=self._poll_playlists_thread, daemon=True)
        thread.start()

    def _poll_playlists_thread(self):
        for entry in list(config.sections["spotify"]["watched_playlists"]):
            self._poll_single_playlist(entry)

    def _poll_single_playlist(self, entry):
        """Runs on a background thread (see _poll_playlists/
        _add_watched_playlist_thread) -- must not touch GTK directly."""

        playlist_id = entry["playlist_id"]
        list_name = entry["list_name"]
        seen_track_ids = set(entry.get("seen_track_ids", []))
        new_terms = []
        current_track_ids = []

        # "/playlists/{id}/tracks" was Spotify's endpoint for this until their
        # March 2026 Web API migration, which retired it in favor of
        # "/playlists/{id}/items" (same shape, but /tracks now returns a flat
        # 403 for every playlist, including your own, on Development Mode
        # apps -- see https://developer.spotify.com/documentation/web-api/reference/get-playlists-items)
        path = f"/playlists/{playlist_id}/items"
        params = {"fields": "items(track(id,name,artists(name))),next", "limit": 50}

        try:
            while path is not None:
                page = self._api_get(path, params=params)

                for item in page.get("items", []):
                    track = item.get("track")

                    if not track or not track.get("id"):
                        continue

                    current_track_ids.append(track["id"])

                    if track["id"] in seen_track_ids:
                        continue

                    term = self._build_search_term(track)

                    if term:
                        new_terms.append(term)

                # "next" is already a complete URL for the following page, or None if done
                path = page.get("next")
                params = None

        except SpotifyAPIError as error:
            log.add(_('Spotify: checking playlist "%(playlist)s" failed: %(error)s'), {
                "playlist": list_name, "error": error.message
            })
            return

        entry["seen_track_ids"] = current_track_ids
        config.write_configuration()

        if not new_terms:
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

        artist_names = ", ".join(artist["name"] for artist in track.get("artists", []) if artist.get("name"))
        title = track.get("name") or ""

        if not artist_names or not title:
            return None

        if config.sections["spotify"]["watch_ignore_radio_edit"]:
            title = self.RADIO_EDIT_PATTERN.sub("", title).strip()

        return f"{title} - {artist_names}"
