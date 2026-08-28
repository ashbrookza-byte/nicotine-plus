# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os

from unittest import TestCase

from pynicotine.lexiconsync import LexiconClient
from pynicotine.lexiconsync import base_title
from pynicotine.lexiconsync import import_finished_file
from pynicotine.lexiconsync import is_same_song
from pynicotine.lexiconsync import preferred_track
from pynicotine.lexiconsync import process_pending_files
from pynicotine.lexiconsync import sync_smartlists


class FakeLexiconClient:
    """In-memory stand-in for LexiconClient, implementing exactly the calls
    the sync passes make, against a fake library and playlist tree."""

    TYPE_FOLDER = LexiconClient.TYPE_FOLDER
    TYPE_PLAYLIST = LexiconClient.TYPE_PLAYLIST
    TYPE_SMARTLIST = LexiconClient.TYPE_SMARTLIST

    node_type = staticmethod(LexiconClient.node_type)
    location_smartlist = staticmethod(LexiconClient.location_smartlist)

    def __init__(self, playlists=(), tracks=()):

        # id -> playlist node dict (with "trackIds" for normal playlists)
        self.playlists = {node["id"]: dict(node) for node in playlists}

        # id -> track dict
        self.tracks = {track["id"]: dict(track) for track in tracks}

        self._next_id = 1000
        self.track_edits = []

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    # Playlists #

    def get_playlist_tree(self):
        return [dict(node) for node in self.playlists.values()]

    def get_playlist(self, playlist_id):
        return dict(self.playlists.get(playlist_id, {}))

    def create_playlist(self, name, playlist_type, parent_id=None, smartlist=None):

        playlist_id = self._new_id()
        self.playlists[playlist_id] = {
            "id": playlist_id, "name": name, "type": playlist_type,
            "parentId": parent_id, "smartlist": smartlist, "trackIds": []
        }
        return playlist_id

    def update_playlist(self, playlist_id, name=None, smartlist=None):

        node = self.playlists[playlist_id]

        if name is not None:
            node["name"] = name

        if smartlist is not None:
            node["smartlist"] = smartlist

    def add_playlist_tracks(self, playlist_id, track_ids):
        self.playlists[playlist_id].setdefault("trackIds", []).extend(track_ids)

    def remove_playlist_tracks(self, playlist_id, track_ids):
        node = self.playlists[playlist_id]
        node["trackIds"] = [track_id for track_id in node.get("trackIds", [])
                            if track_id not in track_ids]

    # Tracks #

    def add_tracks(self, locations):

        added = []

        for location in locations:
            existing = next((track for track in self.tracks.values()
                             if track.get("location") == location), None)

            if existing is not None:
                added.append(dict(existing))
                continue

            track_id = self._new_id()
            basename, _extension = os.path.splitext(os.path.basename(location))
            artist, _separator, title = basename.partition(" - ")
            track = {
                "id": track_id, "location": location,
                "locationUnique": location.casefold(),
                "artist": artist, "title": title, "duration": 200, "bitrate": 320
            }
            self.tracks[track_id] = track
            added.append(dict(track))

        return added

    def search_tracks(self, filters):

        title_filter = filters.get("title", "").casefold()
        return [dict(track) for track in self.tracks.values()
                if title_filter in str(track.get("title", "")).casefold()]

    def update_track(self, track_id, edits):
        self.track_edits.append((track_id, edits))
        self.tracks[track_id].update(edits)

    def delete_tracks(self, track_ids):
        for track_id in track_ids:
            self.tracks.pop(track_id, None)


class DuplicateLogicTest(TestCase):

    def test_base_title_strips_version_qualifiers(self):

        self.assertEqual(base_title("Amman (Extended Version)"), "amman")
        self.assertEqual(base_title("Amman [Nils Hoffmann Remix]"), "amman")
        self.assertEqual(base_title("  Amman  "), "amman")
        self.assertEqual(base_title(None), "")

    def test_is_same_song_requires_artist_and_base_title(self):

        original = {"artist": "Emmit Fenn", "title": "Amman"}
        extended = {"artist": "Emmit Fenn", "title": "Amman (Extended Version)"}
        other_song = {"artist": "Emmit Fenn", "title": "Painting Greys"}
        other_artist = {"artist": "Ben Böhmer", "title": "Amman"}
        untitled = {"artist": "Emmit Fenn", "title": ""}

        self.assertTrue(is_same_song(original, extended))
        self.assertFalse(is_same_song(original, other_song))
        self.assertFalse(is_same_song(original, other_artist))
        self.assertFalse(is_same_song(original, untitled))

    def test_significantly_longer_version_wins(self):

        radio_flac = {"duration": 200, "bitrate": 1411, "location": "/a/song.flac"}
        extended_mp3 = {"duration": 420, "bitrate": 320, "location": "/b/song.mp3"}

        self.assertIs(preferred_track(extended_mp3, radio_flac), extended_mp3)
        self.assertIs(preferred_track(radio_flac, extended_mp3), extended_mp3)

    def test_lossless_wins_at_similar_length(self):

        mp3 = {"duration": 300, "bitrate": 320, "location": "/a/song.mp3"}
        flac = {"duration": 310, "bitrate": 1411, "location": "/b/song.flac"}

        self.assertIs(preferred_track(flac, mp3), flac)
        self.assertIs(preferred_track(mp3, flac), flac)

    def test_higher_bitrate_wins_between_lossy_files(self):

        low = {"duration": 300, "bitrate": 192, "location": "/a/song.mp3"}
        high = {"duration": 300, "bitrate": 320, "location": "/b/song.mp3"}

        self.assertIs(preferred_track(high, low), high)
        self.assertIs(preferred_track(low, high), high)

    def test_tie_keeps_the_old_track(self):

        new_track = {"duration": 300, "bitrate": 320, "location": "/a/song.mp3"}
        old_track = {"duration": 300, "bitrate": 320, "location": "/b/song.mp3"}

        self.assertIs(preferred_track(new_track, old_track), old_track)

    def test_preferences_can_be_disabled(self):

        short_flac = {"duration": 200, "bitrate": 1411, "location": "/a/song.flac"}
        long_mp3 = {"duration": 420, "bitrate": 320, "location": "/b/song.mp3"}

        # Without prefer_longer, quality decides
        self.assertIs(
            preferred_track(long_mp3, short_flac, prefer_longer=False), short_flac)

        # Without either preference, the old track is kept
        self.assertIs(
            preferred_track(long_mp3, short_flac, prefer_longer=False, prefer_lossless=False),
            short_flac)


class SmartlistSyncTest(TestCase):

    def test_creates_parent_folder_and_smartlists(self):

        client = FakeLexiconClient()
        playlist_ids = {}

        synced = sync_smartlists(
            client,
            jobs=[("House", "/music/House"), ("Techno", "/music/Techno")],
            playlist_ids=playlist_ids,
            parent_folder_name="nicotine"
        )

        self.assertEqual(synced, {"House", "Techno"})
        self.assertEqual(set(playlist_ids), {"House", "Techno"})

        folders = [node for node in client.playlists.values()
                   if node["type"] == client.TYPE_FOLDER]
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0]["name"], "nicotine")

        smartlists = [node for node in client.playlists.values()
                      if node["type"] == client.TYPE_SMARTLIST]
        self.assertEqual(len(smartlists), 2)

        for node in smartlists:
            self.assertEqual(node["parentId"], folders[0]["id"])
            rule = node["smartlist"]["rules"][0]
            self.assertEqual(rule["field"], "location")
            self.assertEqual(rule["operator"], "StringContains")
            self.assertTrue(rule["values"][0].endswith(os.sep))

    def test_second_pass_is_idempotent(self):

        client = FakeLexiconClient()
        playlist_ids = {}
        jobs = [("House", "/music/House")]

        sync_smartlists(client, jobs, playlist_ids, "nicotine")
        count_after_first = len(client.playlists)

        sync_smartlists(client, jobs, playlist_ids, "nicotine")
        self.assertEqual(len(client.playlists), count_after_first)

    def test_adopts_existing_smartlist_without_state(self):

        client = FakeLexiconClient()
        first_ids = {}
        sync_smartlists(client, [("House", "/music/House")], first_ids, "nicotine")

        # Same situation but with a lost state file: no duplicate is created
        recovered_ids = {}
        sync_smartlists(client, [("House", "/music/House")], recovered_ids, "nicotine")

        self.assertEqual(recovered_ids, first_ids)

    def test_rename_updates_existing_smartlist(self):

        client = FakeLexiconClient()
        playlist_ids = {}
        sync_smartlists(client, [("House", "/music/House")], playlist_ids, "nicotine")

        # Same Lexicon playlist ID carried over under the new list name
        playlist_ids["Deep House"] = playlist_ids.pop("House")
        sync_smartlists(client, [("Deep House", "/music/Deep House")], playlist_ids, "nicotine")

        node = client.playlists[playlist_ids["Deep House"]]
        self.assertEqual(node["name"], "Deep House")
        self.assertIn("/music/Deep House" + os.sep, node["smartlist"]["rules"][0]["values"][0])

        smartlists = [candidate for candidate in client.playlists.values()
                      if candidate["type"] == client.TYPE_SMARTLIST]
        self.assertEqual(len(smartlists), 1)


class ImportDedupeTest(TestCase):

    def test_import_without_duplicate(self):

        client = FakeLexiconClient()
        outcome = import_finished_file(
            client, "/music/House/Artist - New Song.mp3",
            dedupe_enabled=True, prefer_longer=True, prefer_lossless=True)

        self.assertEqual(outcome, "imported")
        self.assertEqual(len(client.tracks), 1)

    def test_new_better_version_replaces_old(self):

        old_track = {
            "id": 1, "artist": "Artist", "title": "Song",
            "duration": 200, "bitrate": 320,
            "location": "/music/old/Artist - Song.mp3",
            "locationUnique": "/music/old/artist - song.mp3",
            "rating": 4
        }
        playlist = {
            "id": 10, "name": "Peak Time", "type": FakeLexiconClient.TYPE_PLAYLIST,
            "parentId": None, "trackIds": [1]
        }
        client = FakeLexiconClient(playlists=[playlist], tracks=[old_track])

        # The fake importer parses "Artist - Song (Extended Mix).flac" and
        # gives it duration 200 -- lossless wins at similar length
        outcome = import_finished_file(
            client, "/music/House/Artist - Song (Extended Mix).flac",
            dedupe_enabled=True, prefer_longer=True, prefer_lossless=True)

        self.assertIn("replaced older version", outcome)

        # Old track is gone from the library, new one took its playlist spot
        self.assertNotIn(1, client.tracks)
        new_track_id = next(iter(client.tracks))
        self.assertEqual(client.playlists[10]["trackIds"], [new_track_id])

        # User metadata came across
        self.assertEqual(client.tracks[new_track_id].get("rating"), 4)

    def test_old_better_version_withdraws_new_import(self):

        old_track = {
            "id": 1, "artist": "Artist", "title": "Song (Extended Mix)",
            "duration": 420, "bitrate": 1411,
            "location": "/music/old/Artist - Song (Extended Mix).flac",
            "locationUnique": "/music/old/artist - song (extended mix).flac"
        }
        client = FakeLexiconClient(tracks=[old_track])

        outcome = import_finished_file(
            client, "/music/House/Artist - Song.mp3",
            dedupe_enabled=True, prefer_longer=True, prefer_lossless=True)

        self.assertIn("kept existing version", outcome)

        # Only the old track remains in the library
        self.assertEqual(list(client.tracks), [1])

    def test_identical_audio_relocates_and_copies_cues(self):

        old_track = {
            "id": 1, "artist": "Artist", "title": "Song",
            "duration": 200, "bitrate": 320,
            "location": "/music/old/Artist - Song.mp3",
            "locationUnique": "/music/old/artist - song.mp3",
            "rating": 5,
            "cuepoints": [{"position": 0, "startTime": 12.5, "type": "1"}]
        }
        client = FakeLexiconClient(tracks=[old_track])

        # The fake importer assigns duration 200 / bitrate 320: same audio,
        # so the new in-folder copy wins even though it's a quality tie
        outcome = import_finished_file(
            client, "/music/House/Artist - Song.mp3",
            dedupe_enabled=True, prefer_longer=True, prefer_lossless=True)

        self.assertIn("replaced older version", outcome)
        self.assertNotIn(1, client.tracks)

        new_track = client.tracks[next(iter(client.tracks))]
        self.assertEqual(new_track.get("rating"), 5)
        self.assertEqual(new_track.get("cuepoints"), old_track["cuepoints"])

    def test_dedupe_disabled_keeps_both(self):

        old_track = {
            "id": 1, "artist": "Artist", "title": "Song",
            "duration": 200, "bitrate": 128,
            "location": "/music/old/Artist - Song.mp3",
            "locationUnique": "/music/old/artist - song.mp3"
        }
        client = FakeLexiconClient(tracks=[old_track])

        outcome = import_finished_file(
            client, "/music/House/Artist - Song.flac",
            dedupe_enabled=False, prefer_longer=True, prefer_lossless=True)

        self.assertEqual(outcome, "imported")
        self.assertEqual(len(client.tracks), 2)

    def test_reimporting_same_file_is_not_a_duplicate_of_itself(self):

        client = FakeLexiconClient()
        location = "/music/House/Artist - Song.mp3"

        import_finished_file(
            client, location, dedupe_enabled=True, prefer_longer=True, prefer_lossless=True)
        outcome = import_finished_file(
            client, location, dedupe_enabled=True, prefer_longer=True, prefer_lossless=True)

        self.assertEqual(outcome, "imported")
        self.assertEqual(len(client.tracks), 1)


class PendingFilesTest(TestCase):

    def test_missing_files_are_dropped_and_existing_are_imported(self):

        client = FakeLexiconClient()
        pending_files = [("House", "/definitely/not/a/real/file.mp3")]

        outcomes = process_pending_files(
            client, pending_files,
            dedupe_enabled=False, prefer_longer=True, prefer_lossless=True)

        self.assertEqual(pending_files, [])
        self.assertEqual(outcomes, [])
        self.assertEqual(len(client.tracks), 0)
