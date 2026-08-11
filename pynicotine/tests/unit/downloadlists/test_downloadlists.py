# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shutil

from unittest import TestCase

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.downloadlists import DownloadListItemStatus
from pynicotine.slskmessages import FileAttributes
from pynicotine.slskmessages import FileSearchResponse

CURRENT_FOLDER_PATH = os.path.dirname(os.path.realpath(__file__))
DATA_FOLDER_PATH = os.path.join(CURRENT_FOLDER_PATH, "temp_data")


class DownloadListsTest(TestCase):

    # pylint: disable=protected-access

    def setUp(self):

        config.set_data_folder(DATA_FOLDER_PATH)
        config.set_config_file(os.path.join(DATA_FOLDER_PATH, "temp_config"))

        if not os.path.exists(DATA_FOLDER_PATH):
            os.makedirs(DATA_FOLDER_PATH)

        # Ensure each test starts with a clean slate, regardless of what a
        # previous test in this run may have persisted to disk (including its backup)
        for basename in ("download_lists.json", "download_lists.json.old"):
            stale_file_path = os.path.join(DATA_FOLDER_PATH, basename)

            if os.path.isfile(stale_file_path):
                os.remove(stale_file_path)

        core.init_components(enabled_components={
            "pluginhandler", "search", "shares", "users", "downloads", "download_lists", "network_filter"
        })
        config.sections["transfers"]["downloaddir"] = DATA_FOLDER_PATH
        config.sections["transfers"]["incompletedir"] = DATA_FOLDER_PATH
        core.start()

    def tearDown(self):
        core.quit()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(DATA_FOLDER_PATH)

    @staticmethod
    def _make_response(token, username, files, freeulslots=True, inqueue=0):

        msg = FileSearchResponse(
            search_username=username, token=token, shares=files,
            freeulslots=freeulslots, ulspeed=100, inqueue=inqueue
        )
        msg.username = username
        msg.addr = ("127.0.0.1", 1234)
        return msg

    def test_add_list_and_items(self):
        """Adding a list and bulk items works, and duplicates are ignored."""

        download_list = core.download_lists.add_list("My Playlist", auto_download=False)

        self.assertIsNotNone(download_list)
        self.assertEqual(download_list.name, "My Playlist")
        self.assertIn("My Playlist", core.download_lists.lists)

        core.download_lists.add_list_items("My Playlist", ["Song One", "Song Two", "Song One", "  ", "Song Three"])

        self.assertEqual(len(download_list.items), 3)
        self.assertIn("Song One", download_list.items)
        self.assertIn("Song Two", download_list.items)
        self.assertIn("Song Three", download_list.items)

        for item in download_list.items.values():
            self.assertEqual(item.status, DownloadListItemStatus.PENDING)

    def test_add_list_duplicate_name(self):
        """Adding a list with a name that's already taken is a no-op."""

        core.download_lists.add_list("Duplicate")
        result = core.download_lists.add_list("Duplicate")

        self.assertIsNone(result)
        self.assertEqual(len(core.download_lists.lists), 1)

    def test_remove_list(self):

        core.download_lists.add_list("Temp List", auto_download=False)
        core.download_lists.add_list_items("Temp List", ["Some Song"])
        core.download_lists.remove_list("Temp List")

        self.assertNotIn("Temp List", core.download_lists.lists)

    def test_remove_list_item(self):

        download_list = core.download_lists.add_list("List A", auto_download=False)
        core.download_lists.add_list_items("List A", ["Keep This", "Remove This"])

        core.download_lists.remove_list_item("List A", "Remove This")

        self.assertNotIn("Remove This", download_list.items)
        self.assertIn("Keep This", download_list.items)

    def test_rename_list(self):

        core.download_lists.add_list("Old Name", auto_download=False)
        core.download_lists.add_list_items("Old Name", ["A Song"])

        result = core.download_lists.rename_list("Old Name", "New Name")

        self.assertTrue(result)
        self.assertNotIn("Old Name", core.download_lists.lists)
        self.assertIn("New Name", core.download_lists.lists)
        self.assertEqual(core.download_lists.lists["New Name"].items["A Song"].list_name, "New Name")

    def test_rename_list_conflict(self):

        core.download_lists.add_list("List One")
        core.download_lists.add_list("List Two")

        result = core.download_lists.rename_list("List One", "List Two")

        self.assertFalse(result)
        self.assertIn("List One", core.download_lists.lists)

    def test_update_list_settings(self):

        download_list = core.download_lists.add_list("Settings List", quality="any", fuzzy_match_threshold=70)

        core.download_lists.update_list_settings(
            "Settings List", quality="lossless", prefer_longer=False, fuzzy_match_threshold=90,
            download_folder_path="/tmp/my-folder"
        )

        self.assertEqual(download_list.quality, "lossless")
        self.assertFalse(download_list.prefer_longer)
        self.assertEqual(download_list.fuzzy_match_threshold, 90)
        self.assertEqual(download_list.download_folder_path, "/tmp/my-folder")

    def test_term_variants_strip_bracketed_content(self):

        variants = core.download_lists._get_term_variants("Artist - Song Title (Radio Edit)")

        self.assertEqual(variants[0], "Artist - Song Title (Radio Edit)")
        self.assertIn("Artist - Song Title", variants)

    def test_term_variants_strip_featured_artist(self):

        variants = core.download_lists._get_term_variants("Artist1 feat. Artist2 - Song Title")

        self.assertIn("Artist1 - Song Title", variants)

    def test_term_variants_strip_extra_artists(self):

        variants = core.download_lists._get_term_variants("Artist1 & Artist2 - Song Title")

        self.assertIn("Artist1 - Song Title", variants)

    def test_term_variants_no_change_for_simple_term(self):

        variants = core.download_lists._get_term_variants("Solo Artist - Just A Title")

        self.assertEqual(variants, ["Solo Artist - Just A Title"])

    def test_quality_preference(self):

        meets = core.download_lists._meets_quality_preference

        self.assertTrue(meets("any", is_lossless=False, bitrate=64))
        self.assertFalse(meets("good", is_lossless=False, bitrate=128))
        self.assertTrue(meets("good", is_lossless=False, bitrate=192))
        self.assertTrue(meets("good", is_lossless=True, bitrate=0))
        self.assertFalse(meets("high", is_lossless=False, bitrate=192))
        self.assertTrue(meets("high", is_lossless=False, bitrate=320))
        self.assertFalse(meets("lossless", is_lossless=False, bitrate=320))
        self.assertTrue(meets("lossless", is_lossless=True, bitrate=0))

    def test_match_percentage(self):

        match = core.download_lists._match_percentage

        self.assertEqual(match([], "anything"), 100.0)
        self.assertEqual(match(["artist", "song"], "artist - song.mp3"), 100.0)
        self.assertEqual(match(["artist", "song"], "artist - other.mp3"), 50.0)
        self.assertEqual(match(["artist", "song"], "unrelated.mp3"), 0.0)

    def test_save_and_load_round_trip(self):
        """Verify a list with items survives a save + reload cycle."""

        download_list = core.download_lists.add_list(
            "Persisted List", download_folder_path="/tmp/persisted", quality="high",
            prefer_longer=False, fuzzy_match_threshold=80, auto_download=False
        )
        core.download_lists.add_list_items("Persisted List", ["First Song", "Second Song"])

        core.download_lists._save()
        core.download_lists.lists.clear()
        core.download_lists._load()

        self.assertIn("Persisted List", core.download_lists.lists)
        reloaded = core.download_lists.lists["Persisted List"]

        self.assertEqual(reloaded.download_folder_path, "/tmp/persisted")
        self.assertEqual(reloaded.quality, "high")
        self.assertFalse(reloaded.prefer_longer)
        self.assertEqual(reloaded.fuzzy_match_threshold, 80)
        self.assertEqual(len(reloaded.items), 2)
        self.assertIn("First Song", reloaded.items)

    def test_end_to_end_search_and_download(self):
        """Simulate a full item lifecycle: dispatch -> search response ->
        finalize -> download completes -> list marked complete."""

        download_list = core.download_lists.add_list(
            "Live List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Live List", ["Cool Artist - Great Song"])

        item = download_list.items["Cool Artist - Great Song"]

        # Simulate the dispatch that would normally happen via the pacing queue
        core.download_lists._dispatch_item(download_list, item)

        self.assertEqual(item.status, DownloadListItemStatus.SEARCHING)
        self.assertIsNotNone(item.token)

        # Simulate an incoming search response with a matching audio file
        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Cool Artist\\Cool Artist - Great Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)

        self.assertEqual(len(item.download_candidates), 1)
        self.assertIsNotNone(item.collect_timer_id)

        # Fire the collection timer immediately instead of waiting
        core.download_lists._finalize_item("Live List", "Cool Artist - Great Song")

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertEqual(item.download_username, "someuser")
        self.assertTrue(item.download_virtual_path.endswith("Great Song.mp3"))

        transfer = core.downloads.transfers.get("someuser" + item.download_virtual_path)
        self.assertIsNotNone(transfer)
        self.assertEqual(transfer.folder_path, DATA_FOLDER_PATH)

        # Simulate the transfer finishing
        from pynicotine.transfers import TransferStatus
        transfer.status = TransferStatus.FINISHED
        core.download_lists._update_download(transfer, True)

        self.assertEqual(item.status, DownloadListItemStatus.COMPLETED)
        self.assertTrue(download_list.is_complete)

        rows = core.download_lists.get_summary_rows("Live List")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["term"], "Cool Artist - Great Song")
        self.assertTrue(rows[0]["downloaded_file"].endswith("Great Song.mp3"))
        self.assertEqual(rows[0]["user"], "someuser")

    def test_low_quality_candidate_is_rejected(self):
        """A candidate below the list's quality preference should not be
        picked as a download candidate."""

        download_list = core.download_lists.add_list(
            "Strict List", quality="lossless", fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items("Strict List", ["Some Song"])

        item = download_list.items["Some Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=128, length=200, vbr=0)
        files = [(1, "@@abc\\Some Song.mp3", 4000000, "mp3", attributes)]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)

        self.assertEqual(item.download_candidates, [])

    def test_low_match_candidate_is_rejected(self):
        """A candidate that doesn't textually resemble the search term
        should be rejected by the fuzzy match threshold."""

        download_list = core.download_lists.add_list(
            "Fuzzy List", quality="any", fuzzy_match_threshold=90, auto_download=True)
        core.download_lists.add_list_items("Fuzzy List", ["Correct Artist - Correct Song"])

        item = download_list.items["Correct Artist - Correct Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Totally Different Track.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)

        self.assertEqual(item.download_candidates, [])

    # Watch Folder #

    def _set_up_watch_folder(self, enabled=True):

        watch_folder_path = os.path.join(DATA_FOLDER_PATH, "watch")

        if os.path.exists(watch_folder_path):
            shutil.rmtree(watch_folder_path)

        os.makedirs(watch_folder_path)

        config.sections["transfers"]["downloadlistwatchenabled"] = enabled
        config.sections["transfers"]["downloadlistwatchfolder"] = watch_folder_path

        return watch_folder_path

    @staticmethod
    def _write_watch_file(folder_path, basename, contents, encoding="utf-8"):

        file_path = os.path.join(folder_path, basename)

        with open(file_path, "w", encoding=encoding, newline="") as handle:
            handle.write(contents)

        return file_path

    def test_watch_folder_imports_after_file_settles(self):
        """A file is only imported once its size and mtime are unchanged between
        two consecutive scans, so partially written files are never read."""

        watch_folder_path = self._set_up_watch_folder()
        self._write_watch_file(watch_folder_path, "Spotify Missing.txt", "Artist One - Song One\n")

        # First scan only records a snapshot, it must not import yet
        core.download_lists._scan_watch_folder()
        self.assertNotIn("Spotify Missing", core.download_lists.lists)
        self.assertTrue(os.path.isfile(os.path.join(watch_folder_path, "Spotify Missing.txt")))

        # Second scan sees an unchanged file and imports it
        core.download_lists._scan_watch_folder()

        self.assertIn("Spotify Missing", core.download_lists.lists)
        self.assertIn("Artist One - Song One", core.download_lists.lists["Spotify Missing"].items)

    def test_watch_folder_moves_imported_file(self):
        """An imported file is moved into the 'imported' subfolder so it is not read twice."""

        watch_folder_path = self._set_up_watch_folder()
        self._write_watch_file(watch_folder_path, "Wanted.txt", "Artist - Song\n")

        core.download_lists._scan_watch_folder()
        core.download_lists._scan_watch_folder()

        self.assertFalse(os.path.isfile(os.path.join(watch_folder_path, "Wanted.txt")))
        self.assertTrue(os.path.isfile(os.path.join(watch_folder_path, "imported", "Wanted.txt")))

    def test_watch_folder_imported_name_collision(self):
        """Re-importing a file with the same name must not overwrite the previous one."""

        watch_folder_path = self._set_up_watch_folder()

        for _unused in range(2):
            self._write_watch_file(watch_folder_path, "Wanted.txt", "Artist - Song\n")
            core.download_lists._scan_watch_folder()
            core.download_lists._scan_watch_folder()

        imported_folder_path = os.path.join(watch_folder_path, "imported")

        self.assertTrue(os.path.isfile(os.path.join(imported_folder_path, "Wanted.txt")))
        self.assertTrue(os.path.isfile(os.path.join(imported_folder_path, "Wanted (1).txt")))

    def test_watch_folder_parses_bom_crlf_blanks_and_comments(self):
        """A BOM, CRLF line endings, blank lines and comments must not corrupt terms."""

        watch_folder_path = self._set_up_watch_folder()
        self._write_watch_file(
            watch_folder_path, "Messy.txt",
            "# exported by another app\r\nArtist One - Song One\r\n\r\n  Artist Two - Song Two  \r\n",
            encoding="utf-8-sig"
        )

        core.download_lists._scan_watch_folder()
        core.download_lists._scan_watch_folder()

        terms = list(core.download_lists.lists["Messy"].items)

        self.assertEqual(terms, ["Artist One - Song One", "Artist Two - Song Two"])

    def test_watch_folder_appends_to_existing_list(self):
        """Importing a file whose name matches an existing list adds to that list,
        and duplicate terms are not added twice."""

        watch_folder_path = self._set_up_watch_folder()
        core.download_lists.add_list("Wanted", auto_download=False)
        core.download_lists.add_list_items("Wanted", ["Artist - Existing Song"])

        self._write_watch_file(
            watch_folder_path, "Wanted.txt", "Artist - Existing Song\nArtist - New Song\n")

        core.download_lists._scan_watch_folder()
        core.download_lists._scan_watch_folder()

        terms = list(core.download_lists.lists["Wanted"].items)

        self.assertEqual(terms, ["Artist - Existing Song", "Artist - New Song"])

    def test_watch_folder_disabled_is_a_no_op(self):
        """Nothing is imported while the watch folder is disabled."""

        watch_folder_path = self._set_up_watch_folder(enabled=False)
        self._write_watch_file(watch_folder_path, "Wanted.txt", "Artist - Song\n")

        core.download_lists._scan_watch_folder()
        core.download_lists._scan_watch_folder()

        self.assertNotIn("Wanted", core.download_lists.lists)
        self.assertTrue(os.path.isfile(os.path.join(watch_folder_path, "Wanted.txt")))

    def test_watch_folder_ignores_non_list_files(self):
        """Files that are not song lists are left alone."""

        watch_folder_path = self._set_up_watch_folder()
        self._write_watch_file(watch_folder_path, "cover.jpg", "not a song list")

        core.download_lists._scan_watch_folder()
        core.download_lists._scan_watch_folder()

        self.assertEqual(core.download_lists.lists, {})
        self.assertTrue(os.path.isfile(os.path.join(watch_folder_path, "cover.jpg")))

    def test_watch_folder_missing_folder_is_handled(self):
        """A watch folder that does not exist must not raise."""

        config.sections["transfers"]["downloadlistwatchenabled"] = True
        config.sections["transfers"]["downloadlistwatchfolder"] = os.path.join(
            DATA_FOLDER_PATH, "does_not_exist")

        core.download_lists._scan_watch_folder()

        self.assertEqual(core.download_lists.lists, {})
