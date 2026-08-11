# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shutil

from unittest import TestCase
from unittest.mock import patch

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.spotifywatch import SpotifyAPIError
from pynicotine.spotifywatch import SpotifyWatch

CURRENT_FOLDER_PATH = os.path.dirname(os.path.realpath(__file__))
DATA_FOLDER_PATH = os.path.join(CURRENT_FOLDER_PATH, "temp_data")


class SpotifyWatchTest(TestCase):

    # pylint: disable=protected-access

    def setUp(self):

        config.set_data_folder(DATA_FOLDER_PATH)
        config.set_config_file(os.path.join(DATA_FOLDER_PATH, "temp_config"))

        if not os.path.exists(DATA_FOLDER_PATH):
            os.makedirs(DATA_FOLDER_PATH)

        for basename in ("download_lists.json", "download_lists.json.old"):
            stale_file_path = os.path.join(DATA_FOLDER_PATH, basename)

            if os.path.isfile(stale_file_path):
                os.remove(stale_file_path)

        core.init_components(enabled_components={
            "pluginhandler", "search", "shares", "users", "downloads", "download_lists",
            "spotify_watch", "network_filter"
        })
        config.sections["transfers"]["downloaddir"] = DATA_FOLDER_PATH
        config.sections["transfers"]["incompletedir"] = DATA_FOLDER_PATH

        # The "spotify" config section otherwise persists across tests via the shared
        # temp_config file on disk (loaded fresh each setUp, but written to by the
        # previous test's calls) -- reset it explicitly so every test starts from a
        # true clean slate, the same way download_lists.json is deleted above
        config.sections["spotify"]["client_id"] = ""
        config.sections["spotify"]["client_secret"] = ""
        config.sections["spotify"]["refresh_token"] = ""
        config.sections["spotify"]["watch_ignore_radio_edit"] = True
        config.sections["spotify"]["watched_playlists"] = []

        core.start()

    def tearDown(self):
        core.quit()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA_FOLDER_PATH)

    @staticmethod
    def _flush_main_thread_callbacks():
        """invoke_main_thread() only queues a callback -- normally drained by
        the GTK main loop's process_thread_events(), which doesn't run in
        tests. Call this after anything that uses invoke_main_thread to
        actually run the queued callback(s)."""

        events.process_thread_events()

    # Playlist ID extraction #

    def test_extract_playlist_id_from_bare_id(self):
        self.assertEqual(
            core.spotify_watch._extract_playlist_id("37i9dQZF1DXcBWIGoYBM5M"), "37i9dQZF1DXcBWIGoYBM5M")

    def test_extract_playlist_id_from_uri(self):
        self.assertEqual(
            core.spotify_watch._extract_playlist_id("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"),
            "37i9dQZF1DXcBWIGoYBM5M")

    def test_extract_playlist_id_from_url(self):
        self.assertEqual(
            core.spotify_watch._extract_playlist_id(
                "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123"),
            "37i9dQZF1DXcBWIGoYBM5M")

    def test_extract_playlist_id_invalid_input(self):
        self.assertIsNone(core.spotify_watch._extract_playlist_id(""))
        self.assertIsNone(core.spotify_watch._extract_playlist_id("not a valid playlist reference!"))
        self.assertIsNone(core.spotify_watch._extract_playlist_id(None))

    # Search term construction / radio edit handling #

    def test_build_search_term_strips_radio_edit_when_enabled(self):

        config.sections["spotify"]["watch_ignore_radio_edit"] = True
        track = {"name": "Song Title (Radio Edit)", "artists": [{"name": "Some Artist"}]}

        self.assertEqual(core.spotify_watch._build_search_term(track), "Song Title - Some Artist")

    def test_build_search_term_strips_radio_edit_dash_style(self):

        config.sections["spotify"]["watch_ignore_radio_edit"] = True
        track = {"name": "Song Title - Radio Edit", "artists": [{"name": "Some Artist"}]}

        self.assertEqual(core.spotify_watch._build_search_term(track), "Song Title - Some Artist")

    def test_build_search_term_keeps_radio_edit_when_disabled(self):

        config.sections["spotify"]["watch_ignore_radio_edit"] = False
        track = {"name": "Song Title (Radio Edit)", "artists": [{"name": "Some Artist"}]}

        self.assertEqual(core.spotify_watch._build_search_term(track), "Song Title (Radio Edit) - Some Artist")

    def test_build_search_term_joins_multiple_artists(self):

        config.sections["spotify"]["watch_ignore_radio_edit"] = True
        track = {"name": "Song Title", "artists": [{"name": "Artist One"}, {"name": "Artist Two"}]}

        self.assertEqual(core.spotify_watch._build_search_term(track), "Song Title - Artist One, Artist Two")

    def test_build_search_term_missing_data_returns_none(self):

        self.assertIsNone(core.spotify_watch._build_search_term({"name": "", "artists": [{"name": "Someone"}]}))
        self.assertIsNone(core.spotify_watch._build_search_term({"name": "Song", "artists": []}))

    # Credential / state checks #

    def test_has_credentials(self):

        self.assertFalse(core.spotify_watch.has_credentials())

        core.spotify_watch.update_credentials("client-id", "client-secret")

        self.assertTrue(core.spotify_watch.has_credentials())
        self.assertEqual(config.sections["spotify"]["client_id"], "client-id")
        self.assertEqual(config.sections["spotify"]["client_secret"], "client-secret")

    def test_is_authorized(self):

        self.assertFalse(core.spotify_watch.is_authorized())

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"

        self.assertTrue(core.spotify_watch.is_authorized())

    def test_update_ignore_radio_edit(self):

        core.spotify_watch.update_ignore_radio_edit(False)
        self.assertFalse(config.sections["spotify"]["watch_ignore_radio_edit"])

        core.spotify_watch.update_ignore_radio_edit(True)
        self.assertTrue(config.sections["spotify"]["watch_ignore_radio_edit"])

    # Watched playlist management #

    def test_has_watched_playlists(self):

        self.assertFalse(core.spotify_watch.has_watched_playlists())

        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": []}
        ]

        self.assertTrue(core.spotify_watch.has_watched_playlists())

    def test_get_watched_playlists_returns_id_and_name_only(self):

        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": ["t1", "t2"]},
            {"playlist_id": "def456", "list_name": "Another Playlist", "seen_track_ids": []}
        ]

        self.assertEqual(core.spotify_watch.get_watched_playlists(), [
            {"playlist_id": "abc123", "list_name": "My Playlist"},
            {"playlist_id": "def456", "list_name": "Another Playlist"}
        ])

    def test_remove_watched_playlist(self):

        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": []},
            {"playlist_id": "def456", "list_name": "Another Playlist", "seen_track_ids": []}
        ]

        core.spotify_watch.remove_watched_playlist("abc123")

        self.assertEqual(
            [entry["playlist_id"] for entry in config.sections["spotify"]["watched_playlists"]], ["def456"])

    def test_remove_watched_playlist_unknown_id_is_noop(self):

        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": []}
        ]

        core.spotify_watch.remove_watched_playlist("does-not-exist")

        self.assertEqual(len(config.sections["spotify"]["watched_playlists"]), 1)

    def test_remove_last_watched_playlist_cancels_poll_timer(self):

        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": []}
        ]
        core.spotify_watch._poll_timer_id = 12345

        core.spotify_watch.remove_watched_playlist("abc123")

        self.assertIsNone(core.spotify_watch._poll_timer_id)

    def test_add_watched_playlist_fails_when_not_authorized(self):

        results = []
        core.spotify_watch.add_watched_playlist("abc123", lambda success, message: results.append((success, message)))

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertEqual(config.sections["spotify"]["watched_playlists"], [])

    def test_add_watched_playlist_fails_on_invalid_url(self):

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"

        results = []
        core.spotify_watch.add_watched_playlist(
            "not a valid playlist reference!", lambda success, message: results.append((success, message)))

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])

    def test_add_watched_playlist_fails_when_already_watching(self):

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"
        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": []}
        ]

        results = []
        core.spotify_watch.add_watched_playlist("abc123", lambda success, message: results.append((success, message)))

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])

    def test_add_watched_playlist_thread_registers_and_imports_immediately(self):

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"

        def fake_api_get(path, params=None):  # noqa: ARG001 (params unused in this fake)
            if path == "/playlists/abc123":
                return {"name": "My Watched Playlist"}

            if path == "/playlists/abc123/items":
                return {
                    "items": [
                        {"track": {"id": "new-id", "name": "New Song (Radio Edit)", "artists": [{"name": "Artist"}]}}
                    ],
                    "next": None
                }

            raise AssertionError(f"Unexpected path requested: {path}")

        results = []

        with patch.object(SpotifyWatch, "_api_get", side_effect=fake_api_get):
            core.spotify_watch._add_watched_playlist_thread(
                "abc123", lambda success, message: results.append((success, message)))

        self._flush_main_thread_callbacks()

        self.assertEqual(results, [(True, "My Watched Playlist")])

        watched = config.sections["spotify"]["watched_playlists"]
        self.assertEqual(len(watched), 1)
        self.assertEqual(watched[0]["playlist_id"], "abc123")
        self.assertEqual(watched[0]["list_name"], "My Watched Playlist")
        self.assertEqual(watched[0]["seen_track_ids"], ["new-id"])

        self.assertIn("My Watched Playlist", core.download_lists.lists)
        self.assertIn("New Song - Artist", core.download_lists.lists["My Watched Playlist"].items)

        # An immediate poll must also have (re)started the periodic timer
        self.assertIsNotNone(core.spotify_watch._poll_timer_id)

    def test_add_watched_playlist_thread_disambiguates_name_collision(self):
        """A pre-existing list (manually created, Watch Folder import, or a
        different watched playlist) with the same name must not silently
        absorb the newly watched playlist's tracks -- each watched playlist
        gets its own list, even if that means appending "(2)"."""

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"
        core.download_lists.add_list("My Watched Playlist")

        def fake_api_get(path, params=None):  # noqa: ARG001
            if path == "/playlists/abc123":
                return {"name": "My Watched Playlist"}

            if path == "/playlists/abc123/items":
                return {
                    "items": [{"track": {"id": "new-id", "name": "New Song", "artists": [{"name": "Artist"}]}}],
                    "next": None
                }

            raise AssertionError(f"Unexpected path requested: {path}")

        results = []

        with patch.object(SpotifyWatch, "_api_get", side_effect=fake_api_get):
            core.spotify_watch._add_watched_playlist_thread(
                "abc123", lambda success, message: results.append((success, message)))

        self._flush_main_thread_callbacks()

        self.assertEqual(results, [(True, "My Watched Playlist (2)")])

        watched = config.sections["spotify"]["watched_playlists"]
        self.assertEqual(watched[0]["list_name"], "My Watched Playlist (2)")

        # The pre-existing list must be untouched -- nothing merged into it
        self.assertEqual(core.download_lists.lists["My Watched Playlist"].items, {})
        self.assertIn("New Song - Artist", core.download_lists.lists["My Watched Playlist (2)"].items)

    def test_add_watched_playlist_thread_reports_lookup_failure(self):

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"

        results = []

        with patch.object(SpotifyWatch, "_api_get", side_effect=SpotifyAPIError("nope", status=403)):
            core.spotify_watch._add_watched_playlist_thread(
                "abc123", lambda success, message: results.append((success, message)))

        self._flush_main_thread_callbacks()

        self.assertEqual(len(results), 1)
        success, message = results[0]
        self.assertFalse(success)
        # A 403 also triggers a GET /me diagnostic to tell apart a connection-wide
        # problem from one specific to this playlist -- the mock fails every call,
        # so it lands in the "connection itself is also failing" branch
        self.assertIn("nope", message)
        self.assertIn("reconnecting", message)
        self.assertEqual(config.sections["spotify"]["watched_playlists"], [])

    def test_add_watched_playlist_thread_403_diagnoses_ownership_mismatch(self):
        """The most likely real-world cause of a playlist-specific 403 (per
        Spotify's Feb 2026 Development Mode changes): the playlist belongs
        to someone other than the connected account. Once /me confirms the
        connection itself is fine, an owner mismatch must be called out by
        name instead of the generic "no further detail" fallback."""

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"

        def fake_api_get(path, params=None):
            if path == "/playlists/abc123" and params and params.get("fields") == "name":
                raise SpotifyAPIError("nope", status=403)

            if path == "/playlists/abc123":
                return {"owner": {"id": "someone-else", "display_name": "Someone Else"}, "collaborative": False}

            if path == "/me":
                return {"display_name": "Me", "id": "me-id", "product": "premium"}

            raise AssertionError(f"Unexpected path requested: {path}")

        results = []

        with patch.object(SpotifyWatch, "_api_get", side_effect=fake_api_get):
            core.spotify_watch._add_watched_playlist_thread(
                "abc123", lambda success, message: results.append((success, message)))

        self._flush_main_thread_callbacks()

        self.assertEqual(len(results), 1)
        success, message = results[0]
        self.assertFalse(success)
        self.assertIn("nope", message)
        self.assertIn("Someone Else", message)

    def test_diagnose_403_only_runs_once_per_playlist_per_session(self):
        """The extra GET /me + ownership-check requests shouldn't repeat
        every single time the same playlist keeps failing -- e.g. once per
        POLL_INTERVAL, indefinitely, for a playlist that stays broken."""

        call_count = 0

        def fake_api_get(path, params=None):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            return {"id": "me-id"}

        with patch.object(SpotifyWatch, "_api_get", side_effect=fake_api_get):
            first = core.spotify_watch._diagnose_403("abc123")
            calls_after_first = call_count
            second = core.spotify_watch._diagnose_403("abc123")

        self.assertGreater(calls_after_first, 0)
        self.assertEqual(call_count, calls_after_first, "second call must not make any more requests")
        self.assertNotEqual(first, second)
        self.assertIn("Already diagnosed", second)

    # Polling #

    def test_poll_playlists_noop_when_not_authorized(self):

        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": []}
        ]

        with patch.object(SpotifyWatch, "_api_get") as mock_api_get:
            core.spotify_watch._poll_playlists()

        mock_api_get.assert_not_called()

    def test_poll_single_playlist_adds_new_tracks_and_skips_already_seen_ones(self):

        entry = {"playlist_id": "abc123", "list_name": "My Watched Playlist", "seen_track_ids": ["already-seen-id"]}

        def fake_api_get(path, params=None):  # noqa: ARG001
            if path == "/playlists/abc123/items":
                return {
                    "items": [
                        {"track": {"id": "already-seen-id", "name": "Old Song", "artists": [{"name": "Old Artist"}]}},
                        {"track": {
                            "id": "new-id", "name": "New Song (Radio Edit)", "artists": [{"name": "New Artist"}]
                        }}
                    ],
                    "next": None
                }

            raise AssertionError(f"Unexpected path requested: {path}")

        with patch.object(SpotifyWatch, "_api_get", side_effect=fake_api_get):
            core.spotify_watch._poll_single_playlist(entry)

        self._flush_main_thread_callbacks()

        self.assertIn("My Watched Playlist", core.download_lists.lists)
        download_list = core.download_lists.lists["My Watched Playlist"]

        # The already-seen track must not be (re-)added, only the new one
        self.assertNotIn("Old Song - Old Artist", download_list.items)
        self.assertIn("New Song - New Artist", download_list.items)

        self.assertEqual(set(entry["seen_track_ids"]), {"already-seen-id", "new-id"})

    def test_poll_single_playlist_paginates_through_all_pages(self):

        entry = {"playlist_id": "abc123", "list_name": "Big Playlist", "seen_track_ids": []}
        page_two_url = "https://api.spotify.com/v1/playlists/abc123/items?offset=100"

        def fake_api_get(path, params=None):  # noqa: ARG001
            if path == "/playlists/abc123/items":
                return {
                    "items": [{"track": {"id": "id1", "name": "Song One", "artists": [{"name": "Artist One"}]}}],
                    "next": page_two_url
                }

            if path == page_two_url:
                return {
                    "items": [{"track": {"id": "id2", "name": "Song Two", "artists": [{"name": "Artist Two"}]}}],
                    "next": None
                }

            raise AssertionError(f"Unexpected path requested: {path}")

        with patch.object(SpotifyWatch, "_api_get", side_effect=fake_api_get):
            core.spotify_watch._poll_single_playlist(entry)

        self._flush_main_thread_callbacks()

        download_list = core.download_lists.lists["Big Playlist"]
        self.assertIn("Song One - Artist One", download_list.items)
        self.assertIn("Song Two - Artist Two", download_list.items)

    def test_poll_single_playlist_logs_and_returns_on_api_error(self):

        entry = {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": ["old-id"]}

        with patch.object(SpotifyWatch, "_api_get", side_effect=SpotifyAPIError("boom", status=403)):
            core.spotify_watch._poll_single_playlist(entry)

        self._flush_main_thread_callbacks()

        # Must not have wiped out the previously-known seen tracks on failure
        self.assertEqual(entry["seen_track_ids"], ["old-id"])
        self.assertNotIn("My Playlist", core.download_lists.lists)

    def test_poll_playlists_checks_every_watched_playlist(self):

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"
        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "Playlist One", "seen_track_ids": []},
            {"playlist_id": "def456", "list_name": "Playlist Two", "seen_track_ids": []}
        ]

        def fake_api_get(path, params=None):  # noqa: ARG001
            if path == "/playlists/abc123/items":
                return {
                    "items": [{"track": {"id": "id1", "name": "Song One", "artists": [{"name": "Artist One"}]}}],
                    "next": None
                }

            if path == "/playlists/def456/items":
                return {
                    "items": [{"track": {"id": "id2", "name": "Song Two", "artists": [{"name": "Artist Two"}]}}],
                    "next": None
                }

            raise AssertionError(f"Unexpected path requested: {path}")

        with patch.object(SpotifyWatch, "_api_get", side_effect=fake_api_get):
            core.spotify_watch._poll_playlists_thread()

        self._flush_main_thread_callbacks()

        self.assertIn("Song One - Artist One", core.download_lists.lists["Playlist One"].items)
        self.assertIn("Song Two - Artist Two", core.download_lists.lists["Playlist Two"].items)

    # Browsing the user's own playlists #

    def test_fetch_own_playlists_fails_when_not_authorized(self):

        results = []
        core.spotify_watch.fetch_own_playlists(lambda playlists, error: results.append((playlists, error)))

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][0])
        self.assertIsNotNone(results[0][1])

    def test_fetch_own_playlists_thread_sorts_by_name_and_paginates(self):

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"
        page_two_url = "https://api.spotify.com/v1/me/playlists?offset=50"

        def fake_api_get(path, params=None):  # noqa: ARG001
            if path == "/me/playlists":
                return {
                    "items": [
                        {"id": "id2", "name": "Zebra Playlist", "owner": {"display_name": "Someone"}}
                    ],
                    "next": page_two_url
                }

            if path == page_two_url:
                return {
                    "items": [
                        {"id": "id1", "name": "Alpha Playlist", "owner": {"display_name": "Me"}},
                        None  # Defensive: Spotify has been known to return null items
                    ],
                    "next": None
                }

            raise AssertionError(f"Unexpected path requested: {path}")

        results = []

        with patch.object(SpotifyWatch, "_api_get", side_effect=fake_api_get):
            core.spotify_watch._fetch_own_playlists_thread(lambda playlists, error: results.append((playlists, error)))

        self._flush_main_thread_callbacks()

        self.assertEqual(len(results), 1)
        playlists, error = results[0]
        self.assertIsNone(error)
        self.assertEqual(playlists, [
            {"id": "id1", "name": "Alpha Playlist", "owner": "Me"},
            {"id": "id2", "name": "Zebra Playlist", "owner": "Someone"}
        ])

    def test_fetch_own_playlists_thread_reports_api_error(self):

        config.sections["spotify"]["refresh_token"] = "some-refresh-token"

        results = []

        with patch.object(SpotifyWatch, "_api_get", side_effect=SpotifyAPIError("boom", status=500)):
            core.spotify_watch._fetch_own_playlists_thread(lambda playlists, error: results.append((playlists, error)))

        self._flush_main_thread_callbacks()

        self.assertEqual(results, [(None, "boom")])

    # Token refresh #

    def test_ensure_access_token_returns_cached_token_when_still_valid(self):

        core.spotify_watch._access_token = "cached-token"  # noqa: SLF001
        core.spotify_watch._access_token_expires_at = __import__("time").time() + 3600  # noqa: SLF001

        with patch.object(SpotifyWatch, "_token_request") as mock_token_request:
            token = core.spotify_watch._ensure_access_token()

        self.assertEqual(token, "cached-token")
        mock_token_request.assert_not_called()

    def test_ensure_access_token_refreshes_when_expired(self):

        core.spotify_watch.update_credentials("client-id", "client-secret")
        config.sections["spotify"]["refresh_token"] = "old-refresh-token"
        core.spotify_watch._access_token = "stale-token"  # noqa: SLF001
        core.spotify_watch._access_token_expires_at = 0  # noqa: SLF001 (already expired)

        with patch.object(
            SpotifyWatch, "_token_request",
            return_value={"access_token": "fresh-token", "expires_in": 3600}
        ) as mock_token_request:
            token = core.spotify_watch._ensure_access_token()

        self.assertEqual(token, "fresh-token")
        mock_token_request.assert_called_once()

    def test_ensure_access_token_none_without_refresh_token(self):
        self.assertIsNone(core.spotify_watch._ensure_access_token())

    # begin_authorization guard #

    def test_begin_authorization_fails_without_credentials(self):

        results = []
        core.spotify_watch.begin_authorization(lambda success, message: results.append((success, message)))

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
