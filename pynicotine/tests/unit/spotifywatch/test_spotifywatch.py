# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shutil
import threading

from unittest import TestCase
from unittest.mock import patch

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events

CURRENT_FOLDER_PATH = os.path.dirname(os.path.realpath(__file__))
DATA_FOLDER_PATH = os.path.join(CURRENT_FOLDER_PATH, "temp_data")


# Stand-ins for spotify_scraper's own typed models/exceptions -- kept minimal,
# just the attributes pynicotine.spotifywatch actually reads


class FakeScraperError(Exception):
    """Stand-in for spotify_scraper.SpotifyScraperError."""


class FakeNotFoundError(FakeScraperError):
    """Stand-in for spotify_scraper.NotFoundError."""


class FakeRef:
    def __init__(self, name):
        self.name = name


class FakeTrack:
    def __init__(self, id, name, artist_names):  # noqa: A002 (matches the real model's field name)
        self.id = id
        self.name = name
        self.artists = [FakeRef(name) for name in artist_names]


class FakePlaylistTrack:
    def __init__(self, track):
        self.track = track


class FakePlaylist:
    def __init__(self, id, name, tracks=(), owner=None):  # noqa: A002
        self.id = id
        self.name = name
        self.tracks = tuple(FakePlaylistTrack(track) for track in tracks)
        self.owner = owner


class FakeSearchResults:
    def __init__(self, playlists=()):
        self.playlists = playlists


class FakeSpotifyClient:
    """Stand-in for spotify_scraper.SpotifyClient. Call sites always do
    `with SpotifyClient(timeout=...) as client:`, constructing a fresh
    instance per call -- so tests configure behavior via class attributes
    (reset in setUp) rather than per-instance state."""

    get_playlist_side_effect = None  # callable(playlist_id, max_tracks) -> FakePlaylist, or raises
    search_side_effect = None        # callable(query) -> FakeSearchResults, or raises

    def __init__(self, timeout=15):  # noqa: ARG002
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_playlist(self, value, max_tracks=100):  # noqa: ARG002
        return type(self).get_playlist_side_effect(value)

    def search(self, query, types=("playlist",), limit=20):  # noqa: ARG002
        return type(self).search_side_effect(query)


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
        config.sections["spotify"]["watch_ignore_radio_edit"] = True
        config.sections["spotify"]["watched_playlists"] = []

        FakeSpotifyClient.get_playlist_side_effect = None
        FakeSpotifyClient.search_side_effect = None

        self._scraper_patches = [
            patch("pynicotine.spotifywatch.SPOTIFY_SCRAPER_AVAILABLE", True),
            patch("pynicotine.spotifywatch.SpotifyClient", FakeSpotifyClient),
            patch("pynicotine.spotifywatch.SpotifyNotFoundError", FakeNotFoundError),
            patch("pynicotine.spotifywatch.SpotifyScraperError", FakeScraperError)
        ]

        for scraper_patch in self._scraper_patches:
            scraper_patch.start()

        core.start()

    def tearDown(self):
        core.quit()

        for scraper_patch in self._scraper_patches:
            scraper_patch.stop()

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
        track = FakeTrack("id1", "Song Title (Radio Edit)", ["Some Artist"])

        self.assertEqual(core.spotify_watch._build_search_term(track), "Song Title - Some Artist")

    def test_build_search_term_strips_radio_edit_dash_style(self):

        config.sections["spotify"]["watch_ignore_radio_edit"] = True
        track = FakeTrack("id1", "Song Title - Radio Edit", ["Some Artist"])

        self.assertEqual(core.spotify_watch._build_search_term(track), "Song Title - Some Artist")

    def test_build_search_term_keeps_radio_edit_when_disabled(self):

        config.sections["spotify"]["watch_ignore_radio_edit"] = False
        track = FakeTrack("id1", "Song Title (Radio Edit)", ["Some Artist"])

        self.assertEqual(core.spotify_watch._build_search_term(track), "Song Title (Radio Edit) - Some Artist")

    def test_build_search_term_joins_multiple_artists(self):

        config.sections["spotify"]["watch_ignore_radio_edit"] = True
        track = FakeTrack("id1", "Song Title", ["Artist One", "Artist Two"])

        self.assertEqual(core.spotify_watch._build_search_term(track), "Song Title - Artist One, Artist Two")

    def test_build_search_term_missing_data_returns_none(self):

        self.assertIsNone(core.spotify_watch._build_search_term(FakeTrack("id1", "", ["Someone"])))
        self.assertIsNone(core.spotify_watch._build_search_term(FakeTrack("id1", "Song", [])))

    # Availability guard #

    def test_add_watched_playlist_fails_when_scraper_unavailable(self):

        with patch("pynicotine.spotifywatch.SPOTIFY_SCRAPER_AVAILABLE", False):
            results = []
            core.spotify_watch.add_watched_playlist(
                "abc123", lambda success, message: results.append((success, message)))

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertIn("spotifyscraper", results[0][1])

    def test_search_playlists_fails_when_scraper_unavailable(self):

        with patch("pynicotine.spotifywatch.SPOTIFY_SCRAPER_AVAILABLE", False):
            results = []
            core.spotify_watch.search_playlists(
                "chill", lambda playlists, error: results.append((playlists, error)))

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][0])
        self.assertIn("spotifyscraper", results[0][1])

    # Ignore radio edit setting #

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

    def test_add_watched_playlist_fails_on_invalid_url(self):

        results = []
        core.spotify_watch.add_watched_playlist(
            "not a valid playlist reference!", lambda success, message: results.append((success, message)))

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])

    def test_add_watched_playlist_fails_when_already_watching(self):

        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": []}
        ]

        results = []
        core.spotify_watch.add_watched_playlist("abc123", lambda success, message: results.append((success, message)))

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])

    def test_add_watched_playlist_thread_registers_and_imports_immediately(self):

        def fake_get_playlist(value):
            self.assertEqual(value, "abc123")
            return FakePlaylist("abc123", "My Watched Playlist", tracks=[
                FakeTrack("new-id", "New Song (Radio Edit)", ["Artist"])
            ])

        FakeSpotifyClient.get_playlist_side_effect = fake_get_playlist

        results = []
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

    def test_add_watched_playlist_thread_creates_list_even_when_empty(self):
        """A playlist can be genuinely empty (or every track missing name/
        artist data) -- _apply_new_tracks never runs in that case, so
        without a separate creation step the list would never appear at
        all, indistinguishable from watching having silently failed. Also
        confirms the playlist name gets stripped of surrounding
        whitespace."""

        FakeSpotifyClient.get_playlist_side_effect = (
            lambda value: FakePlaylist(value, "Worship ", tracks=[]))

        results = []
        core.spotify_watch._add_watched_playlist_thread(
            "abc123", lambda success, message: results.append((success, message)))

        self._flush_main_thread_callbacks()

        self.assertEqual(results, [(True, "Worship")])
        self.assertIn("Worship", core.download_lists.lists)
        self.assertEqual(core.download_lists.lists["Worship"].items, {})

    def test_add_watched_playlist_thread_disambiguates_name_collision(self):
        """A pre-existing list (manually created, Watch Folder import, or a
        different watched playlist) with the same name must not silently
        absorb the newly watched playlist's tracks -- each watched playlist
        gets its own list, even if that means appending "(2)"."""

        core.download_lists.add_list("My Watched Playlist")

        FakeSpotifyClient.get_playlist_side_effect = (
            lambda value: FakePlaylist(value, "My Watched Playlist", tracks=[
                FakeTrack("new-id", "New Song", ["Artist"])
            ]))

        results = []
        core.spotify_watch._add_watched_playlist_thread(
            "abc123", lambda success, message: results.append((success, message)))

        self._flush_main_thread_callbacks()

        self.assertEqual(results, [(True, "My Watched Playlist (2)")])

        watched = config.sections["spotify"]["watched_playlists"]
        self.assertEqual(watched[0]["list_name"], "My Watched Playlist (2)")

        # The pre-existing list must be untouched -- nothing merged into it
        self.assertEqual(core.download_lists.lists["My Watched Playlist"].items, {})
        self.assertIn("New Song - Artist", core.download_lists.lists["My Watched Playlist (2)"].items)

    def test_add_watched_playlist_thread_reports_not_found(self):

        def fake_get_playlist(_value):
            raise FakeNotFoundError("nope")

        FakeSpotifyClient.get_playlist_side_effect = fake_get_playlist

        results = []
        core.spotify_watch._add_watched_playlist_thread(
            "abc123", lambda success, message: results.append((success, message)))

        self._flush_main_thread_callbacks()

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertEqual(config.sections["spotify"]["watched_playlists"], [])

    def test_add_watched_playlist_thread_reports_scraper_error(self):

        def fake_get_playlist(_value):
            raise FakeScraperError("boom")

        FakeSpotifyClient.get_playlist_side_effect = fake_get_playlist

        results = []
        core.spotify_watch._add_watched_playlist_thread(
            "abc123", lambda success, message: results.append((success, message)))

        self._flush_main_thread_callbacks()

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][0])
        self.assertIn("boom", results[0][1])
        self.assertEqual(config.sections["spotify"]["watched_playlists"], [])

    # Polling #

    def test_poll_playlists_noop_when_scraper_unavailable(self):

        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": []}
        ]

        threads_before = set(threading.enumerate())

        with patch("pynicotine.spotifywatch.SPOTIFY_SCRAPER_AVAILABLE", False):
            core.spotify_watch._poll_playlists()

        # No polling thread should have been spawned at all
        self.assertEqual(set(threading.enumerate()), threads_before)

    def test_poll_single_playlist_adds_new_tracks_and_skips_already_seen_ones(self):

        entry = {"playlist_id": "abc123", "list_name": "My Watched Playlist", "seen_track_ids": ["already-seen-id"]}

        FakeSpotifyClient.get_playlist_side_effect = lambda value: FakePlaylist(value, "My Watched Playlist", tracks=[
            FakeTrack("already-seen-id", "Old Song", ["Old Artist"]),
            FakeTrack("new-id", "New Song (Radio Edit)", ["New Artist"])
        ])

        core.spotify_watch._poll_single_playlist(entry)
        self._flush_main_thread_callbacks()

        self.assertIn("My Watched Playlist", core.download_lists.lists)
        download_list = core.download_lists.lists["My Watched Playlist"]

        # The already-seen track must not be (re-)added, only the new one
        self.assertNotIn("Old Song - Old Artist", download_list.items)
        self.assertIn("New Song - New Artist", download_list.items)

        self.assertEqual(set(entry["seen_track_ids"]), {"already-seen-id", "new-id"})

    def test_poll_single_playlist_logs_and_returns_on_error(self):

        entry = {"playlist_id": "abc123", "list_name": "My Playlist", "seen_track_ids": ["old-id"]}

        def fake_get_playlist(_value):
            raise FakeScraperError("boom")

        FakeSpotifyClient.get_playlist_side_effect = fake_get_playlist

        core.spotify_watch._poll_single_playlist(entry)
        self._flush_main_thread_callbacks()

        # Must not have wiped out the previously-known seen tracks on failure
        self.assertEqual(entry["seen_track_ids"], ["old-id"])
        self.assertNotIn("My Playlist", core.download_lists.lists)

    def test_poll_playlists_checks_every_watched_playlist(self):

        config.sections["spotify"]["watched_playlists"] = [
            {"playlist_id": "abc123", "list_name": "Playlist One", "seen_track_ids": []},
            {"playlist_id": "def456", "list_name": "Playlist Two", "seen_track_ids": []}
        ]

        def fake_get_playlist(value):
            if value == "abc123":
                return FakePlaylist(value, "Playlist One", tracks=[FakeTrack("id1", "Song One", ["Artist One"])])

            if value == "def456":
                return FakePlaylist(value, "Playlist Two", tracks=[FakeTrack("id2", "Song Two", ["Artist Two"])])

            raise AssertionError(f"Unexpected playlist id requested: {value}")

        FakeSpotifyClient.get_playlist_side_effect = fake_get_playlist

        # _poll_playlists_thread dispatches one independent thread per
        # playlist (see its own docstring for why) rather than checking
        # them inline -- capture and join the ones it spawns so this test
        # doesn't race ahead of them finishing
        threads_before = set(threading.enumerate())

        core.spotify_watch._poll_playlists_thread()

        for thread in set(threading.enumerate()) - threads_before:
            thread.join(timeout=5)

        self._flush_main_thread_callbacks()

        self.assertIn("Song One - Artist One", core.download_lists.lists["Playlist One"].items)
        self.assertIn("Song Two - Artist Two", core.download_lists.lists["Playlist Two"].items)

    # Searching #

    def test_search_playlists_empty_query_returns_immediately(self):

        results = []
        core.spotify_watch.search_playlists("   ", lambda playlists, error: results.append((playlists, error)))

        self.assertEqual(results, [([], None)])

    def test_search_playlists_thread_returns_results(self):

        def fake_search(query):
            self.assertEqual(query, "chill vibes")
            return FakeSearchResults(playlists=[
                FakePlaylist("id1", "Chill Vibes", owner=FakeRef("Someone")),
                FakePlaylist("id2", "More Chill", owner=None)
            ])

        FakeSpotifyClient.search_side_effect = fake_search

        results = []
        core.spotify_watch._search_playlists_thread(
            "chill vibes", lambda playlists, error: results.append((playlists, error)))

        self._flush_main_thread_callbacks()

        self.assertEqual(len(results), 1)
        playlists, error = results[0]
        self.assertIsNone(error)
        self.assertEqual(playlists, [
            {"id": "id1", "name": "Chill Vibes", "owner": "Someone"},
            {"id": "id2", "name": "More Chill", "owner": ""}
        ])

    def test_search_playlists_thread_reports_error(self):

        def fake_search(_query):
            raise FakeScraperError("boom")

        FakeSpotifyClient.search_side_effect = fake_search

        results = []
        core.spotify_watch._search_playlists_thread(
            "chill vibes", lambda playlists, error: results.append((playlists, error)))

        self._flush_main_thread_callbacks()

        self.assertEqual(len(results), 1)
        playlists, error = results[0]
        self.assertIsNone(playlists)
        self.assertIn("boom", error)
