# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Watches a Spotify playlist and adds newly added tracks to a wishlist,
the same way Watch Folder adds songs from exported .txt files -- just
sourced from a Spotify playlist instead.

Requires the user's own Spotify Developer app (client ID/secret from
https://developer.spotify.com/dashboard) -- there's no way around that,
since only the user can create that app and log into their own Spotify
account. Reading a private or collaborative playlist additionally requires
a one-time OAuth login (see begin_authorization): the user's browser opens
Spotify's own login/consent page (Nicotine+ never sees their Spotify
password), and a short-lived local HTTP server catches the resulting
redirect so the login flow can complete without a browser extension or
manual copy-pasting."""

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

        if self.is_watch_enabled():
            self._poll_timer_id = events.schedule(
                delay=self.POLL_INTERVAL, callback=self._poll_playlist, repeat=True)

            # Also check shortly after startup, rather than waiting a full POLL_INTERVAL
            events.schedule(delay=5, callback=self._poll_playlist)

    def _quit(self):
        events.cancel_scheduled(self._poll_timer_id)
        self._shutdown_pending_server()

    # Credentials / settings #

    @staticmethod
    def has_credentials():
        section = config.sections["spotify"]
        return bool(section["client_id"] and section["client_secret"])

    @staticmethod
    def is_authorized():
        return bool(config.sections["spotify"]["refresh_token"])

    @staticmethod
    def is_watch_enabled():
        section = config.sections["spotify"]
        return bool(section["watch_enabled"] and section["watch_playlist_id"])

    def update_credentials(self, client_id, client_secret):
        config.sections["spotify"]["client_id"] = client_id.strip()
        config.sections["spotify"]["client_secret"] = client_secret.strip()
        config.write_configuration()

    def update_watch_settings(self, enabled, playlist_url_or_id, ignore_radio_edit):
        """enabled/ignore_radio_edit are plain booleans; playlist_url_or_id
        accepts a bare playlist ID, a spotify:playlist:<id> URI, or an
        open.spotify.com/playlist/<id> URL."""

        previous_playlist_id = config.sections["spotify"]["watch_playlist_id"]
        playlist_id = self._extract_playlist_id(playlist_url_or_id) or ""

        config.sections["spotify"]["watch_enabled"] = bool(enabled)
        config.sections["spotify"]["watch_playlist_id"] = playlist_id
        config.sections["spotify"]["watch_ignore_radio_edit"] = bool(ignore_radio_edit)

        if playlist_id != previous_playlist_id:
            # Watching a different playlist now -- forget what we'd already imported
            # from whatever was watched before, so the new one starts from scratch
            config.sections["spotify"]["watch_seen_track_ids"] = []

        config.write_configuration()

        events.cancel_scheduled(self._poll_timer_id)
        self._poll_timer_id = None

        if self.is_watch_enabled():
            self._poll_timer_id = events.schedule(
                delay=self.POLL_INTERVAL, callback=self._poll_playlist, repeat=True)
            events.schedule(delay=1, callback=self._poll_playlist)

    @staticmethod
    def _extract_playlist_id(playlist_url_or_id):

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

        except (urllib.error.URLError, OSError, ValueError) as error:
            events.invoke_main_thread(
                result_callback, False, _("Couldn't complete Spotify login: %s") % error)
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

    def _token_request(self, data):

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

        with urllib.request.urlopen(request, timeout=15) as handle:  # noqa: S310 (fixed https:// URL above)
            return json.loads(handle.read().decode("utf-8"))

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

        except (urllib.error.URLError, OSError, ValueError) as error:
            log.add(_("Spotify: couldn't refresh access token: %s"), error)
            return None

        self._access_token = response.get("access_token")
        self._access_token_expires_at = time.time() + response.get("expires_in", 3600) - 30

        # Spotify occasionally rotates the refresh token itself when refreshing
        if response.get("refresh_token"):
            config.sections["spotify"]["refresh_token"] = response["refresh_token"]
            config.write_configuration()

        return self._access_token

    def _api_get(self, path, params=None):

        access_token = self._ensure_access_token()

        if access_token is None:
            return None

        url = path if path.startswith("http") else self.API_BASE_URL + path

        if params:
            url += "?" + urllib.parse.urlencode(params)

        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})

        try:
            with urllib.request.urlopen(request, timeout=15) as handle:  # noqa: S310 (fixed https:// URL above)
                return json.loads(handle.read().decode("utf-8"))

        except (urllib.error.URLError, OSError, ValueError) as error:
            log.add(_("Spotify: request to %(path)s failed: %(error)s"), {"path": path, "error": error})
            return None

    # Playlist polling #

    def _poll_playlist(self):

        if not self.is_watch_enabled() or not self.is_authorized():
            return

        playlist_id = config.sections["spotify"]["watch_playlist_id"]
        playlist = self._api_get(f"/playlists/{playlist_id}", params={"fields": "name"})

        if playlist is None:
            return

        list_name = playlist.get("name") or playlist_id
        seen_track_ids = set(config.sections["spotify"]["watch_seen_track_ids"])
        new_terms = []
        current_track_ids = []

        path = f"/playlists/{playlist_id}/tracks"
        params = {"fields": "items(track(id,name,artists(name))),next", "limit": 100}

        while path is not None:
            page = self._api_get(path, params=params)

            if page is None:
                break

            for entry in page.get("items", []):
                track = entry.get("track")

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

        config.sections["spotify"]["watch_seen_track_ids"] = current_track_ids
        config.write_configuration()

        if not new_terms:
            return

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
