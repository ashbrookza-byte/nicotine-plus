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

    def test_pump_queue_respects_max_concurrent_downloads(self):
        """Only up to max_concurrent_downloads items are dispatched (moved out
        of Pending) at once, even with a much bigger backlog waiting; the rest
        stay queued until something frees up."""

        from pynicotine.slskmessages import UserStatus

        core.users.login_status = UserStatus.ONLINE
        self.addCleanup(setattr, core.users, "login_status", UserStatus.OFFLINE)

        core.download_lists.update_max_concurrent_downloads(2)
        self.addCleanup(core.download_lists.update_max_concurrent_downloads, 3)

        download_list = core.download_lists.add_list("Concurrency List", auto_download=True)
        core.download_lists.add_list_items(
            "Concurrency List", ["Song One", "Song Two", "Song Three", "Song Four", "Song Five"])

        # add_list_items() already kicked the queue, but a scheduled retry left over
        # from _start()'s own (offline, at the time) load-time kick can still be
        # pending, which _kick_queue()'s already-scheduled guard would otherwise
        # skip; pump directly for a deterministic result
        core.download_lists._pump_queue()

        dispatched = sum(
            1 for item in download_list.items.values()
            if item.status == DownloadListItemStatus.SEARCHING)
        pending = sum(
            1 for item in download_list.items.values()
            if item.status == DownloadListItemStatus.PENDING)

        self.assertEqual(dispatched, 2)
        self.assertEqual(pending, 3)
        self.assertEqual(core.download_lists._count_active_items(), 2)

    def test_pinned_list_is_dispatched_ahead_of_others(self):
        """A pinned list's queued item is dispatched before an earlier-queued
        item from a plain, unpinned list."""

        from pynicotine.slskmessages import UserStatus

        core.users.login_status = UserStatus.ONLINE
        self.addCleanup(setattr, core.users, "login_status", UserStatus.OFFLINE)

        core.download_lists.update_max_concurrent_downloads(1)
        self.addCleanup(core.download_lists.update_max_concurrent_downloads, 3)

        # Queued first, but not pinned
        plain_list = core.download_lists.add_list("Plain List", auto_download=True)
        core.download_lists.add_list_items("Plain List", ["Plain Song"])

        # Queued second, but pinned -- should still go first
        pinned_list = core.download_lists.add_list("Pinned List", auto_download=True)
        core.download_lists.set_list_pinned("Pinned List", True)
        core.download_lists.add_list_items("Pinned List", ["Pinned Song"])

        core.download_lists._pump_queue()

        self.assertEqual(pinned_list.items["Pinned Song"].status, DownloadListItemStatus.SEARCHING)
        self.assertEqual(plain_list.items["Plain Song"].status, DownloadListItemStatus.PENDING)

    def test_set_list_pinned_persists_and_toggles(self):

        download_list = core.download_lists.add_list("Toggle Pin List")
        self.assertFalse(download_list.pinned)

        core.download_lists.set_list_pinned("Toggle Pin List", True)
        self.assertTrue(download_list.pinned)

        core.download_lists.set_list_pinned("Toggle Pin List", False)
        self.assertFalse(download_list.pinned)

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

    def test_rename_list_moves_name_subfolder(self):
        """Renaming a list that saves into a subfolder named after itself must
        move that folder (and its already-downloaded files) to match, rather
        than leaving them behind under the old name."""

        base_folder_path = os.path.join(DATA_FOLDER_PATH, "renamed_list_downloads")
        old_folder_path = os.path.join(base_folder_path, "Old Chart Name")
        os.makedirs(old_folder_path, exist_ok=True)

        with open(os.path.join(old_folder_path, "Already Downloaded.mp3"), "w", encoding="utf-8") as handle:
            handle.write("not really audio, just needs to exist")

        core.download_lists.add_list(
            "Old Chart Name", download_folder_path=base_folder_path, use_name_subfolder=True)

        result = core.download_lists.rename_list("Old Chart Name", "New Chart Name")
        self.assertTrue(result)

        new_folder_path = os.path.join(base_folder_path, "New Chart Name")

        self.assertFalse(os.path.exists(old_folder_path))
        self.assertTrue(os.path.isfile(os.path.join(new_folder_path, "Already Downloaded.mp3")))

        download_list = core.download_lists.lists["New Chart Name"]
        self.assertEqual(download_list.effective_download_folder_path, new_folder_path)

    def test_rename_list_merges_into_existing_destination_folder(self):
        """If a folder for the new name already exists (e.g. left over from an
        earlier list), renaming must merge into it rather than failing or
        overwriting anything, keeping files from both."""

        base_folder_path = os.path.join(DATA_FOLDER_PATH, "renamed_list_merge")
        old_folder_path = os.path.join(base_folder_path, "Old Merge Name")
        new_folder_path = os.path.join(base_folder_path, "New Merge Name")
        os.makedirs(old_folder_path, exist_ok=True)
        os.makedirs(new_folder_path, exist_ok=True)

        with open(os.path.join(old_folder_path, "From Old.mp3"), "w", encoding="utf-8") as handle:
            handle.write("old")

        with open(os.path.join(new_folder_path, "Already Here.mp3"), "w", encoding="utf-8") as handle:
            handle.write("pre-existing")

        core.download_lists.add_list(
            "Old Merge Name", download_folder_path=base_folder_path, use_name_subfolder=True)

        result = core.download_lists.rename_list("Old Merge Name", "New Merge Name")
        self.assertTrue(result)

        self.assertFalse(os.path.exists(old_folder_path))
        self.assertTrue(os.path.isfile(os.path.join(new_folder_path, "From Old.mp3")))
        self.assertTrue(os.path.isfile(os.path.join(new_folder_path, "Already Here.mp3")))

    def test_rename_list_without_name_subfolder_leaves_folder_untouched(self):
        """A list using an explicit, fixed download folder (not one named after
        itself) shouldn't have anything on disk touched by a rename."""

        fixed_folder_path = os.path.join(DATA_FOLDER_PATH, "fixed_folder_untouched")
        os.makedirs(fixed_folder_path, exist_ok=True)

        core.download_lists.add_list("Old Fixed Name", download_folder_path=fixed_folder_path)

        result = core.download_lists.rename_list("Old Fixed Name", "New Fixed Name")

        self.assertTrue(result)
        self.assertTrue(os.path.isdir(fixed_folder_path))

        download_list = core.download_lists.lists["New Fixed Name"]
        self.assertEqual(download_list.effective_download_folder_path, fixed_folder_path)

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

    def _restore_wishlist_defaults(self):
        """Undo update_wishlist_default_settings side effects so later tests in
        this run aren't affected by config.sections being process-global."""

        defaults = config.defaults["transfers"]
        core.download_lists.update_wishlist_default_settings(
            quality=defaults["downloadlistdefaultquality"],
            prefer_longer=defaults["downloadlistdefaultpreferlonger"],
            prefer_lossless=defaults["downloadlistdefaultpreferlossless"],
            preferred_keywords=defaults["downloadlistdefaultkeywords"],
            fuzzy_match_threshold=defaults["downloadlistdefaultfuzzy"],
            auto_download=defaults["downloadlistdefaultautodownload"],
            use_name_subfolder=defaults["downloadlistdefaultnamesubfolder"]
        )

    def test_list_inherits_overall_defaults(self):
        """A list with no overrides of its own follows the overall wishlist defaults,
        and picks up later changes to them."""

        self.addCleanup(self._restore_wishlist_defaults)
        download_list = core.download_lists.add_list("Inheriting List")

        self.assertEqual(download_list.effective_quality, config.defaults["transfers"]["downloadlistdefaultquality"])
        self.assertTrue(download_list.effective_auto_download)

        core.download_lists.update_wishlist_default_settings(
            quality="lossless", prefer_longer=False, prefer_lossless=False, preferred_keywords="beatport",
            fuzzy_match_threshold=55, auto_download=False, use_name_subfolder=True
        )

        self.assertEqual(download_list.effective_quality, "lossless")
        self.assertFalse(download_list.effective_prefer_longer)
        self.assertEqual(download_list.effective_preferred_keywords, "beatport")
        self.assertEqual(download_list.effective_fuzzy_match_threshold, 55)
        self.assertFalse(download_list.effective_auto_download)
        self.assertTrue(download_list.effective_use_name_subfolder)

    def test_list_override_survives_default_changes(self):
        """A list with its own override for a setting keeps it regardless of
        later changes to the overall default."""

        self.addCleanup(self._restore_wishlist_defaults)
        download_list = core.download_lists.add_list("Overriding List")
        core.download_lists.update_list_settings("Overriding List", quality="high")

        core.download_lists.update_wishlist_default_settings(
            quality="lossless", prefer_longer=True, prefer_lossless=True, preferred_keywords="",
            fuzzy_match_threshold=70, auto_download=True, use_name_subfolder=False
        )

        self.assertEqual(download_list.effective_quality, "high")

    def test_update_list_settings_explicit_none_clears_override(self):
        """Passing an explicit None (as opposed to omitting the argument) clears
        this list's override, reverting it to the overall default."""

        core.download_lists.update_wishlist_default_settings(
            quality="good", prefer_longer=True, prefer_lossless=True, preferred_keywords="",
            fuzzy_match_threshold=70, auto_download=True, use_name_subfolder=False
        )

        download_list = core.download_lists.add_list("Clearable List", quality="lossless")
        self.assertEqual(download_list.quality, "lossless")

        core.download_lists.update_list_settings("Clearable List", quality=None)

        self.assertIsNone(download_list.quality)
        self.assertEqual(download_list.effective_quality, "good")

    def test_reset_mid_download_cancels_the_old_transfer(self):
        """Resetting an item that's actively downloading must actually cancel
        that transfer, not just stop tracking it — otherwise it keeps running
        in the background, finishes on its own, and the file lands on disk
        while the item has already moved on to a new search."""

        from pynicotine.transfers import TransferStatus

        download_list = core.download_lists.add_list(
            "Reset Mid Download List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Reset Mid Download List", ["Reset Me Song"])

        item = download_list.items["Reset Me Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Reset Me Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "resetuser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Reset Mid Download List", "Reset Me Song")

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)

        transfer_key = "resetuser" + item.download_virtual_path
        transfer = core.downloads.transfers.get(transfer_key)
        self.assertIsNotNone(transfer)
        transfer.status = TransferStatus.TRANSFERRING

        core.download_lists.reset_list_item("Reset Mid Download List", "Reset Me Song")

        self.assertEqual(item.status, DownloadListItemStatus.PENDING)
        # The old transfer must be gone, not left running unabandoned in the background
        self.assertIsNone(core.downloads.transfers.get(transfer_key))
        self.assertIsNone(item.download_match_percentage)

    def test_finalize_item_records_the_winning_candidates_match_percentage(self):
        """The chosen candidate's match percentage (how many of the original
        term's words its path actually contained) is stored on the item."""

        download_list = core.download_lists.add_list(
            "Match List", quality="any", fuzzy_match_threshold=40, auto_download=True)
        core.download_lists.add_list_items("Match List", ["Some Artist Full Title Here"])

        item = download_list.items["Some Artist Full Title Here"]
        core.download_lists._dispatch_item(download_list, item)

        # Path only contains "some", "artist", "here" out of 5 term words -> 60%
        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Some Artist - Here.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Match List", "Some Artist Full Title Here")

        self.assertEqual(item.download_match_percentage, 60)
        self.assertEqual(item.h_match_percentage, "60%")

    def test_reset_of_already_finished_download_marks_it_completed(self):
        """If the transfer backing a Downloading item already finished by the
        time Reset is clicked, the reset must recognize that and mark the item
        Completed — not discard a download that, in fact, already succeeded
        just because it hadn't been processed as Completed yet."""

        from pynicotine.transfers import TransferStatus

        download_list = core.download_lists.add_list(
            "Reset After Finish List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Reset After Finish List", ["Already Done Song"])

        item = download_list.items["Already Done Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Already Done Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "finisheduser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Reset After Finish List", "Already Done Song")

        transfer_key = "finisheduser" + item.download_virtual_path
        transfer = core.downloads.transfers.get(transfer_key)
        self.assertIsNotNone(transfer)
        transfer.status = TransferStatus.FINISHED

        core.download_lists.reset_list_item("Reset After Finish List", "Already Done Song")

        self.assertEqual(item.status, DownloadListItemStatus.COMPLETED)
        self.assertEqual(item.download_percent, 100)

    def test_reset_reconciles_completion_even_with_an_orphaned_transfer_map(self):
        """Same as above, but even when the _transfer_map entry linking the
        item to its transfer is already missing (e.g. left over from an
        earlier, unrelated bug) — reconciliation must work directly from the
        transfer itself (found via the item's own recorded username/virtual
        path), not depend on that mapping still being intact."""

        from pynicotine.transfers import TransferStatus

        download_list = core.download_lists.add_list(
            "Orphaned Map List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Orphaned Map List", ["Orphaned Song"])

        item = download_list.items["Orphaned Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Orphaned Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "orphaneduser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Orphaned Map List", "Orphaned Song")

        transfer_key = "orphaneduser" + item.download_virtual_path
        transfer = core.downloads.transfers.get(transfer_key)
        self.assertIsNotNone(transfer)
        transfer.status = TransferStatus.FINISHED

        # Simulate the orphaning: the map entry is gone, but the item still
        # points at a real, finished transfer
        core.download_lists._transfer_map.pop(transfer_key, None)

        core.download_lists.reset_list_item("Orphaned Map List", "Orphaned Song")

        self.assertEqual(item.status, DownloadListItemStatus.COMPLETED)

    def test_pause_and_resume_list(self):
        """Pausing marks a list inactive; resuming re-queues anything still pending,
        even if it was never removed from the queue while paused."""

        download_list = core.download_lists.add_list("Pausable List")
        core.download_lists.add_list_items("Pausable List", ["Artist - Song"])

        core.download_lists.pause_list("Pausable List")
        self.assertFalse(download_list.effective_auto_download)

        # Simulate the queue having drained while paused (e.g. _pump_queue skipped
        # this now-paused item), so resuming has to put it back itself
        core.download_lists._queue.clear()

        core.download_lists.resume_list("Pausable List")

        self.assertTrue(download_list.effective_auto_download)
        self.assertIn(("Pausable List", "Artist - Song"), core.download_lists._queue)

    def test_start_item_next_moves_pending_item_to_front_of_queue(self):
        """A pending item is moved to the front of the dispatch queue, ahead of
        items that were already queued before it."""

        download_list = core.download_lists.add_list("Priority List", auto_download=True)
        core.download_lists.add_list_items("Priority List", ["First Song", "Second Song", "Third Song"])

        core.download_lists.start_item_next("Priority List", "Third Song")

        self.assertEqual(core.download_lists._queue[0], ("Priority List", "Third Song"))
        # Not duplicated: still only one entry for it in the queue
        self.assertEqual(
            list(core.download_lists._queue).count(("Priority List", "Third Song")), 1)

    def test_start_item_next_resets_a_not_found_item_first(self):
        """An item that isn't pending (e.g. Not Found) is reset before being
        placed at the front of the queue."""

        download_list = core.download_lists.add_list("Priority List", auto_download=True)
        core.download_lists.add_list_items("Priority List", ["Missing Song"])

        item = download_list.items["Missing Song"]
        item.status = DownloadListItemStatus.NOT_FOUND
        core.download_lists._queue.clear()

        core.download_lists.start_item_next("Priority List", "Missing Song")

        self.assertEqual(item.status, DownloadListItemStatus.PENDING)
        self.assertEqual(core.download_lists._queue[0], ("Priority List", "Missing Song"))

    def test_start_item_next_is_a_no_op_for_active_item(self):
        """An item that's already searching or downloading isn't touched."""

        download_list = core.download_lists.add_list("Priority List", auto_download=True)
        core.download_lists.add_list_items("Priority List", ["Active Song"])

        item = download_list.items["Active Song"]
        core.download_lists._dispatch_item(download_list, item)
        self.assertEqual(item.status, DownloadListItemStatus.SEARCHING)

        # _dispatch_item() doesn't remove the queue entry itself (only _pump_queue()
        # does, when it pops and dispatches); clear it so the assertion below only
        # reflects what start_item_next() itself did, or rather didn't do
        core.download_lists._queue.clear()

        core.download_lists.start_item_next("Priority List", "Active Song")

        self.assertEqual(item.status, DownloadListItemStatus.SEARCHING)
        self.assertNotIn(("Priority List", "Active Song"), core.download_lists._queue)

    def test_start_item_next_is_a_no_op_for_paused_list(self):
        """Nothing is queued for a list that isn't currently auto-downloading."""

        download_list = core.download_lists.add_list("Paused Priority List", auto_download=False)
        core.download_lists.add_list_items("Paused Priority List", ["Some Song"])
        core.download_lists._queue.clear()

        core.download_lists.start_item_next("Paused Priority List", "Some Song")

        self.assertNotIn(("Paused Priority List", "Some Song"), core.download_lists._queue)

    def test_effective_download_folder_path_with_name_subfolder(self):
        """When "save into a subfolder named after this list" is on, the effective
        download folder is the base folder plus a subfolder named after the list."""

        download_list = core.download_lists.add_list(
            "My Chart", download_folder_path="/tmp/downloads", use_name_subfolder=True)

        self.assertEqual(download_list.effective_download_folder_path, "/tmp/downloads/My Chart")

    def test_prefer_lossless_affects_candidate_score_but_not_eligibility(self):
        """Preferring lossless breaks ties in favor of a lossless candidate, but
        (unlike a "Lossless only" quality requirement) doesn't reject lossy ones."""

        download_list = core.download_lists.add_list(
            "Lossless Preferring List", quality="any", fuzzy_match_threshold=50,
            auto_download=True, prefer_lossless=True)
        core.download_lists.add_list_items("Lossless Preferring List", ["Some Song"])

        item = download_list.items["Some Song"]
        core.download_lists._dispatch_item(download_list, item)

        lossy_attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        lossless_attributes = FileAttributes(bitrate=None, length=200, bit_depth=16)
        files = [
            (1, "@@abc\\Some Song (lossy).mp3", 8000000, "mp3", lossy_attributes),
            (1, "@@abc\\Some Song (lossless).flac", 20000000, "flac", lossless_attributes),
        ]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)

        # Only the best-scoring file within a single response is kept as a candidate,
        # and lossless should win the tie-break over the (otherwise equal) lossy file
        self.assertEqual(len(item.download_candidates), 1)
        _score, _username, virtual_path, _size, _attributes = item.download_candidates[0]
        self.assertIn("lossless", virtual_path)

    def test_prefer_lossless_off_actually_prefers_mp3(self):
        """A 44.1kHz/16-bit FLAC's estimated "bitrate" (~1411) dwarfs a real mp3's
        (<=320), which would silently win the raw bitrate tiebreaker regardless of
        preference if left uncapped. With prefer_lossless off, the mp3 must win —
        turning the preference off should actually mean "prefer mp3", not "no
        preference, let the inflated FLAC number decide"."""

        download_list = core.download_lists.add_list(
            "Mp3 Preferring List", quality="any", fuzzy_match_threshold=50,
            auto_download=True, prefer_lossless=False)
        core.download_lists.add_list_items("Mp3 Preferring List", ["Kasablanca - Time Is A Circle"])

        item = download_list.items["Kasablanca - Time Is A Circle"]
        core.download_lists._dispatch_item(download_list, item)

        lossy_attributes = FileAttributes(bitrate=320, length=258, vbr=0)
        lossless_attributes = FileAttributes(length=258, sample_rate=44100, bit_depth=16)
        files = [
            (1, "@@abc\\Kasablanca - Time Is A Circle.flac", 28100000, "flac", lossless_attributes),
            (1, "@@abc\\Kasablanca - Time Is A Circle (Extended Club Mix).mp3", 13700000, "mp3", lossy_attributes),
        ]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)

        self.assertEqual(len(item.download_candidates), 1)
        _score, _username, virtual_path, _size, _attributes = item.download_candidates[0]
        self.assertTrue(virtual_path.endswith(".mp3"))

    def test_prefer_lossless_on_still_prefers_flac_despite_capped_bitrate(self):
        """Capping the bitrate tiebreaker must not break the normal case: with
        prefer_lossless on, FLAC should still win over mp3."""

        download_list = core.download_lists.add_list(
            "Flac Preferring List", quality="any", fuzzy_match_threshold=50,
            auto_download=True, prefer_lossless=True)
        core.download_lists.add_list_items("Flac Preferring List", ["Kasablanca - Time Is A Circle"])

        item = download_list.items["Kasablanca - Time Is A Circle"]
        core.download_lists._dispatch_item(download_list, item)

        lossy_attributes = FileAttributes(bitrate=320, length=258, vbr=0)
        lossless_attributes = FileAttributes(length=258, sample_rate=44100, bit_depth=16)
        files = [
            (1, "@@abc\\Kasablanca - Time Is A Circle.flac", 28100000, "flac", lossless_attributes),
            (1, "@@abc\\Kasablanca - Time Is A Circle (Extended Club Mix).mp3", 13700000, "mp3", lossy_attributes),
        ]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)

        self.assertEqual(len(item.download_candidates), 1)
        _score, _username, virtual_path, _size, _attributes = item.download_candidates[0]
        self.assertTrue(virtual_path.endswith(".flac"))

    def test_matches_preferred_keywords_uses_word_boundaries(self):
        """"bp" should match a whole "BP" folder/word, not an unrelated substring
        like "bpm128" where "bp" isn't a standalone word."""

        matches = core.download_lists._matches_preferred_keywords

        self.assertTrue(matches("beatport, bp", r"beatport\2025\artist - song.mp3".lower()))
        self.assertTrue(matches("beatport, bp", r"bp sep 2025\artist - song.mp3".lower()))
        self.assertFalse(matches("beatport, bp", r"random pool\artist - song (bpm128).mp3".lower()))
        self.assertFalse(matches("", r"beatport\2025\artist - song.mp3".lower()))
        self.assertFalse(matches(None, r"beatport\2025\artist - song.mp3".lower()))

    def test_preferred_keywords_affects_score_but_not_eligibility(self):
        """A candidate from a non-matching folder is still eligible, just outscored
        by an otherwise-equal candidate from a folder matching a preferred keyword."""

        download_list = core.download_lists.add_list(
            "Beatport Preferring List", quality="any", fuzzy_match_threshold=50,
            auto_download=True, preferred_keywords="beatport, bp")
        core.download_lists.add_list_items("Beatport Preferring List", ["Some Song"])

        item = download_list.items["Some Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [
            (1, "@@abc\\Random Pool\\Some Song.mp3", 8000000, "mp3", attributes),
            (1, "@@abc\\BEATPORT 2025\\Some Song.mp3", 8000000, "mp3", attributes),
        ]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)

        self.assertEqual(len(item.download_candidates), 1)
        _score, _username, virtual_path, _size, _attributes = item.download_candidates[0]
        self.assertIn("BEATPORT", virtual_path)

    def test_apply_to_existing_lists_clears_overrides(self):
        """apply_to_existing_lists=True clears every list's own override for the
        matching/download settings, switching them all to the new defaults."""

        self.addCleanup(self._restore_wishlist_defaults)

        download_list = core.download_lists.add_list("Overridden List", quality="lossless", auto_download=False)
        self.assertEqual(download_list.quality, "lossless")

        core.download_lists.update_wishlist_default_settings(
            quality="good", prefer_longer=True, prefer_lossless=True, preferred_keywords="",
            fuzzy_match_threshold=70, auto_download=True, use_name_subfolder=False, apply_to_existing_lists=True
        )

        self.assertIsNone(download_list.quality)
        self.assertIsNone(download_list.auto_download)
        self.assertEqual(download_list.effective_quality, "good")
        self.assertTrue(download_list.effective_auto_download)

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
        """A plain "Artist - Title" term with nothing to strip still gets the
        artist-only and title-only fallbacks appended (see
        test_term_variants_artist_and_title_only_fallback)."""

        variants = core.download_lists._get_term_variants("Solo Artist - Just A Title")

        self.assertEqual(
            variants, ["Solo Artist - Just A Title", "Solo Artist", "Just A Title"])

    def test_term_variants_artist_and_title_only_fallback(self):
        """A combined "artist title" search can come back with fewer/no results
        the same way it sometimes does manually, while just the artist name —
        or just the title — often finds plenty from peers whose tags don't line
        up neatly with a multi-word query. Which one actually works varies by
        track, so both are tried. Candidates are still filtered against every
        word of the original full term regardless (see _file_search_response),
        so this can only narrow results further, never accept a wrong one."""

        variants = core.download_lists._get_term_variants("Tinlicker - Melancholia")

        self.assertEqual(variants, ["Tinlicker - Melancholia", "Tinlicker", "Melancholia"])

    def test_term_variants_no_artist_only_fallback_without_separator(self):
        """A term with no "Artist - Title" separator has nothing to fall back to."""

        variants = core.download_lists._get_term_variants("JustOneWordTitle")

        self.assertEqual(variants, ["JustOneWordTitle"])

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
        self.assertEqual(item.download_match_percentage, 100)
        self.assertEqual(item.h_match_percentage, "100%")

        transfer = core.downloads.transfers.get("someuser" + item.download_virtual_path)
        self.assertIsNotNone(transfer)
        self.assertEqual(transfer.folder_path, DATA_FOLDER_PATH)
        self.assertEqual(item.download_percent, 0)

        # Simulate a progress update partway through the transfer
        from pynicotine.transfers import TransferStatus
        transfer.status = TransferStatus.TRANSFERRING
        transfer.current_byte_offset = 4000000
        transfer.size = 8000000
        core.download_lists._update_download(transfer, True)

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertEqual(item.download_percent, 50)

        # Simulate the transfer finishing
        transfer.status = TransferStatus.FINISHED
        core.download_lists._update_download(transfer, True)

        self.assertEqual(item.status, DownloadListItemStatus.COMPLETED)
        self.assertEqual(item.download_percent, 100)
        self.assertTrue(download_list.is_complete)

        rows = core.download_lists.get_summary_rows("Live List")
        self.assertEqual(len(rows), 1)

    def test_single_variant_term_is_not_marked_not_found_prematurely(self):
        """A term with no "Artist - Title" separator (so no bracket/feat/extra-artist
        clause to strip, and no artist-only fallback to fall back to) has only one
        variant, so it exhausts it on the very first escalation attempt. That must
        not immediately mark the item Not Found — only reaching the full
        SEARCH_TIMEOUT with zero candidates should."""

        import time

        download_list = core.download_lists.add_list("Escalation List", auto_download=True)
        core.download_lists.add_list_items("Escalation List", ["Melancholia"])

        item = download_list.items["Melancholia"]
        core.download_lists._dispatch_item(download_list, item)

        variants = core.download_lists._get_term_variants(item.term)
        self.assertEqual(variants, ["Melancholia"])

        # First escalation attempt: the single variant is immediately exhausted, but
        # since we're nowhere near SEARCH_TIMEOUT yet, the item must keep searching —
        # and, crucially, actually re-issue the search rather than just waiting
        # silently on the original request (whose visibility on the network is
        # time-limited, so passively waiting longer wouldn't surface new responses)
        from pynicotine.events import events
        from pynicotine.slskmessages import FileSearch

        sent_messages = []

        def capture_message(msg):
            sent_messages.append(msg)

        events.connect("queue-network-message", capture_message)
        try:
            core.download_lists._escalate_item("Escalation List", "Melancholia")
        finally:
            events.disconnect("queue-network-message", capture_message)

        resent_searches = [msg for msg in sent_messages if isinstance(msg, FileSearch)]
        self.assertTrue(resent_searches, "expected the search to be re-issued, not just waited on")
        self.assertEqual(resent_searches[0].token, item.token)

        self.assertEqual(item.status, DownloadListItemStatus.SEARCHING)
        self.assertIsNotNone(item.escalation_timer_id)

        # Simulate SEARCH_TIMEOUT having actually elapsed since dispatch
        item.dispatch_time = time.time() - core.download_lists.SEARCH_TIMEOUT - 1
        core.download_lists._escalate_item("Escalation List", "Melancholia")

        self.assertEqual(item.status, DownloadListItemStatus.NOT_FOUND)

    def test_artist_and_title_only_fallbacks_are_used_during_escalation(self):
        """A term with an "Artist - Title" separator escalates through the
        artist-only, then title-only, fallback before finally being marked
        Not Found, and candidates are still checked against every word of the
        full original term — not just whatever text was actually sent to the
        network — regardless of which fallback found them."""

        import time

        download_list = core.download_lists.add_list("Artist Fallback List", auto_download=True)
        core.download_lists.add_list_items("Artist Fallback List", ["Tinlicker - Melancholia"])

        item = download_list.items["Tinlicker - Melancholia"]
        core.download_lists._dispatch_item(download_list, item)

        # First escalation: falls back to searching "Tinlicker" alone
        core.download_lists._escalate_item("Artist Fallback List", "Tinlicker - Melancholia")

        self.assertEqual(item.status, DownloadListItemStatus.SEARCHING)
        self.assertEqual(item.searched_term, "Tinlicker")

        # A result matching only the artist, not the title, must still be rejected
        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Tinlicker\\Some Other Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "someuser", files)
        core.download_lists._file_search_response(msg)

        self.assertEqual(item.download_candidates, [])

        # Second escalation: falls back to searching "Melancholia" alone
        core.download_lists._escalate_item("Artist Fallback List", "Tinlicker - Melancholia")

        self.assertEqual(item.status, DownloadListItemStatus.SEARCHING)
        self.assertEqual(item.searched_term, "Melancholia")

        # A result matching only the title, not the artist, must still be rejected too
        files = [(1, "@@abc\\Someone Else\\Melancholia.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "otheruser", files)
        core.download_lists._file_search_response(msg)

        self.assertEqual(item.download_candidates, [])

        # Third escalation: variants exhausted, waits out the remaining SEARCH_TIMEOUT
        core.download_lists._escalate_item("Artist Fallback List", "Tinlicker - Melancholia")
        self.assertEqual(item.status, DownloadListItemStatus.SEARCHING)

        item.dispatch_time = time.time() - core.download_lists.SEARCH_TIMEOUT - 1
        core.download_lists._escalate_item("Artist Fallback List", "Tinlicker - Melancholia")

        self.assertEqual(item.status, DownloadListItemStatus.NOT_FOUND)

    def test_stalled_download_is_abandoned_and_requeued(self):
        """A download whose transfer speed never reaches the minimum threshold is
        abandoned once its stall timer fires, and the item is re-queued to search
        for a different source."""

        from pynicotine.transfers import TransferStatus

        download_list = core.download_lists.add_list(
            "Stall List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Stall List", ["Slow Artist - Slow Song"])

        item = download_list.items["Slow Artist - Slow Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Slow Artist\\Slow Artist - Slow Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "slowuser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Stall List", "Slow Artist - Slow Song")

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertIsNotNone(item.stall_timer_id)

        transfer_key = "slowuser" + item.download_virtual_path
        transfer = core.downloads.transfers.get(transfer_key)
        self.assertIsNotNone(transfer)

        # A trickle of progress below the minimum speed shouldn't reset the stall timer
        transfer.status = TransferStatus.TRANSFERRING
        transfer.current_byte_offset = 100
        transfer.speed = 100  # well below min_transfer_speed
        core.download_lists._update_download(transfer, True)

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)

        # Simulate the stall timer firing
        core.download_lists._handle_stalled_download("Stall List", "Slow Artist - Slow Song")

        self.assertEqual(item.status, DownloadListItemStatus.PENDING)
        self.assertEqual(item.download_percent, 0)
        self.assertIsNone(item.download_username)
        self.assertIsNone(core.downloads.transfers.get(transfer_key))
        self.assertIn(("Stall List", "Slow Artist - Slow Song"), core.download_lists._queue)

    def test_fully_received_transfer_disarms_stall_watchdog(self):
        """Once every byte has arrived, the transfer is just finalizing (moving
        out of the incomplete folder, etc.) — which can legitimately take a
        while and isn't a stall, even though speed often drops to 0 there. The
        watchdog must not fire and abort an already-finished download out from
        under itself before the FINISHED status arrives."""

        from pynicotine.transfers import TransferStatus

        download_list = core.download_lists.add_list(
            "Finalizing List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Finalizing List", ["Almost Done Song"])

        item = download_list.items["Almost Done Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Almost Done Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "finishinguser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Finalizing List", "Almost Done Song")

        transfer_key = "finishinguser" + item.download_virtual_path
        transfer = core.downloads.transfers.get(transfer_key)
        self.assertIsNotNone(transfer)

        # All bytes are in, but the transfer hasn't been marked FINISHED yet
        # (still finalizing) and speed has dropped to 0 now there's nothing left
        # to send
        transfer.status = TransferStatus.TRANSFERRING
        transfer.current_byte_offset = transfer.size = 8000000
        transfer.speed = 0
        core.download_lists._update_download(transfer, True)

        self.assertIsNone(item.stall_timer_id)

        # Even if a previously-scheduled stall callback still fires in this window,
        # it must not abort a transfer that's already fully received
        core.download_lists._handle_stalled_download("Finalizing List", "Almost Done Song")

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertIsNotNone(core.downloads.transfers.get(transfer_key))

        # The completion now arrives (as it would have anyway) and must go through cleanly
        transfer.status = TransferStatus.FINISHED
        core.download_lists._update_download(transfer, True)

        self.assertEqual(item.status, DownloadListItemStatus.COMPLETED)

    def test_near_complete_transfer_is_exempt_from_stalling(self):
        """A transfer that's e.g. 95% in (not literally every last byte) but has
        gone quiet is still just finalizing, not stalled — an exact '100%'
        check isn't a wide enough margin, since the watchdog's timer fires on
        its own independent schedule and can land a hair before the last byte
        is technically accounted for."""

        from pynicotine.transfers import TransferStatus

        download_list = core.download_lists.add_list(
            "Near Complete List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Near Complete List", ["Nearly There Song"])

        item = download_list.items["Nearly There Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Nearly There Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "nearlydoneuser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Near Complete List", "Nearly There Song")

        transfer_key = "nearlydoneuser" + item.download_virtual_path
        transfer = core.downloads.transfers.get(transfer_key)
        self.assertIsNotNone(transfer)

        transfer.status = TransferStatus.TRANSFERRING
        transfer.current_byte_offset = 7600000  # 95% of 8000000
        transfer.size = 8000000
        transfer.speed = 0
        core.download_lists._update_download(transfer, True)

        self.assertIsNone(item.stall_timer_id)

        core.download_lists._handle_stalled_download("Near Complete List", "Nearly There Song")

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertIsNotNone(core.downloads.transfers.get(transfer_key))

        transfer.status = TransferStatus.FINISHED
        core.download_lists._update_download(transfer, True)

        self.assertEqual(item.status, DownloadListItemStatus.COMPLETED)

    def test_healthy_speed_resets_stall_timer(self):
        """A transfer whose speed reaches the minimum threshold gets its stall
        deadline pushed back out, rather than being abandoned."""

        from pynicotine.transfers import TransferStatus

        download_list = core.download_lists.add_list(
            "Healthy List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Healthy List", ["Fast Artist - Fast Song"])

        item = download_list.items["Fast Artist - Fast Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Fast Artist\\Fast Artist - Fast Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "fastuser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Healthy List", "Fast Artist - Fast Song")

        original_timer_id = item.stall_timer_id
        self.assertIsNotNone(original_timer_id)

        transfer = core.downloads.transfers.get("fastuser" + item.download_virtual_path)
        transfer.status = TransferStatus.TRANSFERRING
        transfer.current_byte_offset = 4000000
        transfer.speed = 50 * 1024  # well above MIN_TRANSFER_SPEED
        core.download_lists._update_download(transfer, True)

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertIsNotNone(item.stall_timer_id)
        self.assertNotEqual(item.stall_timer_id, original_timer_id)

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

    def test_update_watch_folder_settings(self):
        """The UI-facing settings updater persists both config values and
        clears stale snapshots so a folder switch takes effect immediately."""

        watch_folder_path = self._set_up_watch_folder()
        self._write_watch_file(watch_folder_path, "Wanted.txt", "Artist - Song\n")

        core.download_lists._scan_watch_folder()
        self.assertTrue(core.download_lists._watch_snapshots)

        new_folder_path = os.path.join(DATA_FOLDER_PATH, "watch2")
        os.makedirs(new_folder_path, exist_ok=True)

        core.download_lists.update_watch_folder_settings(False, new_folder_path)

        self.assertFalse(config.sections["transfers"]["downloadlistwatchenabled"])
        self.assertEqual(config.sections["transfers"]["downloadlistwatchfolder"], new_folder_path)
        self.assertEqual(core.download_lists._watch_snapshots, {})

    def test_update_stall_settings(self):
        """The stall timeout/minimum speed are persisted to config and exposed
        via the stall_timeout/min_transfer_speed properties (the latter
        converted from the user-facing KiB/s to bytes/sec)."""

        defaults = config.defaults["transfers"]
        self.addCleanup(
            core.download_lists.update_stall_settings,
            defaults["downloadliststalltimeout"], defaults["downloadlistminspeed"]
        )

        core.download_lists.update_stall_settings(stall_timeout=60, min_speed_kib=0)

        self.assertEqual(config.sections["transfers"]["downloadliststalltimeout"], 60)
        self.assertEqual(config.sections["transfers"]["downloadlistminspeed"], 0)
        self.assertEqual(core.download_lists.stall_timeout, 60)
        self.assertEqual(core.download_lists.min_transfer_speed, 0)

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
