# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shutil

from unittest import TestCase
from unittest.mock import patch

from pynicotine.config import config
from pynicotine.core import core
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
        config.sections["spotify"]["watch_enabled"] = False
        config.sections["spotify"]["watch_playlist_id"] = ""
        config.sections["spotify"]["watch_ignore_radio_edit"] = True
        config.sections["spotify"]["watch_seen_track_ids"] = []

        core.start()

    def tearDown(self):
        core.quit()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA_FOLDER_PATH)

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

    def test_is_watch_enabled_requires_both_flag_and_playlist(self):

        self.assertFalse(core.spotify_watch.is_watch_enabled())

        core.spotify_watch.update_watch_settings(
            enabled=True, playlist_url_or_id="", ignore_radio_edit=True)
        self.assertFalse(core.spotify_watch.is_watch_enabled(), "no playlist set -- must not be considered enabled")

        core.spotify_watch.update_watch_settings(
            enabled=True, playlist_url_or_id="abc123", ignore_radio_edit=True)
        self.assertTrue(core.spotify_watch.is_watch_enabled())

    def test_update_watch_settings_resets_seen_tracks_on_playlist_change(self):

        config.sections["spotify"]["watch_seen_track_ids"] = ["track1", "track2"]

        core.spotify_watch.update_watch_settings(enabled=True, playlist_url_or_id="abc123", ignore_radio_edit=True)
        self.assertEqual(config.sections["spotify"]["watch_seen_track_ids"], [])

    def test_update_watch_settings_keeps_seen_tracks_for_same_playlist(self):

        core.spotify_watch.update_watch_settings(enabled=True, playlist_url_or_id="abc123", ignore_radio_edit=True)
        config.sections["spotify"]["watch_seen_track_ids"] = ["track1", "track2"]

        core.spotify_watch.update_watch_settings(enabled=True, playlist_url_or_id="abc123", ignore_radio_edit=False)
        self.assertEqual(config.sections["spotify"]["watch_seen_track_ids"], ["track1", "track2"])

    # Polling #

    def test_poll_playlist_noop_when_not_enabled(self):

        with patch.object(SpotifyWatch, "_api_get") as mock_api_get:
            core.spotify_watch._poll_playlist()

        mock_api_get.assert_not_called()

    def test_poll_playlist_noop_when_not_authorized(self):

        core.spotify_watch.update_watch_settings(enabled=True, playlist_url_or_id="abc123", ignore_radio_edit=True)

        with patch.object(SpotifyWatch, "_api_get") as mock_api_get:
            core.spotify_watch._poll_playlist()

        mock_api_get.assert_not_called()

    def test_poll_playlist_adds_new_tracks_and_skips_already_seen_ones(self):

        core.spotify_watch.update_watch_settings(enabled=True, playlist_url_or_id="abc123", ignore_radio_edit=True)
        config.sections["spotify"]["refresh_token"] = "some-refresh-token"
        config.sections["spotify"]["watch_seen_track_ids"] = ["already-seen-id"]

        def fake_api_get(path, params=None):  # noqa: ARG001 (params unused in this fake)
            if path == "/playlists/abc123":
                return {"name": "My Watched Playlist"}

            if path == "/playlists/abc123/tracks":
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
            core.spotify_watch._poll_playlist()

        self.assertIn("My Watched Playlist", core.download_lists.lists)
        download_list = core.download_lists.lists["My Watched Playlist"]

        # The already-seen track must not be (re-)added, only the new one
        self.assertNotIn("Old Song - Old Artist", download_list.items)
        self.assertIn("New Song - New Artist", download_list.items)

        self.assertEqual(set(config.sections["spotify"]["watch_seen_track_ids"]), {"already-seen-id", "new-id"})

    def test_poll_playlist_paginates_through_all_pages(self):

        core.spotify_watch.update_watch_settings(enabled=True, playlist_url_or_id="abc123", ignore_radio_edit=True)
        config.sections["spotify"]["refresh_token"] = "some-refresh-token"

        page_two_url = "https://api.spotify.com/v1/playlists/abc123/tracks?offset=100"

        def fake_api_get(path, params=None):  # noqa: ARG001
            if path == "/playlists/abc123":
                return {"name": "Big Playlist"}

            if path == "/playlists/abc123/tracks":
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
            core.spotify_watch._poll_playlist()

        download_list = core.download_lists.lists["Big Playlist"]
        self.assertIn("Song One - Artist One", download_list.items)
        self.assertIn("Song Two - Artist Two", download_list.items)

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
