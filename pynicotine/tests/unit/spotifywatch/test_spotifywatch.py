# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import hashlib
import os
import shutil

from unittest import TestCase

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

        for basename in ("spotify_watch.json", "spotify_watch.json.old",
                         "download_lists.json", "download_lists.json.old"):
            stale_file_path = os.path.join(DATA_FOLDER_PATH, basename)

            if os.path.isfile(stale_file_path):
                os.remove(stale_file_path)

        core.init_components(enabled_components={
            "pluginhandler", "search", "shares", "users", "downloads", "download_lists",
            "spotify_watch", "network_filter"
        })
        config.sections["transfers"]["downloaddir"] = DATA_FOLDER_PATH
        config.sections["transfers"]["incompletedir"] = DATA_FOLDER_PATH
        # Disabled by default so that adding a playlist does not kick off a real
        # poll thread against the Spotify API from a unit test
        config.sections["spotify"]["enabled"] = False
        config.sections["spotify"]["clientid"] = "test_client_id"
        config.sections["spotify"]["refreshtoken"] = "test_refresh_token"
        config.sections["spotify"]["callbackport"] = 8888
        core.start()

    def tearDown(self):
        core.quit()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA_FOLDER_PATH)

    # Playlist link parsing #

    def test_parse_playlist_id_from_url(self):

        parse = SpotifyWatch.parse_playlist_id

        self.assertEqual(
            parse("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"), "37i9dQZF1DXcBWIGoYBM5M")

        # Links copied from the app carry a tracking query string
        self.assertEqual(
            parse("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123"),
            "37i9dQZF1DXcBWIGoYBM5M")

        # Localised links include a country segment
        self.assertEqual(
            parse("https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M"),
            "37i9dQZF1DXcBWIGoYBM5M")

    def test_parse_playlist_id_from_uri_and_bare_id(self):

        parse = SpotifyWatch.parse_playlist_id

        self.assertEqual(parse("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"), "37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(parse("37i9dQZF1DXcBWIGoYBM5M"), "37i9dQZF1DXcBWIGoYBM5M")

    def test_parse_playlist_id_rejects_invalid(self):

        parse = SpotifyWatch.parse_playlist_id

        self.assertIsNone(parse(""))
        self.assertIsNone(parse(None))
        self.assertIsNone(parse("https://example.com/playlist/abc123"))
        self.assertIsNone(parse("https://open.spotify.com/album/37i9dQZF1DXcBWIGoYBM5M"))

    # PKCE #

    def test_code_challenge_matches_verifier(self):
        """The challenge must be the base64url-encoded SHA-256 of the verifier,
        without padding, or Spotify rejects the token exchange."""

        verifier = SpotifyWatch._generate_code_verifier()
        challenge = SpotifyWatch._generate_code_challenge(verifier)

        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("utf-8")).digest()).decode("utf-8").rstrip("=")

        self.assertEqual(challenge, expected)
        self.assertNotIn("=", challenge)

    def test_code_verifier_is_unique_and_long_enough(self):

        verifiers = {SpotifyWatch._generate_code_verifier() for _unused in range(10)}

        self.assertEqual(len(verifiers), 10)

        for verifier in verifiers:
            # Spotify requires a verifier between 43 and 128 characters
            self.assertGreaterEqual(len(verifier), 43)
            self.assertLessEqual(len(verifier), 128)

    # Watched playlists #

    def test_add_playlist_creates_download_list(self):

        core.spotify_watch.add_playlist(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "House Wants")

        self.assertIn("37i9dQZF1DXcBWIGoYBM5M", core.spotify_watch.playlists)
        self.assertIn("House Wants", core.download_lists.lists)

    def test_add_playlist_rejects_duplicates_and_bad_links(self):

        core.spotify_watch.add_playlist(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "House Wants")

        self.assertIsNone(core.spotify_watch.add_playlist(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "House Wants"))
        self.assertIsNone(core.spotify_watch.add_playlist("not a playlist link", "Other"))
        self.assertIsNone(core.spotify_watch.add_playlist(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", ""))

    def test_remove_playlist(self):

        core.spotify_watch.add_playlist("37i9dQZF1DXcBWIGoYBM5M", "House Wants")
        core.spotify_watch.remove_playlist("37i9dQZF1DXcBWIGoYBM5M")

        self.assertEqual(core.spotify_watch.playlists, {})

    # Applying poll results #

    def test_new_tracks_are_added_to_download_list(self):

        core.spotify_watch.add_playlist("37i9dQZF1DXcBWIGoYBM5M", "House Wants")

        core.spotify_watch._apply_poll_result("37i9dQZF1DXcBWIGoYBM5M", "House Wants", [
            ("track1", "Artist One - Song One"),
            ("track2", "Artist Two - Song Two")
        ])

        items = core.download_lists.lists["House Wants"].items

        self.assertIn("Artist One - Song One", items)
        self.assertIn("Artist Two - Song Two", items)

    def test_already_seen_tracks_are_not_added_again(self):
        """Polling returns the whole playlist every time, so only tracks whose IDs
        have not been seen before may be added."""

        core.spotify_watch.add_playlist("37i9dQZF1DXcBWIGoYBM5M", "House Wants")

        core.spotify_watch._apply_poll_result(
            "37i9dQZF1DXcBWIGoYBM5M", "House Wants", [("track1", "Artist One - Song One")])

        # The user removes the item from the download list by hand
        core.download_lists.remove_list_item("House Wants", "Artist One - Song One")

        # A later poll returns the same track plus a new one
        core.spotify_watch._apply_poll_result("37i9dQZF1DXcBWIGoYBM5M", "House Wants", [
            ("track1", "Artist One - Song One"),
            ("track2", "Artist Two - Song Two")
        ])

        items = core.download_lists.lists["House Wants"].items

        self.assertNotIn("Artist One - Song One", items)
        self.assertIn("Artist Two - Song Two", items)

    def test_poll_result_updates_playlist_name(self):

        core.spotify_watch.add_playlist("37i9dQZF1DXcBWIGoYBM5M", "House Wants")
        core.spotify_watch._apply_poll_result("37i9dQZF1DXcBWIGoYBM5M", "Deep House Weekly", [])

        self.assertEqual(core.spotify_watch.playlists["37i9dQZF1DXcBWIGoYBM5M"].name, "Deep House Weekly")

    def test_poll_failure_is_recorded(self):

        core.spotify_watch.add_playlist("37i9dQZF1DXcBWIGoYBM5M", "House Wants")
        core.spotify_watch._poll_failed("37i9dQZF1DXcBWIGoYBM5M", "Rate limited")

        self.assertEqual(core.spotify_watch.playlists["37i9dQZF1DXcBWIGoYBM5M"].last_error, "Rate limited")

    # Persistence #

    def test_save_and_load_round_trip(self):

        core.spotify_watch.add_playlist("37i9dQZF1DXcBWIGoYBM5M", "House Wants")
        core.spotify_watch._apply_poll_result(
            "37i9dQZF1DXcBWIGoYBM5M", "Deep House Weekly", [("track1", "Artist One - Song One")])
        core.spotify_watch._save()

        reloaded = SpotifyWatch()
        reloaded._load()

        playlist = reloaded.playlists["37i9dQZF1DXcBWIGoYBM5M"]

        self.assertEqual(playlist.list_name, "House Wants")
        self.assertEqual(playlist.name, "Deep House Weekly")
        self.assertIn("track1", playlist.seen_track_ids)

    # Authorization guards #

    def test_authorization_requires_client_id(self):

        config.sections["spotify"]["clientid"] = ""

        self.assertIsNone(core.spotify_watch.start_authorization())

    def test_sign_out_clears_refresh_token(self):

        core.spotify_watch.sign_out()

        self.assertEqual(config.sections["spotify"]["refreshtoken"], "")
        self.assertFalse(core.spotify_watch.is_authorized)

    def test_redirect_uri_is_loopback(self):
        """The redirect must be loopback only, and must match what the user
        registered in the Spotify developer dashboard."""

        config.sections["spotify"]["callbackport"] = 8888

        self.assertEqual(core.spotify_watch.redirect_uri, "http://127.0.0.1:8888/callback")

    def test_poll_does_nothing_without_authorization(self):
        """No refresh token means no poll thread, so nothing reaches the network."""

        core.spotify_watch.add_playlist("37i9dQZF1DXcBWIGoYBM5M", "House Wants")

        config.sections["spotify"]["enabled"] = True
        config.sections["spotify"]["refreshtoken"] = ""

        core.spotify_watch.poll_now()

        self.assertIsNone(core.spotify_watch._poll_thread)

    def test_poll_does_nothing_while_disabled(self):

        core.spotify_watch.add_playlist("37i9dQZF1DXcBWIGoYBM5M", "House Wants")

        config.sections["spotify"]["enabled"] = False
        core.spotify_watch.poll_now()

        self.assertIsNone(core.spotify_watch._poll_thread)
