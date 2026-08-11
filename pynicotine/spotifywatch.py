# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Watch Spotify playlists and feed new tracks into download lists.

Spotify offers no webhooks or websockets, so watched playlists are polled on a
timer. The playlist snapshot_id is not used to detect changes: it is documented
as lagging behind the actual track list, and is reported to sometimes not update
at all, so every poll compares the playlist's track IDs against the IDs already
seen for that playlist.

Authorization uses the OAuth 2.0 Authorization Code flow with PKCE, which is the
flow Spotify documents for applications that cannot keep a client secret. The
user supplies the client ID of an application they registered themselves; the
authorization step opens their browser and a short-lived loopback HTTP server
receives the redirect.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time

from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.logfacility import log
from pynicotine.utils import encode_path
from pynicotine.utils import load_file
from pynicotine.utils import write_file_and_backup


class WatchedPlaylist:
    __slots__ = ("playlist_id", "list_name", "name", "seen_track_ids", "last_polled", "last_error")

    def __init__(self, playlist_id, list_name, name=None, seen_track_ids=None, last_polled=None,
                 last_error=None):

        self.playlist_id = playlist_id
        self.list_name = list_name
        self.name = name or list_name
        self.seen_track_ids = set(seen_track_ids or [])
        self.last_polled = last_polled
        self.last_error = last_error

    def as_dict(self):
        return {
            "playlist_id": self.playlist_id,
            "list_name": self.list_name,
            "name": self.name,
            "seen_track_ids": sorted(self.seen_track_ids),
            "last_polled": self.last_polled,
            "last_error": self.last_error
        }


class SpotifyWatch:
    __slots__ = ("playlists", "file_path", "_access_token", "_access_token_expiry", "_auth_state",
                 "_auth_verifier", "_auth_server", "_auth_thread", "_poll_thread", "_poll_timer_id",
                 "_allow_saving")

    FILE_BASENAME = "spotify_watch.json"

    AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API_BASE_URL = "https://api.spotify.com/v1"

    # Reading private playlists and collaborative playlists the user has access to.
    # Public playlists need no scope, but still require a user token.
    SCOPES = "playlist-read-private playlist-read-collaborative"

    # Maximum number of playlist items Spotify returns per request
    PAGE_LIMIT = 100

    # Only ask for the fields actually needed, to keep responses small
    ITEM_FIELDS = "next,items(is_local,track(id,name,type,artists(name)))"

    # Give up on the browser authorization step after this long
    AUTH_TIMEOUT = 300

    # Refresh the access token this many seconds before it actually expires
    TOKEN_EXPIRY_MARGIN = 60

    HTTP_TIMEOUT = 15

    def __init__(self):

        self.playlists = {}
        self.file_path = os.path.join(config.data_folder_path, self.FILE_BASENAME)

        self._access_token = None
        self._access_token_expiry = 0
        self._auth_state = None
        self._auth_verifier = None
        self._auth_server = None
        self._auth_thread = None
        self._poll_thread = None
        self._poll_timer_id = None
        self._allow_saving = False

        for event_name, callback in (
            ("quit", self._quit),
            ("spotify-poll-failed", self._poll_failed),
            ("spotify-poll-finished", self._apply_poll_result),
            ("spotify-poll-now", self.poll_now),
            ("start", self._start)
        ):
            events.connect(event_name, callback)

    def _start(self):

        self._load()
        self._allow_saving = True

        self._schedule_poll()

    def _quit(self):

        events.cancel_scheduled(self._poll_timer_id)
        self._stop_auth_server()

        self._save()
        self._allow_saving = False

    # Persistence #

    @staticmethod
    def _load_file(file_path):

        file_path = encode_path(file_path)

        if not os.path.isfile(file_path):
            return []

        with open(file_path, encoding="utf-8") as handle:
            return json.load(handle)

    def _load(self):

        playlists_data = load_file(self.file_path, self._load_file)

        for playlist_data in playlists_data:
            playlist_id = playlist_data.get("playlist_id")
            list_name = playlist_data.get("list_name")

            if not playlist_id or not list_name:
                continue

            self.playlists[playlist_id] = WatchedPlaylist(
                playlist_id=playlist_id,
                list_name=list_name,
                name=playlist_data.get("name"),
                seen_track_ids=playlist_data.get("seen_track_ids"),
                last_polled=playlist_data.get("last_polled"),
                last_error=playlist_data.get("last_error")
            )

    def _save_callback(self, file_handle):

        json_encoder = json.JSONEncoder(check_circular=False, ensure_ascii=False)
        is_first_item = True

        file_handle.write("[")

        for playlist in self.playlists.values():
            if is_first_item:
                is_first_item = False
            else:
                file_handle.write(",\n")

            file_handle.write(json_encoder.encode(playlist.as_dict()))

        file_handle.write("]")

    def _save(self):

        if not self._allow_saving:
            return

        config.create_data_folder()
        write_file_and_backup(self.file_path, self._save_callback)

    # Playlist Management #

    @staticmethod
    def parse_playlist_id(text):
        """Extract a playlist ID from a Spotify URL, URI or a bare ID."""

        text = (text or "").strip()

        if not text:
            return None

        if text.startswith("spotify:playlist:"):
            return text.rsplit(":", 1)[-1] or None

        if "://" in text:
            url = urlparse(text)

            if not url.netloc.endswith("spotify.com"):
                return None

            path_parts = [part for part in url.path.split("/") if part]

            if len(path_parts) < 2 or path_parts[-2] != "playlist":
                return None

            return path_parts[-1] or None

        # A bare playlist ID
        if text.isalnum():
            return text

        return None

    def add_playlist(self, text, list_name):
        """Start watching a playlist, creating its download list if needed."""

        playlist_id = self.parse_playlist_id(text)
        list_name = (list_name or "").strip()

        if not playlist_id or not list_name:
            return None

        if playlist_id in self.playlists:
            return None

        self.playlists[playlist_id] = playlist = WatchedPlaylist(
            playlist_id=playlist_id, list_name=list_name)

        if core.download_lists is not None and list_name not in core.download_lists.lists:
            core.download_lists.add_list(list_name)

        events.emit("add-spotify-playlist", playlist_id)
        self._save()

        self.poll_now(playlist_id)

        return playlist

    def remove_playlist(self, playlist_id):

        if self.playlists.pop(playlist_id, None) is None:
            return

        events.emit("remove-spotify-playlist", playlist_id)
        self._save()

    # Authorization #

    @property
    def is_authorized(self):
        return bool(config.sections["spotify"]["refreshtoken"])

    def sign_out(self):

        config.sections["spotify"]["refreshtoken"] = ""
        self._access_token = None
        self._access_token_expiry = 0

        events.emit("spotify-authorization", False, None)

    @staticmethod
    def _generate_code_verifier():
        return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("utf-8").rstrip("=")

    @staticmethod
    def _generate_code_challenge(verifier):
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    @property
    def redirect_uri(self):
        return f"http://127.0.0.1:{config.sections['spotify']['callbackport']}/callback"

    def start_authorization(self):
        """Open the user's browser to authorize this application.

        Returns the authorization URL, or None if it could not be started. The
        result of the flow is delivered through the "spotify-authorization" event.
        """

        client_id = config.sections["spotify"]["clientid"].strip()

        if not client_id:
            log.add(_("Spotify: no client ID configured. Create an application in the Spotify "
                      "developer dashboard and add its client ID and redirect URI %s"), self.redirect_uri)
            return None

        if self._auth_thread is not None and self._auth_thread.is_alive():
            return None

        self._auth_verifier = self._generate_code_verifier()
        self._auth_state = secrets.token_urlsafe(24)

        parameters = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "state": self._auth_state,
            "scope": self.SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": self._generate_code_challenge(self._auth_verifier)
        }
        authorize_url = f"{self.AUTHORIZE_URL}?{urlencode(parameters)}"

        if not self._start_auth_server():
            return None

        return authorize_url

    def _start_auth_server(self):

        from http.server import BaseHTTPRequestHandler
        from http.server import HTTPServer

        watcher = self

        class CallbackHandler(BaseHTTPRequestHandler):
            """Receives the single OAuth redirect, then the server is shut down."""

            def do_GET(self):  # pylint: disable=invalid-name

                query = parse_qs(urlparse(self.path).query)
                code = query.get("code", [None])[0]
                state = query.get("state", [None])[0]
                error = query.get("error", [None])[0]

                if error or not code or state != watcher._auth_state:  # pylint: disable=protected-access
                    message = _("Authorization failed. You can close this page and try again.")
                else:
                    message = _("Authorization complete. You can close this page.")

                body = f"<html><body><p>{message}</p></body></html>".encode()

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

                if not error and code and state == watcher._auth_state:  # pylint: disable=protected-access
                    threading.Thread(
                        target=watcher._complete_authorization,  # pylint: disable=protected-access
                        args=(code,), name="SpotifyTokenExchange"
                    ).start()

            def log_message(self, *_args):
                # Silence the default stderr access log
                pass

        port = config.sections["spotify"]["callbackport"]

        try:
            # Loopback only, so the redirect cannot be reached from outside this machine
            self._auth_server = HTTPServer(("127.0.0.1", port), CallbackHandler)

        except OSError as error:
            log.add(_("Spotify: cannot listen on port %(port)s for authorization: %(error)s"),
                    {"port": port, "error": error})
            return False

        self._auth_server.timeout = self.AUTH_TIMEOUT
        self._auth_thread = threading.Thread(
            target=self._run_auth_server, name="SpotifyAuthServer", daemon=True)
        self._auth_thread.start()

        return True

    def _run_auth_server(self):

        try:
            # Serve exactly one request, the redirect back from Spotify
            self._auth_server.handle_request()

        finally:
            self._stop_auth_server()

    def _stop_auth_server(self):

        if self._auth_server is None:
            return

        try:
            self._auth_server.server_close()

        except OSError:
            pass

        self._auth_server = None

    def _complete_authorization(self, code):

        try:
            response = self._request_token({
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": config.sections["spotify"]["clientid"].strip(),
                "code_verifier": self._auth_verifier
            })

        except Exception as error:
            log.add(_("Spotify: could not complete authorization: %s"), error)
            events.emit_main_thread("spotify-authorization", False, str(error))
            return

        self._store_token_response(response)
        events.emit_main_thread("spotify-authorization", True, None)
        events.emit_main_thread("spotify-poll-now", None)

    def _request_token(self, parameters):

        from urllib.request import Request
        from urllib.request import urlopen

        request = Request(
            self.TOKEN_URL,
            data=urlencode(parameters).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        with urlopen(request, timeout=self.HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))

    def _store_token_response(self, response):

        self._access_token = response.get("access_token")
        self._access_token_expiry = time.time() + response.get("expires_in", 3600)

        # Spotify may issue a new refresh token when refreshing; keep the newest one
        refresh_token = response.get("refresh_token")

        if refresh_token:
            config.sections["spotify"]["refreshtoken"] = refresh_token

    def _ensure_access_token(self):
        """Return a usable access token, refreshing it if needed."""

        if self._access_token and time.time() < (self._access_token_expiry - self.TOKEN_EXPIRY_MARGIN):
            return self._access_token

        refresh_token = config.sections["spotify"]["refreshtoken"]

        if not refresh_token:
            return None

        response = self._request_token({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.sections["spotify"]["clientid"].strip()
        })

        self._store_token_response(response)

        return self._access_token

    # API Requests #

    def _api_request(self, url, parameters=None):

        from urllib.error import HTTPError
        from urllib.request import Request
        from urllib.request import urlopen

        if parameters:
            url = f"{url}?{urlencode(parameters)}"

        for attempt in range(2):
            token = self._ensure_access_token()

            if not token:
                raise ValueError(_("Not authorized with Spotify"))

            request = Request(url, headers={"Authorization": f"Bearer {token}"})

            try:
                with urlopen(request, timeout=self.HTTP_TIMEOUT) as response:
                    return json.loads(response.read().decode("utf-8"))

            except HTTPError as error:
                if error.code == 401 and not attempt:
                    # Token rejected, force a refresh and try once more
                    self._access_token = None
                    continue

                if error.code == 429:
                    retry_after = error.headers.get("Retry-After")
                    raise ValueError(
                        _("Rate limited by Spotify, retry after %s seconds") % (retry_after or "?")) from error

                raise

        return None

    def _fetch_playlist_name(self, playlist_id):

        response = self._api_request(f"{self.API_BASE_URL}/playlists/{playlist_id}", {"fields": "name"})

        return (response or {}).get("name")

    def _fetch_playlist_tracks(self, playlist_id):
        """Return [(track_id, search_term)] for every track currently in the playlist."""

        url = f"{self.API_BASE_URL}/playlists/{playlist_id}/tracks"
        parameters = {"fields": self.ITEM_FIELDS, "limit": self.PAGE_LIMIT, "additional_types": "track"}
        tracks = []

        while url:
            response = self._api_request(url, parameters)

            if not response:
                break

            # Subsequent pages come back as a complete URL with parameters already applied
            parameters = None

            for item in response.get("items", []):
                if item.get("is_local"):
                    # Local files in a playlist have no usable identity to search for
                    continue

                track = item.get("track")

                if not track or track.get("type") != "track":
                    # Removed tracks come back as null, podcast episodes are not music
                    continue

                track_id = track.get("id")
                title = track.get("name")
                artists = ", ".join(
                    artist["name"] for artist in track.get("artists", []) if artist.get("name"))

                if not track_id or not title:
                    continue

                term = f"{artists} - {title}" if artists else title
                tracks.append((track_id, term))

            url = response.get("next")

        return tracks

    # Polling #

    def _schedule_poll(self):

        events.cancel_scheduled(self._poll_timer_id)

        interval_minutes = max(1, config.sections["spotify"]["pollinterval"])
        self._poll_timer_id = events.schedule(
            delay=interval_minutes * 60, callback=self.poll_now, repeat=True)

    def poll_now(self, playlist_id=None):
        """Poll watched playlists in the background."""

        if not config.sections["spotify"]["enabled"] or not self.is_authorized:
            return

        if not self.playlists:
            return

        if self._poll_thread is not None and self._poll_thread.is_alive():
            return

        self._poll_thread = threading.Thread(
            target=self._poll, args=(playlist_id,), name="SpotifyPoll", daemon=True)
        self._poll_thread.start()

    def _poll(self, playlist_id=None):

        playlist_ids = [playlist_id] if playlist_id else list(self.playlists)

        for current_id in playlist_ids:
            playlist = self.playlists.get(current_id)

            if playlist is None:
                continue

            try:
                tracks = self._fetch_playlist_tracks(current_id)
                name = self._fetch_playlist_name(current_id)

            except Exception as error:
                log.add(_('Spotify: could not read playlist "%(name)s": %(error)s'),
                        {"name": playlist.name, "error": error})
                events.emit_main_thread("spotify-poll-failed", current_id, str(error))
                continue

            events.emit_main_thread("spotify-poll-finished", current_id, name, tracks)

    def _poll_failed(self, playlist_id, error):

        playlist = self.playlists.get(playlist_id)

        if playlist is None:
            return

        playlist.last_error = error
        playlist.last_polled = time.time()

        events.emit("update-spotify-playlist", playlist_id)

    def _apply_poll_result(self, playlist_id, name, tracks):
        """Runs on the main thread: add tracks not seen before to the download list."""

        playlist = self.playlists.get(playlist_id)

        if playlist is None:
            return

        if name:
            playlist.name = name

        playlist.last_polled = time.time()
        playlist.last_error = None

        new_terms = []

        for track_id, term in tracks:
            if track_id in playlist.seen_track_ids:
                continue

            playlist.seen_track_ids.add(track_id)
            new_terms.append(term)

        if new_terms and core.download_lists is not None:
            if playlist.list_name not in core.download_lists.lists:
                core.download_lists.add_list(playlist.list_name)

            core.download_lists.add_list_items(playlist.list_name, new_terms)

            log.add(_('Spotify: added %(num)i new tracks from "%(playlist)s" to download list "%(list)s"'),
                    {"num": len(new_terms), "playlist": playlist.name, "list": playlist.list_name})

        events.emit("update-spotify-playlist", playlist_id)
        self._save()
