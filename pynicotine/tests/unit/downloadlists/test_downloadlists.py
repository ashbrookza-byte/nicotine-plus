# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shutil
import time

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

    def test_resolve_library_check(self):
        """A waiting library-check item either becomes In Library (found, no
        download) or is released into the search queue (not found)."""

        download_list = core.download_lists.add_list("Library List", auto_download=True)
        core.download_lists.add_list_items("Library List", ["Owned Song", "New Song"])

        for item in download_list.items.values():
            item.status = DownloadListItemStatus.LIBRARY_CHECK

        core.download_lists.resolve_library_check(
            "Library List", "Owned Song", True, "/music/library/Owned Song.flac")
        owned_item = download_list.items["Owned Song"]

        self.assertEqual(owned_item.status, DownloadListItemStatus.IN_LIBRARY)
        self.assertEqual(owned_item.download_filename, "Owned Song.flac")
        self.assertEqual(download_list.num_in_library, 1)

        core.download_lists.resolve_library_check("Library List", "New Song", False)
        new_item = download_list.items["New Song"]

        self.assertEqual(new_item.status, DownloadListItemStatus.PENDING)
        self.assertIn(("Library List", "New Song"), core.download_lists._queue)

        # In Library counts as a terminal state for completion
        new_item.status = DownloadListItemStatus.COMPLETED
        self.assertTrue(download_list.is_complete)

    def test_release_library_check_items(self):
        """"Continue and download" releases every waiting item to the queue."""

        download_list = core.download_lists.add_list("Release List", auto_download=True)
        core.download_lists.add_list_items("Release List", ["Song A", "Song B"])

        for item in download_list.items.values():
            item.status = DownloadListItemStatus.LIBRARY_CHECK

        core.download_lists.release_library_check_items()

        for term, item in download_list.items.items():
            self.assertEqual(item.status, DownloadListItemStatus.PENDING)
            self.assertIn(("Release List", term), core.download_lists._queue)

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
        item from a plain, unpinned list -- pinning always groups it ahead."""

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

    def test_set_list_pinned_keeps_pinned_lists_grouped_first(self):
        """Pinning a list moves it ahead of every unpinned list (to the back of
        the pinned block); unpinning moves it back to the front of the unpinned
        block. Priority order (self.lists key order) must reflect this."""

        core.download_lists.add_list("List A")
        core.download_lists.add_list("List B")
        core.download_lists.add_list("List C")

        core.download_lists.set_list_pinned("List B", True)
        self.assertEqual(list(core.download_lists.lists), ["List B", "List A", "List C"])

        core.download_lists.set_list_pinned("List C", True)
        self.assertEqual(list(core.download_lists.lists), ["List B", "List C", "List A"])

        core.download_lists.set_list_pinned("List B", False)
        self.assertEqual(list(core.download_lists.lists), ["List C", "List B", "List A"])

    def test_move_list_up_and_down_reorders_within_pinned_tier(self):
        """Move Up/Down swaps a list with its neighbor, but only within the same
        pinned/unpinned group -- it can't cross the pinned/unpinned boundary."""

        core.download_lists.add_list("List A")
        core.download_lists.add_list("List B")
        core.download_lists.add_list("List C")
        core.download_lists.set_list_pinned("List A", True)

        # List A (pinned) is alone in its group; moving it should be a no-op
        core.download_lists.move_list_down("List A")
        self.assertEqual(list(core.download_lists.lists), ["List A", "List B", "List C"])

        core.download_lists.move_list_down("List B")
        self.assertEqual(list(core.download_lists.lists), ["List A", "List C", "List B"])

        core.download_lists.move_list_up("List B")
        self.assertEqual(list(core.download_lists.lists), ["List A", "List B", "List C"])

        # Already at the front of its group -- no-op, doesn't cross into pinned territory
        core.download_lists.move_list_up("List B")
        self.assertEqual(list(core.download_lists.lists), ["List A", "List B", "List C"])

    def test_reorder_lists_from_drag_and_drop(self):
        """reorder_lists() applies a full new order within each pinned/unpinned
        tier, top row of each first -- as if the user dragged List C above
        List B, both unpinned."""

        core.download_lists.add_list("List A")
        core.download_lists.add_list("List B")
        core.download_lists.add_list("List C")

        core.download_lists.reorder_lists(["List A", "List C", "List B"])
        self.assertEqual(list(core.download_lists.lists), ["List A", "List C", "List B"])

    def test_reorder_lists_keeps_pinned_lists_grouped_first(self):
        """A drag that crosses the pinned/unpinned boundary is snapped back --
        pinned lists always stay grouped ahead of unpinned ones regardless of
        the raw order requested."""

        core.download_lists.add_list("List A")
        core.download_lists.add_list("List B")
        core.download_lists.add_list("List C")
        core.download_lists.set_list_pinned("List A", True)

        # Attempt to drag unpinned List C above pinned List A -- List A must
        # still end up first; only the relative order within each tier
        # (List C before List B, among the unpinned ones) is actually applied
        core.download_lists.reorder_lists(["List C", "List A", "List B"])
        self.assertEqual(list(core.download_lists.lists), ["List A", "List C", "List B"])

    def test_reorder_lists_ignores_incomplete_input(self):
        """A partial/invalid order (e.g. from a bug that lost track of a row)
        must not be applied -- risking silently dropping a list is worse than
        just ignoring the drag."""

        core.download_lists.add_list("List A")
        core.download_lists.add_list("List B")
        core.download_lists.add_list("List C")

        core.download_lists.reorder_lists(["List C", "List A"])  # missing List B
        self.assertEqual(list(core.download_lists.lists), ["List A", "List B", "List C"])

    def test_move_list_excludes_completed_unpinned_lists(self):
        """A completed, unpinned list has no meaningful priority left, and isn't
        a valid Move Up/Down neighbor for a still-active list."""

        core.download_lists.add_list("Active List", auto_download=False)
        completed_list = core.download_lists.add_list("Completed List", auto_download=False)
        core.download_lists.add_list_items("Completed List", ["Only Song"])
        completed_list.items["Only Song"].status = DownloadListItemStatus.COMPLETED

        self.assertTrue(completed_list.is_complete)

        # Nothing in the same (active-only) group to swap with -- no-op, and
        # nothing should crash trying to reorder past the completed list
        core.download_lists.move_list_down("Active List")
        self.assertEqual(list(core.download_lists.lists), ["Active List", "Completed List"])

    def test_dispatch_priority_follows_list_order_beyond_pinned_binary(self):
        """Among unpinned lists, priority order (not just plain queue order)
        determines dispatch order -- moving a list up gets it served first even
        though its item was queued after the other list's."""

        from pynicotine.slskmessages import UserStatus

        core.users.login_status = UserStatus.ONLINE
        self.addCleanup(setattr, core.users, "login_status", UserStatus.OFFLINE)

        core.download_lists.update_max_concurrent_downloads(1)
        self.addCleanup(core.download_lists.update_max_concurrent_downloads, 3)

        first_list = core.download_lists.add_list("First List", auto_download=True)
        core.download_lists.add_list_items("First List", ["First Song"])

        second_list = core.download_lists.add_list("Second List", auto_download=True)
        core.download_lists.add_list_items("Second List", ["Second Song"])

        # Second List was queued after First List, but raise its priority above it
        core.download_lists.move_list_up("Second List")

        core.download_lists._pump_queue()

        self.assertEqual(second_list.items["Second Song"].status, DownloadListItemStatus.SEARCHING)
        self.assertEqual(first_list.items["First Song"].status, DownloadListItemStatus.PENDING)

    def test_rename_list_preserves_priority_position(self):
        """Renaming a list must not silently drop it to the back of the priority
        order -- it should keep its exact position."""

        core.download_lists.add_list("List A")
        core.download_lists.add_list("List B")
        core.download_lists.add_list("List C")

        core.download_lists.rename_list("List B", "List B Renamed")

        self.assertEqual(list(core.download_lists.lists), ["List A", "List B Renamed", "List C"])

    def test_list_is_complete_once_every_item_is_terminal(self):

        download_list = core.download_lists.add_list("Completion List", auto_download=False)
        self.assertFalse(download_list.is_complete, "An empty list is never complete")

        core.download_lists.add_list_items("Completion List", ["Song One", "Song Two"])
        self.assertFalse(download_list.is_complete)

        download_list.items["Song One"].status = DownloadListItemStatus.COMPLETED
        self.assertFalse(download_list.is_complete, "Still one pending item left")

        download_list.items["Song Two"].status = DownloadListItemStatus.NOT_FOUND
        self.assertTrue(download_list.is_complete, "Completed + Not Found both count as terminal")

        # Resetting an item makes the list incomplete again
        core.download_lists.reset_list_item("Completion List", "Song Two")
        self.assertFalse(download_list.is_complete)

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

    def test_periodic_reconciliation_catches_a_finished_transfer_without_reset(self):
        """The periodic safety net (not Reset, not the stall handler — nothing
        user- or timer-triggered on this specific item) must, on its own,
        notice a Downloading item whose transfer actually finished and catch
        it up to Completed. This is what would have caught the real-world
        case: a transfer that failed (e.g. the peer went offline), was later
        retried/resumed to success by the transfer subsystem itself, with
        that completion never routed back through the normal per-item paths."""

        download_list = core.download_lists.add_list(
            "Reconcile List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Reconcile List", ["Silently Finished Song"])

        item = download_list.items["Silently Finished Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Silently Finished Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "resumeduser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Reconcile List", "Silently Finished Song")

        from pynicotine.transfers import TransferStatus

        transfer_key = "resumeduser" + item.download_virtual_path
        transfer = core.downloads.transfers.get(transfer_key)
        self.assertIsNotNone(transfer)

        # Simulate a failure and later out-of-band resume to success, entirely
        # outside anything download lists itself triggered
        transfer.status = TransferStatus.CONNECTION_TIMEOUT
        transfer.status = TransferStatus.FINISHED

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)

        core.download_lists._reconcile_downloading_items()

        self.assertEqual(item.status, DownloadListItemStatus.COMPLETED)
        self.assertEqual(item.download_percent, 100)

    def test_periodic_reconciliation_leaves_a_still_in_progress_item_alone(self):
        """The sweep must not touch a Downloading item whose transfer hasn't
        actually finished yet."""

        download_list = core.download_lists.add_list(
            "Reconcile In Progress List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Reconcile In Progress List", ["Still Going Song"])

        item = download_list.items["Still Going Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Still Going Song.mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "activeuser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Reconcile In Progress List", "Still Going Song")

        core.download_lists._reconcile_downloading_items()

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)

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
            (1, "@@abc\\Kasablanca - Time Is A Circle.mp3", 13700000, "mp3", lossy_attributes),
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
            (1, "@@abc\\Kasablanca - Time Is A Circle.mp3", 13700000, "mp3", lossy_attributes),
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
        """A word found in the filename itself counts in full; one found only
        in a parent folder counts for half."""

        match = core.download_lists._match_percentage

        self.assertEqual(match([], "anything", "anything"), 100.0)
        self.assertEqual(match(["artist", "song"], "artist - song.mp3", "folder/artist - song.mp3"), 100.0)
        self.assertEqual(match(["artist", "song"], "artist - other.mp3", "folder/artist - other.mp3"), 50.0)
        self.assertEqual(match(["artist", "song"], "unrelated.mp3", "folder/unrelated.mp3"), 0.0)

    def test_match_percentage_does_not_inflate_from_folder_name_alone(self):
        """A compilation/mix set whose FOLDER happens to be named after the
        search term must not score as a full match for a differently-titled
        track file inside it -- folder-only matches count for half, capping
        an all-folder, no-filename match at 50%, below the 70% default
        threshold, instead of the 100% a same-weighted path search would give."""

        match = core.download_lists._match_percentage

        term_words = ["pure", "carl", "cox"]
        # All 3 words are in the folder name only; the filename is an unrelated track title
        path = "music/carl cox - pure (radio show)/16. mind of the man (intro) (original mix).mp3"
        filename = "16. mind of the man (intro) (original mix).mp3"

        self.assertEqual(match(term_words, filename, path), 50.0)
        self.assertLess(match(term_words, filename, path), 70)  # below the default fuzzy threshold

    def test_extra_artists_become_optional_once_title_and_one_artist_match(self):
        """A Spotify export's "Title - Artist, Artist, Artist" term: a peer's
        filename that has the title and some of the artists is a full match --
        the artists it doesn't list aren't held against it (with 8 words and
        an 80% threshold, missing two of them used to mean Not Found)."""

        lists = core.download_lists
        term = "Si No Estas - Luna Dusk, Ikarus, MD DJ"

        path = "music/luna dusk, ikarus - si no estas (original mix).mp3"
        words = lists._effective_term_words(term, path)
        self.assertEqual(words, ["si", "no", "estas", "luna", "dusk", "ikarus"])
        self.assertEqual(lists._match_percentage(words, path.rsplit("/", 1)[-1], path), 100.0)

        # Every artist present: nothing is dropped
        path = "music/ikarus, md dj, luna dusk - si no estas (original mix).mp3"
        self.assertEqual(len(lists._effective_term_words(term, path)), 8)

    def test_title_and_at_least_one_artist_stay_required(self):
        """The leniency only covers artists beyond the first match: a different
        track by the same artist, or the same title by someone else, still
        falls well short of the threshold."""

        lists = core.download_lists
        term = "Si No Estas - Luna Dusk, Ikarus, MD DJ"

        # (well below the 80% default threshold -- two-letter words like "si"
        # and "no" match incidentally almost anywhere, as they always did)
        path = "music/luna dusk - otra cancion (original mix).mp3"
        words = lists._effective_term_words(term, path)
        self.assertLess(lists._match_percentage(words, path.rsplit("/", 1)[-1], path), 60)

        path = "music/somebody else - si no estas.mp3"
        words = lists._effective_term_words(term, path)
        self.assertLess(lists._match_percentage(words, path.rsplit("/", 1)[-1], path), 60)

    def test_accents_and_featuring_do_not_block_a_match(self):
        """"Lágrimas" matches a file named "Lagrimas" (and vice versa), and a
        "feat." in the term isn't a word the filename has to contain."""

        lists = core.download_lists

        self.assertEqual(lists._term_words("Lágrimas - MESTIZA (feat. Büya)"), ["lagrimas", "mestiza", "buya"])

        term = "Lágrimas - MESTIZA, PAUZA, Argentina"
        path = lists._fold_text("music/Mestiza, Pauza - Lagrimas (Original Mix).mp3")
        words = lists._effective_term_words(term, path)
        self.assertEqual(lists._match_percentage(words, path.rsplit("/", 1)[-1], path), 100.0)

    def test_joined_words_and_single_typos_are_tolerated_in_longer_words(self):
        """"fourtet" finds "four tet" and "clakson" finds "clarkson"; a short
        word like "opel" is not bent into "opal", one letter being most of it."""

        match = core.download_lists._match_percentage

        path = "music/bicep - opal [four tet rmx].mp3"
        self.assertEqual(match(["opal", "bicep", "fourtet"], path.rsplit("/", 1)[-1], path), 100.0)

        path = "music/kelly clarkson - stronger (what doesn't kill you).mp3"
        self.assertEqual(match(["kelly", "clakson", "stronger"], path.rsplit("/", 1)[-1], path), 100.0)

        path = "music/bicep - opal [four tet rmx].mp3"
        self.assertLess(match(["bicep", "opel", "fourtet"], path.rsplit("/", 1)[-1], path), 80)

    def test_term_variants_include_accent_folded_and_drop_one_word(self):
        """The accent-free spelling is searched as well (peers index filenames
        verbatim), and a short free-form term is also tried with each word
        left out so one misspelled word can't make it Not Found."""

        variants = core.download_lists._get_term_variants("Lágrimas - MESTIZA")
        self.assertEqual(variants[:2], ["Lágrimas - MESTIZA", "lagrimas - mestiza"])

        variants = core.download_lists._get_term_variants("kelly clakson stronger")
        self.assertEqual(
            variants, ["kelly clakson stronger", "clakson stronger", "kelly stronger", "kelly clakson"])

        # Not for structured terms (they have their own fallbacks) or long ones
        self.assertEqual(
            core.download_lists._get_term_variants("Artist - Title"), ["Artist - Title", "Artist", "Title"])
        self.assertEqual(
            core.download_lists._get_term_variants("one two three four five"), ["one two three four five"])

    def test_queued_download_is_not_treated_as_stalled(self):
        """A transfer the peer has accepted into its upload queue (including a
        "Too many files" limit, which downloads.py presents as Queued and resumes
        by itself) is waiting its turn: the stall timer just re-arms, until
        the queued-wait limit is up."""

        from pynicotine.transfers import TransferStatus

        download_list = core.download_lists.add_list(
            "Queue List", download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True
        )
        core.download_lists.add_list_items("Queue List", ["Busy Artist - Busy Song"])

        item = download_list.items["Busy Artist - Busy Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Busy Artist\\Busy Artist - Busy Song.mp3", 8000000, "mp3", attributes)]
        core.download_lists._file_search_response(self._make_response(item.token, "busyuser", files))
        core.download_lists._finalize_item("Queue List", "Busy Artist - Busy Song")

        transfer = core.downloads.transfers.get("busyuser" + item.download_virtual_path)
        self.assertIsNotNone(transfer)
        transfer.status = TransferStatus.QUEUED

        original_timer_id = item.stall_timer_id
        core.download_lists._handle_stalled_download("Queue List", "Busy Artist - Busy Song")

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertEqual(item.download_username, "busyuser")
        self.assertIsNotNone(item.stall_timer_id)
        self.assertNotEqual(item.stall_timer_id, original_timer_id)

        # Queued for longer than the limit: look for another source after all
        item.download_start_time -= core.download_lists.QUEUED_WAIT_LIMIT + 1
        core.download_lists._handle_stalled_download("Queue List", "Busy Artist - Busy Song")

        self.assertEqual(item.status, DownloadListItemStatus.PENDING)
        self.assertIsNone(item.download_username)

    def _dispatch_with_two_sources(self, list_name, term, preferred_user, other_user):
        """Dispatch an item and feed it a candidate from each of two users, the
        first with free slots (so it scores higher). Returns the item."""

        download_list = core.download_lists.add_list(
            list_name, download_folder_path=DATA_FOLDER_PATH, quality="any",
            fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items(list_name, [term])
        item = download_list.items[term]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, f"@@abc\\{term}.mp3", 8000000, "mp3", attributes)]
        core.download_lists._file_search_response(
            self._make_response(item.token, preferred_user, files, freeulslots=True))
        core.download_lists._file_search_response(
            self._make_response(item.token, other_user, files, freeulslots=False, inqueue=3))

        self.assertEqual(len(item.download_candidates), 2)
        return item

    def test_finalize_avoids_peer_with_a_manual_download_waiting(self):
        """A download the user queued by hand that is still waiting at a peer
        goes first: the list takes its download from another source instead
        of competing for the same peer's per-user slots."""

        from pynicotine.transfers import TransferStatus

        core.downloads.enqueue_download("pooluser", "@@abc\\Hand Picked Song.mp3", size=1000)
        manual = core.downloads.transfers.get("pooluser@@abc\\Hand Picked Song.mp3")
        self.assertIsNotNone(manual)
        manual.status = TransferStatus.QUEUED
        self.assertFalse(core.download_lists.owns_transfer("pooluser", "@@abc\\Hand Picked Song.mp3"))

        item = self._dispatch_with_two_sources("Manual First List", "Some Artist - Some Song", "pooluser", "otheruser")
        core.download_lists._finalize_item("Manual First List", "Some Artist - Some Song")

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertEqual(item.download_username, "otheruser")
        self.assertTrue(core.download_lists.owns_transfer("otheruser", item.download_virtual_path))

    def test_finalize_avoids_peer_that_rejected_for_too_many_files(self):
        """A peer that recently answered "Too many files" is passed over while
        its requests are being paced."""

        core.downloads._user_queue_limits["pooluser"] = 5
        try:
            item = self._dispatch_with_two_sources("Limited Peer List", "Busy Artist - Song", "pooluser", "otheruser")
            core.download_lists._finalize_item("Limited Peer List", "Busy Artist - Song")
        finally:
            core.downloads._user_queue_limits.pop("pooluser", None)

        self.assertEqual(item.download_username, "otheruser")

    def test_finalize_caps_automatic_downloads_per_peer(self):
        """Once a peer already has MAX_DOWNLOADS_PER_PEER of our automatic
        downloads, further items take another source when there is one --
        and still the capped peer when it is the only source."""

        download_list = core.download_lists.add_list("Cap List", quality="any", auto_download=True)
        core.download_lists.add_list_items("Cap List", ["First Song", "Second Song"])

        for term in ("First Song", "Second Song"):
            item = download_list.items[term]
            item.status = DownloadListItemStatus.DOWNLOADING
            item.download_username = "pooluser"

        self.assertTrue(core.download_lists._peer_is_busy("pooluser"))
        self.assertFalse(core.download_lists._peer_is_busy("otheruser"))

        item = self._dispatch_with_two_sources("Cap Test List", "Third Artist - Third Song", "pooluser", "otheruser")
        core.download_lists._finalize_item("Cap Test List", "Third Artist - Third Song")
        self.assertEqual(item.download_username, "otheruser")

        item = self._dispatch_with_two_sources("Cap Only List", "Fourth Artist - Fourth Song", "pooluser", "pooluser")
        core.download_lists._finalize_item("Cap Only List", "Fourth Artist - Fourth Song")
        self.assertEqual(item.download_username, "pooluser")

    def test_limited_retries_put_manual_downloads_before_list_downloads(self):
        """When a peer's "Too many files" rejections are retried a few at a
        time, downloads queued by hand are tried before a list's automatic
        ones, whatever order they were queued in."""

        from pynicotine.slskmessages import TransferRejectReason

        download_list = core.download_lists.add_list("Retry List", quality="any", auto_download=True)
        core.download_lists.add_list_items("Retry List", ["List Song"])
        item = download_list.items["List Song"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\List Song.mp3", 8000000, "mp3", attributes)]
        core.download_lists._file_search_response(self._make_response(item.token, "pooluser", files))
        core.download_lists._finalize_item("Retry List", "List Song")

        automatic = core.downloads.transfers.get("pooluser@@abc\\List Song.mp3")
        core.downloads.enqueue_download("pooluser", "@@abc\\Hand Picked Song.mp3", size=1000)
        manual = core.downloads.transfers.get("pooluser@@abc\\Hand Picked Song.mp3")
        self.assertIsNotNone(automatic)
        self.assertIsNotNone(manual)

        # Both bounced off the peer's limit, the automatic one first
        for transfer in (automatic, manual):
            core.downloads._abort_transfer(transfer, status=TransferRejectReason.QUEUED)

        failed_paths = [
            virtual_path for virtual_path in core.downloads.failed_users["pooluser"]
            if virtual_path in (automatic.virtual_path, manual.virtual_path)]
        self.assertEqual(failed_paths, [automatic.virtual_path, manual.virtual_path])

        core.downloads._user_queue_limits["pooluser"] = 1
        core.downloads._enqueue_limited_transfers("pooluser")

        # Only one retry allowed: it went to the manual download
        self.assertNotEqual(manual.status, TransferRejectReason.QUEUED)
        self.assertEqual(automatic.status, TransferRejectReason.QUEUED)

    def test_reset_not_found_items_requeues_every_not_found_song(self):
        """Every Not Found item in every list goes back to Pending and into the
        search queue; items in any other state are left alone."""

        first = core.download_lists.add_list("Retry All A", quality="any", auto_download=True)
        second = core.download_lists.add_list("Retry All B", quality="any", auto_download=True)
        core.download_lists.add_list_items("Retry All A", ["Missing One", "Found One"])
        core.download_lists.add_list_items("Retry All B", ["Missing Two"])

        for download_list, term in ((first, "Missing One"), (second, "Missing Two")):
            item = download_list.items[term]
            item.status = DownloadListItemStatus.NOT_FOUND
            item.searched_term = term.lower()

        found = first.items["Found One"]
        found.status = DownloadListItemStatus.COMPLETED
        found.download_username = "someuser"

        core.download_lists._queue.clear()

        self.assertEqual(core.download_lists.reset_not_found_items(), 2)

        for download_list, term in ((first, "Missing One"), (second, "Missing Two")):
            item = download_list.items[term]
            self.assertIn(item.status, (DownloadListItemStatus.PENDING, DownloadListItemStatus.SEARCHING))
            self.assertIsNone(item.download_username)

        self.assertEqual(found.status, DownloadListItemStatus.COMPLETED)
        self.assertEqual(found.download_username, "someuser")

        # Nothing left to reset
        self.assertEqual(core.download_lists.reset_not_found_items(), 0)

    def _force_not_found(self, list_name, term):
        """Exhaust an item's search variants and time budget so it ends Not Found."""

        item = core.download_lists.lists[list_name].items[term]
        item.dispatch_time = time.time() - core.download_lists.SEARCH_TIMEOUT - 1

        for _attempt in range(12):
            if item.status != DownloadListItemStatus.SEARCHING:
                break

            core.download_lists._escalate_item(list_name, term)

        self.assertEqual(item.status, DownloadListItemStatus.NOT_FOUND)
        return item

    def test_not_found_item_suggests_the_closest_files_it_saw(self):
        """Results that fell short of the match threshold are kept, best first,
        as suggestions on the Not Found item -- and survive a save."""

        download_list = core.download_lists.add_list(
            "Suggest List", quality="any", fuzzy_match_threshold=80, auto_download=True)
        core.download_lists.add_list_items("Suggest List", ["opel bicep fourtet"])

        item = download_list.items["opel bicep fourtet"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [
            (1, "@@abc\\04. Bicep - Opal [Four Tet Rmx].mp3", 8000000, "mp3", attributes),   # 2 of 3 words
            (1, "@@abc\\Bicep - Glue.mp3", 8000000, "mp3", attributes),                      # 1 of 3 words
            (1, "@@abc\\Unrelated - Song.mp3", 8000000, "mp3", attributes)                   # nothing
        ]
        core.download_lists._file_search_response(self._make_response(item.token, "someuser", files))

        self.assertEqual(item.download_candidates, [])
        self.assertEqual(len(item.near_misses), 1)

        item = self._force_not_found("Suggest List", "opel bicep fourtet")

        self.assertEqual(len(item.suggestions), 1)
        self.assertEqual(item.suggestions[0]["filename"], "@@abc\\04. Bicep - Opal [Four Tet Rmx].mp3")
        self.assertEqual(item.suggestions[0]["match"], 67)
        self.assertEqual(item.suggestions[0]["searched_term"], "opel bicep fourtet")
        self.assertEqual(item.as_dict()["suggestions"], item.suggestions)
        self.assertEqual(
            core.download_lists.suggestion_term(item.suggestions[0]["filename"]), "Bicep Opal Four Tet Rmx")

    def test_not_found_item_without_near_misses_has_no_suggestions(self):

        download_list = core.download_lists.add_list("No Suggest List", quality="any", auto_download=True)
        core.download_lists.add_list_items("No Suggest List", ["nothing like this"])
        core.download_lists._dispatch_item(download_list, download_list.items["nothing like this"])

        item = self._force_not_found("No Suggest List", "nothing like this")
        self.assertEqual(item.suggestions, [])

    def test_retarget_list_item_keeps_position_and_searches_afresh(self):

        download_list = core.download_lists.add_list("Retarget List", quality="any", auto_download=True)
        core.download_lists.add_list_items("Retarget List", ["first", "opel bicep fourtet", "third"])

        item = download_list.items["opel bicep fourtet"]
        item.status = DownloadListItemStatus.NOT_FOUND
        item.suggestions = [{"filename": "x.mp3", "match": 50, "searched_term": "opel bicep fourtet"}]

        core.download_lists.retarget_list_item("Retarget List", "opel bicep fourtet", "Bicep Opal Four Tet Rmx")

        self.assertEqual(list(download_list.items), ["first", "Bicep Opal Four Tet Rmx", "third"])
        self.assertIs(download_list.items["Bicep Opal Four Tet Rmx"], item)
        self.assertEqual(item.term, "Bicep Opal Four Tet Rmx")
        self.assertIn(item.status, (DownloadListItemStatus.PENDING, DownloadListItemStatus.SEARCHING))
        self.assertEqual(item.suggestions, [])

        # Retargeting onto a term already in the list just drops the duplicate
        core.download_lists.retarget_list_item("Retarget List", "third", "first")
        self.assertEqual(list(download_list.items), ["first", "Bicep Opal Four Tet Rmx"])

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

    def test_remix_candidate_is_only_a_fallback_when_term_has_no_remix(self):
        """The original term doesn't say "remix" -- a candidate that's some
        specific remix is kept, but any candidate of the right (plain) version
        beats it, however much better the remix's bitrate or slots are. It only
        gets downloaded when nothing else turned up, instead of Not Found."""

        download_list = core.download_lists.add_list(
            "No Remix List", quality="any", fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items("No Remix List", ["Blissful Thinking - Das Pharaoh"])

        item = download_list.items["Blissful Thinking - Das Pharaoh"]
        core.download_lists._dispatch_item(download_list, item)

        remix_attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        remix_files = [
            (1, "@@abc\\Das Pharaoh - Blissful Thinking (Someone Remix).mp3", 8000000, "mp3", remix_attributes)]
        core.download_lists._file_search_response(
            self._make_response(item.token, "remixuser", remix_files, freeulslots=True))

        self.assertEqual(len(item.download_candidates), 1)

        plain_attributes = FileAttributes(bitrate=128, length=200, vbr=0)
        plain_files = [(1, "@@abc\\Das Pharaoh - Blissful Thinking.mp3", 4000000, "mp3", plain_attributes)]
        core.download_lists._file_search_response(
            self._make_response(item.token, "plainuser", plain_files, freeulslots=False, inqueue=5))

        self.assertEqual(len(item.download_candidates), 2)

        core.download_lists._finalize_item("No Remix List", "Blissful Thinking - Das Pharaoh")

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertEqual(item.download_username, "plainuser")

    def test_typed_term_ignores_remix_status_entirely(self):
        """A free-form term typed by hand (no "Title - Artist" separator) is a
        keyword search: a remix and the plain track are equal candidates, and
        whichever is otherwise better (here: free slots) is picked."""

        download_list = core.download_lists.add_list(
            "Typed List", quality="any", fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items("Typed List", ["bicep atlas"])

        item = download_list.items["bicep atlas"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        plain_files = [(1, "@@abc\\Bicep - Atlas (Original Mix).mp3", 8000000, "mp3", attributes)]
        remix_files = [(1, "@@abc\\Bicep - Atlas (Someone Remix).mp3", 8000000, "mp3", attributes)]
        core.download_lists._file_search_response(
            self._make_response(item.token, "plainuser", plain_files, freeulslots=False))
        core.download_lists._file_search_response(
            self._make_response(item.token, "remixuser", remix_files, freeulslots=True))

        self.assertEqual(len(item.download_candidates), 2)
        # Neither candidate carries a version penalty
        self.assertTrue(all(candidate[0][0] for candidate in item.download_candidates))

        core.download_lists._finalize_item("Typed List", "bicep atlas")
        self.assertEqual(item.download_username, "remixuser")

    def test_remix_candidate_downloaded_when_nothing_else_exists(self):
        """A structured term whose track only exists as a remix (e.g. a Spotify
        title that already is a remix without saying so) is downloaded as that
        remix rather than ending in Not Found."""

        download_list = core.download_lists.add_list(
            "Remix Only List", quality="any", fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items("Remix Only List", ["Wait - M83, David Mackay"])

        item = download_list.items["Wait - M83, David Mackay"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=296, vbr=0)
        files = [(1, "@@abc\\m83 - wait (shimza, ewerseen, david mackay remix).mp3", 11000000, "mp3", attributes)]
        core.download_lists._file_search_response(self._make_response(item.token, "someuser", files))
        core.download_lists._finalize_item("Remix Only List", "Wait - M83, David Mackay")

        self.assertEqual(item.status, DownloadListItemStatus.DOWNLOADING)
        self.assertTrue(item.download_virtual_path.endswith("david mackay remix).mp3"))

    def test_plain_candidate_rejected_when_term_wants_remix(self):
        """The original term explicitly asks for a remix -- a candidate that's
        the plain original mix is a different version and must not be picked."""

        download_list = core.download_lists.add_list(
            "Wants Remix List", quality="any", fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items("Wants Remix List", ["Blissful Thinking (Someone Remix) - Das Pharaoh"])

        item = download_list.items["Blissful Thinking (Someone Remix) - Das Pharaoh"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)
        files = [(1, "@@abc\\Das Pharaoh - Blissful Thinking (Original Mix).mp3", 8000000, "mp3", attributes)]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)

        self.assertEqual(item.download_candidates, [])

    def test_remix_candidate_accepted_when_remix_status_agrees(self):
        """Both non-remix-to-non-remix and remix-to-remix pairings are valid
        candidates -- only a mismatch between the two is rejected."""

        download_list = core.download_lists.add_list(
            "Matching Remix List", quality="any", fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items(
            "Matching Remix List",
            ["Blissful Thinking - Das Pharaoh", "Blissful Thinking (Someone Remix) - Das Pharaoh"]
        )

        plain_item = download_list.items["Blissful Thinking - Das Pharaoh"]
        remix_item = download_list.items["Blissful Thinking (Someone Remix) - Das Pharaoh"]

        core.download_lists._dispatch_item(download_list, plain_item)
        core.download_lists._dispatch_item(download_list, remix_item)

        attributes = FileAttributes(bitrate=320, length=200, vbr=0)

        plain_files = [(1, "@@abc\\Das Pharaoh - Blissful Thinking.mp3", 8000000, "mp3", attributes)]
        core.download_lists._file_search_response(
            self._make_response(plain_item.token, "someuser", plain_files))

        remix_files = [(1, "@@abc\\Das Pharaoh - Blissful Thinking (Someone Remix).mp3", 8000000, "mp3", attributes)]
        core.download_lists._file_search_response(
            self._make_response(remix_item.token, "otheruser", remix_files))

        self.assertEqual(len(plain_item.download_candidates), 1)
        self.assertEqual(len(remix_item.download_candidates), 1)

    def test_named_mix_candidate_is_accepted_but_scored_honestly(self):
        """Reported live: 'PURE - Carl Cox' (a plain term) matched and
        downloaded 'Carl Cox - PURE (El Rancho Mix).mp3' at a claimed 100%
        match. Confirmed as an acceptable match (a named/branded mix that
        doesn't say "remix" isn't rejected) -- but a padded filename like
        this must not be indistinguishable from a truly exact one, so its
        displayed match percentage should honestly reflect the extra
        "(El Rancho Mix)" content, not claim 100%."""

        download_list = core.download_lists.add_list(
            "Named Mix List", quality="any", fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items("Named Mix List", ["PURE - Carl Cox"])

        item = download_list.items["PURE - Carl Cox"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=479, vbr=0)
        files = [(1, "@@abc\\Carl Cox - PURE (El Rancho Mix).mp3", 19000000, "mp3", attributes)]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)
        self.assertEqual(len(item.download_candidates), 1)

        core.download_lists._finalize_item("Named Mix List", "PURE - Carl Cox")

        # Term words {pure, carl, cox}; filename words {carl, cox, pure, el, rancho, mix}
        # -> 3 shared / 6 total (Jaccard) = 50%, not the misleading 100% a plain
        # "were all the term's words present" score would still claim
        self.assertEqual(item.download_match_percentage, 50)

    def test_exact_filename_match_still_scores_100_percent(self):
        """The stricter, honest display score must not falsely dock a
        candidate whose filename has no extra content beyond the term."""

        download_list = core.download_lists.add_list(
            "Exact Match List", quality="any", fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items("Exact Match List", ["Carl Cox - Pure"])

        item = download_list.items["Carl Cox - Pure"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=479, vbr=0)
        files = [(1, "@@abc\\Carl Cox - Pure.mp3", 19000000, "mp3", attributes)]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)
        core.download_lists._finalize_item("Exact Match List", "Carl Cox - Pure")

        self.assertEqual(item.download_match_percentage, 100)

    def test_continuous_mix_compilation_rejected_regardless_of_naming(self):
        """Reported live: the same 'PURE - Carl Cox' term also matched a full
        80-minute continuous DJ mix compilation ("25. Carl Cox Continous Mix
        ''Pure Intec 4''.mp3") whose title legitimately contains every term
        word as plain, unparenthesized text -- no naming-convention check
        could reliably catch every phrasing/misspelling of this, but an
        implausible length for a single track always gives it away."""

        download_list = core.download_lists.add_list(
            "No Compilation List", quality="any", fuzzy_match_threshold=50, auto_download=True)
        core.download_lists.add_list_items("No Compilation List", ["PURE - Carl Cox"])

        item = download_list.items["PURE - Carl Cox"]
        core.download_lists._dispatch_item(download_list, item)

        attributes = FileAttributes(bitrate=320, length=4836, vbr=0)  # ~80 minutes
        files = [(
            1, "media\\Pure Intec 4 (Mixed By Carl Cox & Jon Rundell) (2019)\\Disc 1\\"
               "25. Carl Cox Continous Mix ''Pure Intec 4''.mp3", 193500000, "mp3", attributes
        )]
        msg = self._make_response(item.token, "someuser", files)

        core.download_lists._file_search_response(msg)

        self.assertEqual(item.download_candidates, [])

    def test_search_text_does_not_exclude_remix_when_term_has_no_remix(self):
        """No "-remix" exclusion is added to the network search request: peers
        would then never return a track that only exists as a remix, which
        is instead handled locally as a fallback candidate."""

        from pynicotine.events import events
        from pynicotine.slskmessages import FileSearch

        download_list = core.download_lists.add_list("No Remix Search List", auto_download=True)
        core.download_lists.add_list_items("No Remix Search List", ["Blissful Thinking - Das Pharaoh"])
        item = download_list.items["Blissful Thinking - Das Pharaoh"]

        sent_messages = []

        def capture_message(msg):
            sent_messages.append(msg)

        events.connect("queue-network-message", capture_message)
        try:
            core.download_lists._dispatch_item(download_list, item)
        finally:
            events.disconnect("queue-network-message", capture_message)

        searches = [msg for msg in sent_messages if isinstance(msg, FileSearch)]
        self.assertEqual(len(searches), 1)
        self.assertNotIn("-remix", searches[0].searchterm)
        self.assertEqual(searches[0].searchterm, "Blissful Thinking Das Pharaoh")

    def test_search_text_does_not_exclude_remix_when_term_wants_one(self):
        """The term already asks for a remix -- excluding "remix" from its own
        search would find nothing, so no exclusion is added."""

        from pynicotine.events import events
        from pynicotine.slskmessages import FileSearch

        download_list = core.download_lists.add_list("Wants Remix Search List", auto_download=True)
        core.download_lists.add_list_items(
            "Wants Remix Search List", ["Blissful Thinking (Someone Remix) - Das Pharaoh"])
        item = download_list.items["Blissful Thinking (Someone Remix) - Das Pharaoh"]

        sent_messages = []

        def capture_message(msg):
            sent_messages.append(msg)

        events.connect("queue-network-message", capture_message)
        try:
            core.download_lists._dispatch_item(download_list, item)
        finally:
            events.disconnect("queue-network-message", capture_message)

        searches = [msg for msg in sent_messages if isinstance(msg, FileSearch)]
        self.assertEqual(len(searches), 1)
        self.assertNotIn("-remix", searches[0].searchterm)

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
