# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Download Lists: named, folder-backed batches of songs that are searched
for and downloaded automatically.

Add a list of terms (e.g. one song per line), and each one is searched for,
matched against the list's own quality/length/accuracy settings, and
downloaded to the list's own folder — so results can be reviewed there
before being moved into a permanent library. A per-list summary (search
term, term actually searched, downloaded file, and its specs) is available
at any time and can be exported to CSV.

This is a separate feature from the classic per-term "Wishlist" search mode
available from the Search page (core.search.wishlist) - that one just keeps
periodically re-searching a single term via the server's throttled wishlist
protocol, and is left untouched.
"""

import csv
import json
import os
import re
import shutil
import threading
import time
import unicodedata

from collections import deque
from operator import itemgetter

from pynicotine import audioquality
from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.logfacility import log
from pynicotine.slskmessages import AddAllowedResponse
from pynicotine.slskmessages import FileAttributes
from pynicotine.slskmessages import FileListMessage
from pynicotine.slskmessages import FileSearch
from pynicotine.slskmessages import FileSearchResponse
from pynicotine.slskmessages import RemoveAllowedResponse
from pynicotine.slskmessages import UserStatus
from pynicotine.slskmessages import increment_token
from pynicotine.slskmessages import initial_token
from pynicotine.transfers import TransferStatus
from pynicotine.utils import encode_path
from pynicotine.utils import load_file
from pynicotine.utils import safe_path_join
from pynicotine.utils import write_file_and_backup

# Sentinel distinguishing "argument not provided, leave setting unchanged" from an
# explicit None, which means "clear this list's override, follow the overall default"
_UNSET = object()


class DownloadListItemStatus:
    PENDING = "pending"
    LIBRARY_CHECK = "library_check"
    IN_LIBRARY = "in_library"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    QUALITY_CHECK = "quality_check"
    COMPLETED = "completed"
    NOT_FOUND = "not_found"


class DownloadListItem:
    __slots__ = (
        "term", "list_name", "time_added", "status", "searched_term", "token",
        "download_username", "download_virtual_path", "download_size", "download_attributes",
        "download_match_percentage", "download_candidates", "variant_index", "collect_timer_id",
        "escalation_timer_id", "download_percent", "stall_timer_id", "dispatch_time",
        "download_start_time", "library_location", "suggestions", "near_misses",
        "rejected_files", "num_quality_rejections", "quality_cutoff_hz"
    )

    def __init__(self, term, list_name, time_added=None, status=DownloadListItemStatus.PENDING,
                 searched_term=None, download_username=None, download_virtual_path=None,
                 download_size=0, download_attributes=None, download_match_percentage=None,
                 library_location=None, suggestions=None, rejected_files=None, num_quality_rejections=0,
                 quality_cutoff_hz=None):

        self.term = term
        self.list_name = list_name
        self.time_added = time_added if time_added is not None else int(time.time())
        self.searched_term = searched_term

        if status in (DownloadListItemStatus.SEARCHING, DownloadListItemStatus.DOWNLOADING,
                      DownloadListItemStatus.QUALITY_CHECK):
            # In-flight state can't resume across restarts (its bookkeeping isn't
            # persisted), so pick up as pending instead
            status = DownloadListItemStatus.PENDING

        self.status = status
        self.library_location = library_location
        self.download_username = download_username
        self.download_virtual_path = download_virtual_path
        self.download_size = download_size
        self.download_attributes = download_attributes
        self.download_match_percentage = download_match_percentage

        # For a Not Found item: the closest files the searches did see (each a
        # dict with "filename", "match" and "searched_term"), so the user can
        # tell a mistyped term from a track that's genuinely not shared, and
        # pick one of them to search for instead
        self.suggestions = suggestions or []

        # Sources ("username" + virtual path) whose file failed the quality
        # check, never to be picked for this item again; and the measured
        # spectral cutoff of the current download, once checked
        self.rejected_files = set(rejected_files or [])
        self.num_quality_rejections = num_quality_rejections
        self.quality_cutoff_hz = quality_cutoff_hz

        # Transient, session-only state
        self.token = None
        self.download_candidates = []
        # filename (lowercase) -> (match percentage, searched term, filename) of
        # results seen while searching that didn't make the cut
        self.near_misses = {}
        self.variant_index = 0
        self.collect_timer_id = None
        self.escalation_timer_id = None
        self.download_percent = 100 if status == DownloadListItemStatus.COMPLETED else 0
        self.stall_timer_id = None
        self.dispatch_time = None
        self.download_start_time = None

    @property
    def h_quality(self):

        if self.download_attributes is None:
            return ""

        h_quality, *_unused = FileListMessage.parse_audio_quality_length(
            self.download_size, self.download_attributes, always_show_bitrate=True)
        return h_quality

    @property
    def h_length(self):

        if self.download_attributes is None:
            return ""

        _h_quality, _bitrate, h_length, _length = FileListMessage.parse_audio_quality_length(
            self.download_size, self.download_attributes)
        return h_length

    @property
    def download_filename(self):

        if not self.download_virtual_path:
            # An In Library item has no download, but showing which library
            # file satisfied it is just as useful in the same column
            if self.library_location:
                return self.library_location.replace("\\", "/").rsplit("/", 1)[-1]

            return ""

        return self.download_virtual_path.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def h_match_percentage(self):
        return "" if self.download_match_percentage is None else f"{self.download_match_percentage}%"

    def as_dict(self):

        attributes = self.download_attributes

        return {
            "term": self.term,
            "time_added": self.time_added,
            "status": self.status,
            "library_location": self.library_location,
            "searched_term": self.searched_term,
            "download_username": self.download_username,
            "download_virtual_path": self.download_virtual_path,
            "download_size": self.download_size,
            "download_match_percentage": self.download_match_percentage,
            "suggestions": self.suggestions,
            "rejected_files": sorted(self.rejected_files),
            "num_quality_rejections": self.num_quality_rejections,
            "quality_cutoff_hz": self.quality_cutoff_hz,
            "download_bitrate": attributes.bitrate if attributes else None,
            "download_length": attributes.length if attributes else None,
            "download_vbr": attributes.vbr if attributes else None,
            "download_sample_rate": attributes.sample_rate if attributes else None,
            "download_bit_depth": attributes.bit_depth if attributes else None
        }


class DownloadList:
    __slots__ = (
        "name", "download_folder_path", "quality", "prefer_longer", "prefer_lossless",
        "preferred_keywords", "fuzzy_match_threshold", "auto_download", "use_name_subfolder",
        "pinned", "time_added", "items"
    )

    def __init__(self, name, download_folder_path=None, quality=None, prefer_longer=None,
                 prefer_lossless=None, preferred_keywords=None, fuzzy_match_threshold=None,
                 auto_download=None, use_name_subfolder=None, pinned=False, time_added=None, items=None):

        self.name = name
        self.download_folder_path = download_folder_path or None

        # None means "use the overall wishlist default" (set in Wishlist Settings)
        # rather than a value specific to this list
        self.quality = quality
        self.prefer_longer = prefer_longer
        self.prefer_lossless = prefer_lossless
        self.preferred_keywords = preferred_keywords
        self.fuzzy_match_threshold = fuzzy_match_threshold
        self.auto_download = auto_download
        self.use_name_subfolder = use_name_subfolder

        # Not a per-item-matching preference like the above; whether this list's
        # queued items should be dispatched ahead of every other list's
        self.pinned = bool(pinned)

        self.time_added = time_added if time_added is not None else int(time.time())
        self.items = items if items is not None else {}

    @property
    def effective_quality(self):
        return self.quality if self.quality is not None else config.sections["transfers"]["downloadlistdefaultquality"]

    @property
    def effective_prefer_longer(self):
        if self.prefer_longer is not None:
            return self.prefer_longer

        return config.sections["transfers"]["downloadlistdefaultpreferlonger"]

    @property
    def effective_prefer_lossless(self):
        if self.prefer_lossless is not None:
            return self.prefer_lossless

        return config.sections["transfers"]["downloadlistdefaultpreferlossless"]

    @property
    def effective_preferred_keywords(self):
        if self.preferred_keywords is not None:
            return self.preferred_keywords

        return config.sections["transfers"]["downloadlistdefaultkeywords"]

    @property
    def effective_fuzzy_match_threshold(self):
        if self.fuzzy_match_threshold is not None:
            return self.fuzzy_match_threshold

        return config.sections["transfers"]["downloadlistdefaultfuzzy"]

    @property
    def effective_auto_download(self):
        if self.auto_download is not None:
            return self.auto_download

        return config.sections["transfers"]["downloadlistdefaultautodownload"]

    @property
    def effective_use_name_subfolder(self):
        if self.use_name_subfolder is not None:
            return self.use_name_subfolder

        return config.sections["transfers"]["downloadlistdefaultnamesubfolder"]

    @property
    def effective_download_folder_path(self):
        """The folder downloads for this list should land in, taking the
        "subfolder named after the list" preference into account."""

        base_folder_path = self.download_folder_path or core.downloads.get_default_download_folder()

        if not self.effective_use_name_subfolder:
            return base_folder_path

        return safe_path_join(base_folder_path, self.name)

    @property
    def num_completed(self):
        return sum(1 for item in self.items.values() if item.status == DownloadListItemStatus.COMPLETED)

    @property
    def num_in_library(self):
        return sum(1 for item in self.items.values() if item.status == DownloadListItemStatus.IN_LIBRARY)

    @property
    def num_not_found(self):
        return sum(1 for item in self.items.values() if item.status == DownloadListItemStatus.NOT_FOUND)

    @property
    def num_pending(self):
        return sum(
            1 for item in self.items.values()
            if item.status in (
                DownloadListItemStatus.PENDING, DownloadListItemStatus.LIBRARY_CHECK,
                DownloadListItemStatus.SEARCHING, DownloadListItemStatus.DOWNLOADING
            )
        )

    @property
    def is_complete(self):
        """True once every item in the list has reached a terminal state."""
        return bool(self.items) and self.num_pending == 0

    def as_dict(self):

        return {
            "name": self.name,
            "download_folder_path": self.download_folder_path,
            "quality": self.quality,
            "prefer_longer": self.prefer_longer,
            "prefer_lossless": self.prefer_lossless,
            "preferred_keywords": self.preferred_keywords,
            "fuzzy_match_threshold": self.fuzzy_match_threshold,
            "auto_download": self.auto_download,
            "use_name_subfolder": self.use_name_subfolder,
            "pinned": self.pinned,
            "time_added": self.time_added,
            "items": [item.as_dict() for item in self.items.values()]
        }


class DownloadLists:
    __slots__ = (
        "lists", "file_path", "_token", "_queue", "_dispatch_timer_id",
        "_token_map", "_transfer_map", "_allow_saving", "_watch_timer_id", "_watch_snapshots",
        "_reconcile_timer_id"
    )

    FILE_BASENAME = "download_lists.json"

    # How often the watch folder is polled for new song list files
    WATCH_INTERVAL = 30

    # How often Downloading items are double-checked against their actual
    # transfer state, catching any whose completion never got routed back
    RECONCILE_INTERVAL = 60

    # Song list files dropped in the watch folder are moved here once imported
    WATCH_IMPORTED_FOLDER_NAME = "imported"

    WATCH_FILE_EXTENSIONS = (".txt", ".csv")

    # Extensions considered when picking a file to automatically download
    AUDIO_EXTENSIONS = {
        ".mp3", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma", ".ape", ".aiff", ".alac", ".mp4"
    }

    # A continuous DJ mix/compilation album can still end up with a high word-match score
    # (its title/filename often legitimately contains the artist and a track's title, e.g.
    # a "Pure Intec 4 (Mixed By Carl Cox)" compilation, or a "Carl Cox Continuous Mix"), but
    # is never actually the single track being searched for -- unlike naming conventions,
    # which are inconsistent and easy to miss a variant of, an implausible length for a
    # single track is a reliable, naming-agnostic tell
    MAX_REASONABLE_TRACK_LENGTH = 20 * 60

    # Pacing between dispatching each queued item's initial search, so a big list doesn't flood the server
    DISPATCH_DELAY = 8

    # How long to keep collecting results for an item before picking the best match
    COLLECTION_DELAY = 15

    # How long to wait for results before broadening an item's search term
    ESCALATION_DELAY = 10

    # Total time to keep waiting for at least one result before giving up as
    # Not Found. Search responses can keep trickling in well past ESCALATION_DELAY,
    # and a term with no bracket/feat/extra-artist clause to strip (e.g. a plain
    # "Artist - Title") has nothing left to broaden to after the very first
    # escalation attempt, so it must not give up right there
    SEARCH_TIMEOUT = 45

    # A download whose transfer speed stays below the configured minimum for the
    # configured timeout (whether it's stuck at 0% or just crawling) is abandoned
    # and searched again. Both are user-configurable in Wishlist Settings (see
    # stall_timeout/min_transfer_speed below), since what counts as "too slow"
    # depends heavily on the user's own connection

    # A transfer this far along is never treated as stalled, no matter how slow
    # or stuck-at-0-speed it looks, since it's essentially just finalizing
    # (moving out of the incomplete folder, hash-checking, etc.) at this point
    STALL_EXEMPT_PERCENT = 90

    # A download the peer has accepted into its upload queue ("Queued" -- which
    # is also how downloads.py presents a peer's "Too many files" limit, resuming
    # it by itself later) is waiting its turn, not stalled. Only after this long
    # in the queue is a different source looked for
    QUEUED_WAIT_LIMIT = 20 * 60

    # At most this many automatic downloads at once from any one peer. A big
    # share (a DJ pool) turns up in nearly every search and would otherwise
    # end up with every list's downloads in its queue, hitting its per-user
    # file limit -- and then the user's own manual downloads from it bounce
    # with "Too many files" and just sit there as Queued
    MAX_DOWNLOADS_PER_PEER = 2

    # Results that fell short of the match threshold are still worth showing
    # the user as "did you mean" suggestions once an item ends up Not Found --
    # if they matched at least this much of the term -- up to this many
    NEAR_MISS_MIN_PERCENT = 34
    MAX_SUGGESTIONS = 5

    # A finished download whose spectrum shows it was made from a worse
    # encode than it claims is set aside in this subfolder of the list's
    # download folder and searched for again, up to this many times
    REJECTED_QUALITY_FOLDER_NAME = "Rejected Quality"
    MAX_QUALITY_REJECTIONS = 3

    # Filler words in a term that a filename can't be expected to contain
    IGNORED_TERM_WORDS = {"feat", "ft", "featuring"}

    # Minimum word length for the forgiving comparisons (joined words, one typo)
    FUZZY_WORD_MIN_LENGTH = 6

    QUALITY_LABELS = {
        "any": _("Any"),
        "good": _("Good (192 kbps+)"),
        "high": _("High (320 kbps+)"),
        "lossless": _("Lossless only (FLAC/WAV)")
    }

    STATUS_LABELS = {
        DownloadListItemStatus.PENDING: _("Pending"),
        DownloadListItemStatus.LIBRARY_CHECK: _("Checking Library…"),
        DownloadListItemStatus.IN_LIBRARY: _("In Library"),
        DownloadListItemStatus.SEARCHING: _("Searching…"),
        DownloadListItemStatus.DOWNLOADING: _("Downloading…"),
        DownloadListItemStatus.QUALITY_CHECK: _("Checking Quality…"),
        DownloadListItemStatus.COMPLETED: _("Completed"),
        DownloadListItemStatus.NOT_FOUND: _("Not Found")
    }

    # Progressive search term simplification, used to broaden an item's search when no
    # results come back for the exact term (e.g. extra featured artists, remix tags)
    BRACKETED_CONTENT_PATTERN = re.compile(r"[(\[][^)\]]*[)\]]")
    TERM_SEGMENT_SPLIT_PATTERN = re.compile(r"\s+-\s+")
    LEADING_TRACK_NUMBER_PATTERN = re.compile(r"^\s*\d{1,3}\s*[-._)]+\s*")
    TEXT_TOKEN_PATTERN = re.compile(r"\w+")
    FEATURED_ARTIST_PATTERN = re.compile(r"\s*[(\[]?\b(feat\.?|ft\.?|featuring|with)\b[^-]*", re.IGNORECASE)
    EXTRA_ARTIST_SPLIT_PATTERN = re.compile(r"\s*(?:,|&|\bx\b|\bvs\.?\b)\s*", re.IGNORECASE)
    COLLAPSE_WHITESPACE_PATTERN = re.compile(r"\s+")
    REMOVED_SEARCH_CHARACTERS = str.maketrans(dict.fromkeys(
        "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~–—‐’“”…", " "
    ))

    def __init__(self):

        self.lists = {}
        self.file_path = os.path.join(config.data_folder_path, self.FILE_BASENAME)

        self._token = initial_token()
        self._queue = deque()
        self._dispatch_timer_id = None

        # token -> (list_name, term)
        self._token_map = {}

        # "username + virtual_path" transfer key -> (list_name, term)
        self._transfer_map = {}

        self._allow_saving = False
        self._watch_timer_id = None
        self._reconcile_timer_id = None

        # watch folder file path -> (size, modification time) seen on the previous scan
        self._watch_snapshots = {}

        for event_name, callback in (
            ("file-search-response", self._file_search_response),
            ("quit", self._quit),
            ("start", self._start),
            ("update-download", self._update_download)
        ):
            events.connect(event_name, callback)

    @property
    def stall_timeout(self):
        return config.sections["transfers"]["downloadliststalltimeout"]

    @property
    def min_transfer_speed(self):
        """Bytes/sec, converted from the user-facing KiB/s setting."""
        return config.sections["transfers"]["downloadlistminspeed"] * 1024

    @property
    def max_concurrent_downloads(self):
        """How many items (across every list combined, since they share one
        dispatch queue) can be Searching/Downloading at once."""
        return config.sections["transfers"]["downloadlistmaxconcurrent"]

    def _start(self):

        self._load()
        self._allow_saving = True

        # Save download lists every 3 minutes
        events.schedule(delay=180, callback=self._save, repeat=True)

        # Poll the watch folder for song list files exported by other applications
        self._watch_timer_id = events.schedule(
            delay=self.WATCH_INTERVAL, callback=self._scan_watch_folder, repeat=True)

        # Safety net: periodically catch any Downloading item whose transfer actually
        # finished without that ever reaching the item (see _reconcile_downloading_items)
        self._reconcile_timer_id = events.schedule(
            delay=self.RECONCILE_INTERVAL, callback=self._reconcile_downloading_items, repeat=True)

    def _quit(self):

        for download_list in self.lists.values():
            for item in download_list.items.values():
                events.cancel_scheduled(item.collect_timer_id)
                events.cancel_scheduled(item.escalation_timer_id)

        events.cancel_scheduled(self._dispatch_timer_id)
        events.cancel_scheduled(self._watch_timer_id)
        events.cancel_scheduled(self._reconcile_timer_id)

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

        lists_data = load_file(self.file_path, self._load_file)

        for list_data in lists_data:
            name = list_data.get("name")

            if not name:
                continue

            items = {}

            for item_data in list_data.get("items", []):
                term = item_data.get("term")

                if not term:
                    continue

                attributes = None

                if item_data.get("download_bitrate") is not None or item_data.get("download_bit_depth") is not None:
                    attributes = FileAttributes(
                        bitrate=item_data.get("download_bitrate"),
                        length=item_data.get("download_length"),
                        vbr=item_data.get("download_vbr"),
                        sample_rate=item_data.get("download_sample_rate"),
                        bit_depth=item_data.get("download_bit_depth")
                    )

                items[term] = DownloadListItem(
                    term=term, list_name=name, time_added=item_data.get("time_added"),
                    status=item_data.get("status", DownloadListItemStatus.PENDING),
                    searched_term=item_data.get("searched_term"),
                    suggestions=item_data.get("suggestions"),
                    rejected_files=item_data.get("rejected_files"),
                    num_quality_rejections=item_data.get("num_quality_rejections", 0),
                    quality_cutoff_hz=item_data.get("quality_cutoff_hz"),
                    download_username=item_data.get("download_username"),
                    download_virtual_path=item_data.get("download_virtual_path"),
                    download_size=item_data.get("download_size", 0),
                    download_attributes=attributes,
                    download_match_percentage=item_data.get("download_match_percentage"),
                    library_location=item_data.get("library_location")
                )

            self.lists[name] = DownloadList(
                name=name,
                download_folder_path=list_data.get("download_folder_path"),
                quality=list_data.get("quality"),
                prefer_longer=list_data.get("prefer_longer"),
                prefer_lossless=list_data.get("prefer_lossless"),
                preferred_keywords=list_data.get("preferred_keywords"),
                fuzzy_match_threshold=list_data.get("fuzzy_match_threshold"),
                auto_download=list_data.get("auto_download"),
                use_name_subfolder=list_data.get("use_name_subfolder"),
                pinned=list_data.get("pinned", False),
                time_added=list_data.get("time_added"),
                items=items
            )

        # Guarantee the pinned-ahead-of-unpinned invariant _pop_next_queued_entry and
        # the GUI sidebar rely on, in case saved data predates it or was hand-edited.
        # sorted() is stable, so relative order within each group is preserved
        self.lists = {
            name: self.lists[name]
            for name in sorted(self.lists, key=lambda list_name: not self.lists[list_name].pinned)
        }

        # Re-queue anything left pending from a previous session
        for download_list in self.lists.values():
            if not download_list.effective_auto_download:
                continue

            for item in download_list.items.values():
                if item.status == DownloadListItemStatus.PENDING:
                    self._queue.append((download_list.name, item.term))

        self._kick_queue()

    def _save_callback(self, file_handle):

        # Dump every list individually to avoid large memory usage
        json_encoder = json.JSONEncoder(check_circular=False, ensure_ascii=False)
        is_first_item = True

        file_handle.write("[")

        for download_list in self.lists.values():
            if is_first_item:
                is_first_item = False
            else:
                file_handle.write(",\n")

            file_handle.write(json_encoder.encode(download_list.as_dict()))

        file_handle.write("]")

    def _save(self):

        if not self._allow_saving:
            return

        config.create_data_folder()
        write_file_and_backup(self.file_path, self._save_callback)

    # List Management #

    def add_list(self, name, download_folder_path=None, quality=None, prefer_longer=None,
                 prefer_lossless=None, preferred_keywords=None, fuzzy_match_threshold=None,
                 auto_download=None, use_name_subfolder=None):
        """quality/prefer_longer/prefer_lossless/preferred_keywords/fuzzy_match_threshold/
        auto_download/use_name_subfolder default to None, meaning the list follows the
        overall wishlist defaults until overridden."""

        name = name.strip()

        if not name or name in self.lists:
            return None

        self.lists[name] = download_list = DownloadList(
            name=name, download_folder_path=download_folder_path, quality=quality,
            prefer_longer=prefer_longer, prefer_lossless=prefer_lossless,
            preferred_keywords=preferred_keywords, fuzzy_match_threshold=fuzzy_match_threshold,
            auto_download=auto_download, use_name_subfolder=use_name_subfolder
        )

        events.emit("add-download-list", name)
        self._save()

        return download_list

    def update_list_settings(self, name, download_folder_path=_UNSET, quality=_UNSET, prefer_longer=_UNSET,
                             prefer_lossless=_UNSET, preferred_keywords=_UNSET, fuzzy_match_threshold=_UNSET,
                             auto_download=_UNSET, use_name_subfolder=_UNSET):
        """Each argument left at _UNSET (the default) is untouched. Passing an explicit
        None for quality/prefer_longer/prefer_lossless/preferred_keywords/
        fuzzy_match_threshold/auto_download/use_name_subfolder clears this list's
        override, so it follows the overall wishlist default instead."""

        download_list = self.lists.get(name)

        if download_list is None:
            return

        was_auto_download = download_list.effective_auto_download

        if download_folder_path is not _UNSET:
            download_list.download_folder_path = download_folder_path or None

        if quality is not _UNSET:
            download_list.quality = quality or None

        if prefer_longer is not _UNSET:
            download_list.prefer_longer = prefer_longer

        if prefer_lossless is not _UNSET:
            download_list.prefer_lossless = prefer_lossless

        if preferred_keywords is not _UNSET:
            download_list.preferred_keywords = preferred_keywords or None

        if fuzzy_match_threshold is not _UNSET:
            download_list.fuzzy_match_threshold = fuzzy_match_threshold

        if auto_download is not _UNSET:
            download_list.auto_download = auto_download

        if use_name_subfolder is not _UNSET:
            download_list.use_name_subfolder = use_name_subfolder

        if download_list.effective_auto_download and not was_auto_download:
            # Resuming a paused list: re-queue anything still pending
            for item in download_list.items.values():
                if item.status == DownloadListItemStatus.PENDING:
                    self._queue.append((name, item.term))

            self._kick_queue()

        events.emit("update-download-list", name)
        self._save()

    def pause_list(self, name):
        """Pause a list: stop searching for and downloading its remaining pending items.
        Items already downloading or completed are left alone."""

        self.update_list_settings(name, auto_download=False)

    def resume_list(self, name):
        """Resume a paused list, re-queuing anything still pending."""

        self.update_list_settings(name, auto_download=True)

    def update_wishlist_default_settings(self, quality, prefer_longer, prefer_lossless, preferred_keywords,
                                         fuzzy_match_threshold, auto_download, use_name_subfolder,
                                         apply_to_existing_lists=False):
        """Set the overall defaults new lists start with, and that any list without its
        own override follows. By default, existing lists that override a given setting
        are unaffected; pass apply_to_existing_lists=True to clear every list's override
        for these settings, switching all of them over immediately (their download
        folder, which isn't one of these settings, is left untouched either way)."""

        # Lists inheriting the default (auto_download is None) that are about to go from
        # paused to active need their pending items re-queued, just like update_list_settings
        # does for a single list. Not needed when applying to every list below, since that
        # path calls update_list_settings per list, which already handles this itself.
        newly_active = [] if apply_to_existing_lists else [
            name for name, download_list in self.lists.items()
            if download_list.auto_download is None and not download_list.effective_auto_download and auto_download
        ]

        config.sections["transfers"]["downloadlistdefaultquality"] = quality
        config.sections["transfers"]["downloadlistdefaultpreferlonger"] = bool(prefer_longer)
        config.sections["transfers"]["downloadlistdefaultpreferlossless"] = bool(prefer_lossless)
        config.sections["transfers"]["downloadlistdefaultkeywords"] = preferred_keywords or ""
        config.sections["transfers"]["downloadlistdefaultfuzzy"] = int(fuzzy_match_threshold)
        config.sections["transfers"]["downloadlistdefaultautodownload"] = bool(auto_download)
        config.sections["transfers"]["downloadlistdefaultnamesubfolder"] = bool(use_name_subfolder)

        config.write_configuration()

        if apply_to_existing_lists:
            for name in list(self.lists):
                self.update_list_settings(
                    name, quality=None, prefer_longer=None, prefer_lossless=None, preferred_keywords=None,
                    fuzzy_match_threshold=None, auto_download=None, use_name_subfolder=None
                )
            return

        for name in newly_active:
            download_list = self.lists[name]

            for item in download_list.items.values():
                if item.status == DownloadListItemStatus.PENDING:
                    self._queue.append((name, item.term))

        if newly_active:
            self._kick_queue()

        # Lists that don't override these settings are affected, refresh their display
        for name in self.lists:
            events.emit("update-download-list", name)

    def update_watch_folder_settings(self, enabled, folder_path):
        """Enable/disable and point the watch folder at a new location.

        Takes effect on the next poll; snapshots are cleared so a folder
        switch doesn't carry over stale state from the previous location.
        """

        config.sections["transfers"]["downloadlistwatchenabled"] = bool(enabled)
        config.sections["transfers"]["downloadlistwatchfolder"] = folder_path or ""

        self._watch_snapshots.clear()
        config.write_configuration()

    def update_stall_settings(self, stall_timeout, min_speed_kib):
        """How long a download can stay below min_speed_kib (KiB/s) before it's
        abandoned and searched again. Applies to every list; what counts as
        "too slow" depends on the user's own connection, not a given list."""

        config.sections["transfers"]["downloadliststalltimeout"] = max(1, int(stall_timeout))
        config.sections["transfers"]["downloadlistminspeed"] = max(0, int(min_speed_kib))

        config.write_configuration()

    @property
    def quality_check_enabled(self):
        return config.sections["transfers"]["downloadlistqualitycheck"] and audioquality.decoder_available()

    def update_quality_check_setting(self, enabled):
        config.sections["transfers"]["downloadlistqualitycheck"] = bool(enabled)
        config.write_configuration()

    def update_max_concurrent_downloads(self, max_concurrent):
        """How many items (across every list) can be Searching/Downloading at once."""

        config.sections["transfers"]["downloadlistmaxconcurrent"] = max(1, int(max_concurrent))
        config.write_configuration()

        # More headroom may have just opened up; let the queue take advantage of it
        self._kick_queue()

    @staticmethod
    def _move_list_folder(old_folder_path, new_folder_path):
        """Move a renamed list's on-disk subfolder to match, merging into an
        existing destination (e.g. a stale folder from an earlier list of the
        same name) file by file rather than overwriting it outright."""

        old_encoded = encode_path(old_folder_path)

        if not os.path.isdir(old_encoded):
            # Nothing downloaded under the old name yet, nothing to move
            return

        new_encoded = encode_path(new_folder_path)

        try:
            if not os.path.exists(new_encoded):
                parent_folder_path = os.path.dirname(new_folder_path)

                if parent_folder_path:
                    os.makedirs(encode_path(parent_folder_path), exist_ok=True)

                shutil.move(old_encoded, new_encoded)
                return

            # Destination already exists: merge contents in rather than overwriting it
            with os.scandir(old_encoded) as entries:
                basenames = [entry.name.decode("utf-8", "replace") for entry in entries]

            for basename in basenames:
                source_path = os.path.join(old_folder_path, basename)
                target_path = os.path.join(new_folder_path, basename)
                name_root, extension = os.path.splitext(basename)
                counter = 1

                while os.path.exists(encode_path(target_path)):
                    target_path = os.path.join(new_folder_path, f"{name_root} ({counter}){extension}")
                    counter += 1

                shutil.move(encode_path(source_path), encode_path(target_path))

            os.rmdir(old_encoded)

        except OSError as error:
            log.add(_("Cannot move download list folder from %(old)s to %(new)s: %(error)s"),
                    {"old": old_folder_path, "new": new_folder_path, "error": error})

    def set_list_pinned(self, name, pinned):
        """A pinned list always stays at the top of the GUI's Active section,
        ahead of every unpinned list, and never moves to Completed even once
        every item is done -- for a list the user wants to keep adding to and
        always see first. Pinned lists can only be reordered against other
        pinned lists (and likewise unpinned ones against each other) -- see
        reorder_lists/_swap_list_priority -- so pinning/unpinning here also
        moves the list to the back of the pinned block / front of the
        unpinned block, keeping that grouping intact."""

        download_list = self.lists.get(name)

        if download_list is None or download_list.pinned == bool(pinned):
            return

        download_list.pinned = bool(pinned)
        self._regroup_pinned_lists(name)

        events.emit("update-download-list", name)
        events.emit("reorder-download-lists")
        self._save()

        if download_list.pinned:
            self._kick_queue()

    def _regroup_pinned_lists(self, name):
        """Move name to the back of the pinned block if it was just pinned, or
        to the front of the unpinned block if it was just unpinned, keeping
        pinned lists always grouped ahead of unpinned ones in priority order."""

        names = list(self.lists.keys())
        names.remove(name)

        insert_at = sum(1 for other in names if self.lists[other].pinned)
        names.insert(insert_at, name)

        self.lists = {list_name: self.lists[list_name] for list_name in names}

    def _active_list_names(self):
        """Names of lists in the GUI's Active section, in current priority
        order -- everything except a completed, unpinned list."""

        return [name for name, download_list in self.lists.items()
                if download_list.pinned or not download_list.is_complete]

    def move_list_up(self, name):
        """Raise a list's priority relative to its neighbors — earlier in this
        order means its queued items are dispatched first (see
        _pop_next_queued_entry) and it's shown higher in the GUI sidebar."""

        self._swap_list_priority(name, -1)

    def move_list_down(self, name):
        self._swap_list_priority(name, 1)

    def _swap_list_priority(self, name, direction):
        """Swap name with its neighbor (direction -1 for up, +1 for down)
        among lists in the same priority group -- pinned lists only reorder
        among other pinned lists, and likewise for unpinned/active ones -- so
        a swap can't cross the pinned/unpinned boundary or touch a completed,
        unpinned list, which has no meaningful priority anymore."""

        download_list = self.lists.get(name)

        if download_list is None:
            return

        group = [
            list_name for list_name, other in self.lists.items()
            if other.pinned == download_list.pinned and (other.pinned or not other.is_complete)
        ]

        index = group.index(name)
        swap_index = index + direction

        if swap_index < 0 or swap_index >= len(group):
            return

        swap_name = group[swap_index]

        names = list(self.lists.keys())
        i, j = names.index(name), names.index(swap_name)
        names[i], names[j] = names[j], names[i]

        self.lists = {list_name: self.lists[list_name] for list_name in names}

        events.emit("reorder-download-lists")
        self._save()

    def reorder_lists(self, ordered_names):
        """Apply a full new priority order for the Active section's lists --
        e.g. from a drag-and-drop reorder in the GUI sidebar, where the top
        row is priority 1. Pinned lists always stay grouped ahead of unpinned
        ones: each tier's relative order is taken from ordered_names, but a
        drag that crossed the pinned/unpinned boundary is snapped back into
        the correct group rather than breaking that invariant. Lists not in
        the Active section (completed and unpinned) keep their existing
        relative order, appended after it.

        ordered_names must include every current Active-section list exactly
        once, or this is a no-op -- a partial list would otherwise risk
        silently losing track of one."""

        active_names = set(self._active_list_names())
        new_order = [name for name in ordered_names if name in active_names]

        if len(new_order) != len(active_names) or len(set(new_order)) != len(new_order):
            return

        pinned_order = [name for name in new_order if self.lists[name].pinned]
        unpinned_order = [name for name in new_order if not self.lists[name].pinned]
        remaining = [name for name in self.lists if name not in active_names]

        self.lists = {name: self.lists[name] for name in pinned_order + unpinned_order + remaining}

        events.emit("reorder-download-lists")
        self._save()

    def rename_list(self, old_name, new_name):

        new_name = new_name.strip()

        if not new_name or old_name not in self.lists or new_name in self.lists:
            return False

        # Preserve the list's priority position — a plain pop+reinsert would
        # silently drop it to the back, behind every other list
        original_order = list(self.lists.keys())
        download_list = self.lists.pop(old_name)

        # Only lists saving into a subfolder named after themselves have a folder
        # tied to the list name; a list with an explicit fixed folder is unaffected
        old_folder_path = (
            download_list.effective_download_folder_path if download_list.effective_use_name_subfolder else None)

        download_list.name = new_name

        for item in download_list.items.values():
            item.list_name = new_name

        self.lists[new_name] = download_list
        ordered_names = [new_name if list_name == old_name else list_name for list_name in original_order]
        self.lists = {list_name: self.lists[list_name] for list_name in ordered_names}

        self._queue = deque(
            (new_name if list_name == old_name else list_name, term) for list_name, term in self._queue)

        for token, (list_name, term) in list(self._token_map.items()):
            if list_name == old_name:
                self._token_map[token] = (new_name, term)

        active_transfer_keys = []

        for key, (list_name, term) in list(self._transfer_map.items()):
            if list_name == old_name:
                self._transfer_map[key] = (new_name, term)
                active_transfer_keys.append(key)

        if old_folder_path is not None:
            new_folder_path = download_list.effective_download_folder_path

            if new_folder_path != old_folder_path:
                self._move_list_folder(old_folder_path, new_folder_path)

                # Any download still in flight for this list was enqueued with the
                # old folder path baked in; point it at the new one so it lands in
                # the right place once it finishes, instead of recreating the old folder
                for transfer_key in active_transfer_keys:
                    transfer = core.downloads.transfers.get(transfer_key)

                    if transfer is not None and transfer.folder_path == old_folder_path:
                        transfer.folder_path = new_folder_path

        events.emit("rename-download-list", old_name, new_name)
        self._save()

        return True

    def remove_list(self, name):

        download_list = self.lists.pop(name, None)

        if download_list is None:
            return

        for item in download_list.items.values():
            self._forget_item(item)

        self._queue = deque((list_name, term) for list_name, term in self._queue if list_name != name)

        events.emit("remove-download-list", name)
        self._save()

    def add_list_items(self, name, terms):
        """Add multiple search terms (e.g. one per line) to a list."""

        download_list = self.lists.get(name)

        if download_list is None:
            return

        # With library-first Lexicon sync on, new items first wait for a
        # library lookup (LexiconSync picks them up off the update event
        # below) instead of going straight to the search queue -- a song the
        # user already owns shouldn't be re-downloaded. LexiconSync releases
        # each item back to Pending if the library doesn't have it.
        library_first = (
            core.lexicon_sync is not None
            and config.sections["lexicon"]["sync_enabled"]
            and config.sections["lexicon"]["library_first"]
        )

        added_any = False

        for term in terms:
            term = term.strip()

            if not term or term in download_list.items:
                continue

            item = DownloadListItem(term=term, list_name=name)
            download_list.items[term] = item
            added_any = True

            if not download_list.effective_auto_download:
                continue

            if library_first:
                item.status = DownloadListItemStatus.LIBRARY_CHECK
            else:
                self._queue.append((name, term))

        if not added_any:
            return

        events.emit("update-download-list", name)
        self._save()

        if download_list.effective_auto_download:
            self._kick_queue()

    def resolve_library_check(self, name, term, found, library_location=None):
        """Outcome of a Lexicon library lookup for a waiting item: found means
        the user already owns the song (mark In Library, no download); not
        found releases the item into the normal search queue."""

        download_list = self.lists.get(name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None or item.status != DownloadListItemStatus.LIBRARY_CHECK:
            return

        if found:
            item.status = DownloadListItemStatus.IN_LIBRARY
            item.library_location = library_location
            item.download_percent = 100

            events.emit("update-download-list-item", name, term)
            self._save()
            self._check_list_complete(name)
            return

        item.status = DownloadListItemStatus.PENDING

        if download_list.effective_auto_download:
            self._queue.append((name, term))
            self._kick_queue()

        events.emit("update-download-list-item", name, term)
        self._save()

    def release_library_check_items(self, name=None):
        """Give up waiting for a Lexicon library check ("continue and
        download" on the unreachable prompt, or the feature being turned
        off): send every waiting item to the normal search queue."""

        released_any = False

        for download_list in self.lists.values():
            if name is not None and download_list.name != name:
                continue

            for term, item in download_list.items.items():
                if item.status != DownloadListItemStatus.LIBRARY_CHECK:
                    continue

                item.status = DownloadListItemStatus.PENDING
                released_any = True

                if download_list.effective_auto_download:
                    self._queue.append((download_list.name, term))

                events.emit("update-download-list-item", download_list.name, term)

        if released_any:
            self._save()
            self._kick_queue()

    def remove_list_item(self, name, term):

        download_list = self.lists.get(name)

        if download_list is None:
            return

        item = download_list.items.pop(term, None)

        if item is None:
            return

        self._forget_item(item)
        self._queue = deque(
            (list_name, item_term) for list_name, item_term in self._queue
            if not (list_name == name and item_term == term)
        )

        events.emit("update-download-list", name)
        self._save()

    def _emit_item_finished(self, list_name, item):
        """Announce a completed item's on-disk file, for listeners that act on
        the finished download itself (e.g. Lexicon sync importing it)."""

        download_list = self.lists.get(list_name)

        if download_list is None or not item.download_username or not item.download_virtual_path:
            return

        file_path, file_exists = core.downloads.get_complete_download_file_path(
            item.download_username, item.download_virtual_path, item.download_size,
            download_folder_path=download_list.effective_download_folder_path
        )

        if file_exists:
            events.emit("download-list-item-finished", list_name, item.term, file_path)

    def _complete_item_from_transfer(self, name, term, item, transfer):
        """If the given transfer for a Downloading item has already finished,
        mark the item Completed directly from it — using only the transfer
        itself and data already on the item, no _transfer_map lookup required
        — and return True. This is the one source of truth for 'did this
        download actually succeed', used any time we need to double check
        before discarding a Downloading item (resetting it, or the periodic
        reconciliation sweep below), since _transfer_map is just an index that
        can end up stale/orphaned relative to it."""

        if transfer is None or transfer.status != TransferStatus.FINISHED:
            return False

        transfer_key = item.download_username + (item.download_virtual_path or "")
        self._transfer_map.pop(transfer_key, None)

        item.status = DownloadListItemStatus.COMPLETED
        item.download_percent = 100
        self._forget_item(item)

        events.emit("update-download-list-item", name, term)
        self._emit_item_finished(name, item)
        self._save()
        self._check_list_complete(name)
        return True

    def _reconcile_downloading_items(self):
        """Periodic safety net: for every item still marked Downloading, check
        whether its transfer actually finished without that ever being routed
        back to the item (e.g. it failed, dropped out of _transfer_map without
        completing at the time, and was later retried/resumed to success by
        the transfer subsystem outside of anything download lists initiated).
        Catches this regardless of exactly how the routing was lost, rather
        than only the specific paths (Reset, the stall handler) that are
        otherwise guarded against it directly."""

        for name, download_list in list(self.lists.items()):
            for term, item in list(download_list.items.items()):
                if item.status != DownloadListItemStatus.DOWNLOADING or not item.download_username:
                    continue

                transfer_key = item.download_username + (item.download_virtual_path or "")
                transfer = core.downloads.transfers.get(transfer_key)

                self._complete_item_from_transfer(name, term, item, transfer)

    def reset_list_item(self, name, term):
        """Forget an item's search/download progress so it can be retried."""

        download_list = self.lists.get(name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None:
            return

        if item.status == DownloadListItemStatus.DOWNLOADING and item.download_username:
            transfer_key = item.download_username + (item.download_virtual_path or "")
            transfer = core.downloads.transfers.get(transfer_key)

            if self._complete_item_from_transfer(name, term, item, transfer):
                # This download actually already succeeded — Reset must never
                # discard a download that, in fact, already finished
                return

            if transfer is not None:
                # Genuinely still in progress: actually cancel it, not just stop
                # tracking it — otherwise it keeps running in the background,
                # finishes on its own a moment later, and the file lands in the
                # download folder while this item has already moved on to a new
                # search, looking like nothing happened even though it did download
                core.downloads.abort_downloads([transfer], status=TransferStatus.CANCELLED)
                core.downloads.clear_downloads([transfer])

        self._clear_item_progress(item)

        if download_list.effective_auto_download:
            self._queue.append((name, term))
            self._kick_queue()

        events.emit("update-download-list-item", name, term)
        self._save()

    def _clear_item_progress(self, item):
        """Back to a freshly added item: no search or download state left."""

        self._forget_item(item)

        item.status = DownloadListItemStatus.PENDING
        item.searched_term = None
        item.download_username = None
        item.download_virtual_path = None
        item.download_size = 0
        item.download_attributes = None
        item.download_match_percentage = None
        item.download_percent = 0
        item.quality_cutoff_hz = None
        item.suggestions = []
        item.near_misses = {}

    def retarget_list_item(self, name, term, new_term):
        """Replace an item's search term (e.g. with one of its Not Found
        suggestions, or a corrected spelling) in place, keeping its position
        in the list, and search for it afresh."""

        download_list = self.lists.get(name)
        item = download_list.items.get(term) if download_list is not None else None
        new_term = new_term.strip()

        if item is None or not new_term or new_term == term:
            return

        if new_term in download_list.items:
            # Already listed under the new spelling: just drop this duplicate
            self.remove_list_item(name, term)
            return

        self._clear_item_progress(item)
        item.term = new_term
        download_list.items = {
            (new_term if key == term else key): value for key, value in download_list.items.items()}

        if download_list.effective_auto_download:
            self._queue.append((name, new_term))
            self._kick_queue()

        events.emit("update-download-list", name)
        self._save()

    def suggestion_term(self, filename):
        """A search term made from a suggested file's name: no extension, track
        number or punctuation ("04. Bicep - Opal [Four Tet Rmx].mp3" ->
        "Bicep Opal Four Tet Rmx")."""

        stem = os.path.splitext(filename.replace("\\", "/").rsplit("/", 1)[-1])[0]
        stem = self.LEADING_TRACK_NUMBER_PATTERN.sub("", stem)
        return self._sanitize_text(stem)

    def _record_near_miss(self, item, match_percentage, filename):
        """Remember a result that wasn't good enough to download, for the
        suggestions shown if the item ends up Not Found."""

        if match_percentage < self.NEAR_MISS_MIN_PERCENT:
            return

        key = filename.lower()
        previous = item.near_misses.get(key)

        if previous is not None and previous[0] >= match_percentage:
            return

        item.near_misses[key] = (match_percentage, item.searched_term or item.term, filename)

        if len(item.near_misses) > self.MAX_SUGGESTIONS * 4:
            # Keep the collection bounded on a busy network: drop the weakest
            weakest_key = min(item.near_misses, key=lambda near_key: item.near_misses[near_key][0])
            del item.near_misses[weakest_key]

    def _settle_suggestions(self, item):
        """Turn the near misses collected while searching into the item's
        persisted suggestions, best first."""

        ranked = sorted(item.near_misses.values(), key=itemgetter(0), reverse=True)
        item.suggestions = [
            {"filename": filename, "match": round(match_percentage), "searched_term": searched_term}
            for match_percentage, searched_term, filename in ranked[:self.MAX_SUGGESTIONS]
        ]
        item.near_misses = {}

    def reset_not_found_items(self, name=None):
        """Search again for every item marked Not Found -- in one list, or in
        all of them -- as if it had just been added: e.g. once peers that were
        offline are back, or after the matching rules changed. Returns how
        many items were reset."""

        num_reset = 0

        for download_list in self.lists.values():
            if name is not None and download_list.name != name:
                continue

            for term, item in download_list.items.items():
                if item.status != DownloadListItemStatus.NOT_FOUND:
                    continue

                self._clear_item_progress(item)
                num_reset += 1

                if download_list.effective_auto_download:
                    self._queue.append((download_list.name, term))

                events.emit("update-download-list-item", download_list.name, term)

        if num_reset:
            self._save()
            self._kick_queue()

        return num_reset

    def start_item_next(self, name, term):
        """Move an item to the front of the dispatch queue, so it's searched
        next instead of waiting its turn — resetting it first if it isn't
        already pending. No-op for an item that's already active, or a
        list that isn't currently auto-downloading."""

        download_list = self.lists.get(name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None or item.status in (
                DownloadListItemStatus.SEARCHING, DownloadListItemStatus.DOWNLOADING):
            return

        if download_list is None or not download_list.effective_auto_download:
            return

        if item.status != DownloadListItemStatus.PENDING:
            self.reset_list_item(name, term)

        # Drop any existing queue entry for this item before placing it at the
        # front, so it doesn't end up queued twice
        self._queue = deque((n, t) for n, t in self._queue if not (n == name and t == term))
        self._queue.appendleft((name, term))

        self._kick_queue()

    def get_summary_rows(self, name):

        download_list = self.lists.get(name)

        if download_list is None:
            return []

        return [
            {
                "term": item.term,
                "searched_term": item.searched_term or "",
                "status": self.STATUS_LABELS.get(item.status, item.status),
                "downloaded_file": item.download_filename,
                "user": item.download_username or "",
                "quality": item.h_quality,
                "length": item.h_length
            }
            for item in download_list.items.values()
        ]

    def export_summary_csv(self, name, file_path):

        rows = self.get_summary_rows(name)

        with open(encode_path(file_path), "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                _("Search Term"), _("Searched Term"), _("Status"), _("Downloaded File"),
                _("User"), _("Quality"), _("Length")
            ])

            for row in rows:
                writer.writerow([
                    row["term"], row["searched_term"], row["status"], row["downloaded_file"],
                    row["user"], row["quality"], row["length"]
                ])

    # Watch Folder #

    def _scan_watch_folder(self):
        """Poll the watch folder for song list files exported by other applications.

        A file is only imported once its size and modification time are unchanged
        between two consecutive scans, so that files still being written are not
        read while incomplete.
        """

        if not config.sections["transfers"]["downloadlistwatchenabled"]:
            self._watch_snapshots.clear()
            return

        folder_path = config.sections["transfers"]["downloadlistwatchfolder"]

        if not folder_path:
            return

        current_snapshots = {}

        try:
            with os.scandir(encode_path(folder_path)) as entries:
                for entry in entries:
                    basename = entry.name.decode("utf-8", "replace")

                    if not basename.lower().endswith(self.WATCH_FILE_EXTENSIONS):
                        continue

                    if not entry.is_file():
                        continue

                    stat_result = entry.stat()
                    current_snapshots[basename] = (stat_result.st_size, stat_result.st_mtime)

        except OSError as error:
            log.add(_("Cannot open download list watch folder %(folder)s: %(error)s"),
                    {"folder": folder_path, "error": error})
            return

        for basename, snapshot in current_snapshots.items():
            if self._watch_snapshots.get(basename) == snapshot:
                self._import_watch_file(folder_path, basename)

        self._watch_snapshots = current_snapshots

    @staticmethod
    def _read_watch_file(file_path):
        """Read a song list file, one search term per line.

        utf-8-sig transparently strips a byte order mark, which text files exported
        by other applications often start with. splitlines() handles both CRLF and
        LF line endings.
        """

        try:
            with open(encode_path(file_path), encoding="utf-8-sig") as handle:
                contents = handle.read()

        except UnicodeDecodeError:
            with open(encode_path(file_path), encoding="latin-1") as handle:
                contents = handle.read()

        terms = []

        for line in contents.splitlines():
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            terms.append(line)

        return terms

    def _move_imported_watch_file(self, folder_path, basename):
        """Move an imported file into the 'imported' subfolder, so it is not read again."""

        imported_folder_path = os.path.join(folder_path, self.WATCH_IMPORTED_FOLDER_NAME)
        source_path = os.path.join(folder_path, basename)
        target_path = os.path.join(imported_folder_path, basename)
        name_root, extension = os.path.splitext(basename)
        counter = 1

        os.makedirs(encode_path(imported_folder_path), exist_ok=True)

        while os.path.exists(encode_path(target_path)):
            target_path = os.path.join(imported_folder_path, f"{name_root} ({counter}){extension}")
            counter += 1

        os.replace(encode_path(source_path), encode_path(target_path))

    def _import_watch_file(self, folder_path, basename):
        """Import a single song list file into a list named after the file."""

        file_path = os.path.join(folder_path, basename)
        list_name = os.path.splitext(basename)[0].strip()

        try:
            terms = self._read_watch_file(file_path)

        except OSError as error:
            log.add(_("Cannot read download list file %(path)s: %(error)s"),
                    {"path": file_path, "error": error})
            return

        if not list_name:
            # Nothing usable in the file, but still move it aside so it is not rescanned
            terms = []

        if terms:
            if list_name not in self.lists:
                self.add_list(list_name)

            self.add_list_items(list_name, terms)

        try:
            self._move_imported_watch_file(folder_path, basename)

        except OSError as error:
            log.add(_("Cannot move imported download list file %(path)s: %(error)s"),
                    {"path": file_path, "error": error})
            return

        log.add(_('Imported %(num)i songs from "%(path)s" into download list "%(list)s"'),
                {"num": len(terms), "path": basename, "list": list_name})

    # Search Dispatch #

    def _next_token(self):
        self._token = increment_token(self._token)
        return self._token

    def owns_transfer(self, username, virtual_path):
        """Whether a download was started by a download list (as opposed to
        queued by hand from a search or browse)."""
        return (username + virtual_path) in self._transfer_map

    def _peer_is_busy(self, username):
        """Whether a peer should be passed over for another source, if there
        is one: it has recently rejected us for having too many files queued,
        a download the user queued by hand is still waiting in its queue
        (that one goes first), or our lists already have their share of
        downloads there (see MAX_DOWNLOADS_PER_PEER)."""

        downloads = core.downloads

        if downloads.is_user_queue_limited(username):
            return True

        for users in (downloads.queued_users, downloads.failed_users):
            for virtual_path, transfer in users.get(username, {}).items():
                if transfer.status == TransferStatus.QUEUED and not self.owns_transfer(username, virtual_path):
                    return True

        num_active = sum(
            1
            for download_list in self.lists.values()
            for item in download_list.items.values()
            if item.status == DownloadListItemStatus.DOWNLOADING and item.download_username == username
        )

        return num_active >= self.MAX_DOWNLOADS_PER_PEER

    def _forget_search(self, item):
        """Stop tracking an item's in-flight search state (timers, collected
        candidates, and token bookkeeping). Leaves any active download
        transfer mapping untouched."""

        events.cancel_scheduled(item.collect_timer_id)
        events.cancel_scheduled(item.escalation_timer_id)
        item.collect_timer_id = None
        item.escalation_timer_id = None
        item.download_candidates = []

        if item.token is not None:
            self._token_map.pop(item.token, None)

            if core.search is not None:
                core.search.searches.pop(item.token, None)

            core.send_message_to_network_thread(RemoveAllowedResponse(FileSearchResponse, item.token))
            item.token = None

    def _forget_item(self, item):
        """Stop tracking an item's in-flight search AND download state, e.g.
        when the item is removed or reset."""

        self._forget_search(item)

        events.cancel_scheduled(item.stall_timer_id)
        item.stall_timer_id = None

        transfer_key = next(
            (key for key, value in self._transfer_map.items() if value == (item.list_name, item.term)), None)

        if transfer_key is not None:
            del self._transfer_map[transfer_key]

    def _kick_queue(self):

        if self._dispatch_timer_id is not None:
            return

        self._pump_queue()

    def _count_active_items(self):
        return sum(
            1
            for download_list in self.lists.values()
            for item in download_list.items.values()
            if item.status in (DownloadListItemStatus.SEARCHING, DownloadListItemStatus.DOWNLOADING)
        )

    def _pop_next_queued_entry(self):
        """Pop the next (list_name, term) to dispatch, in list priority order --
        self.lists key order (top of the GUI sidebar = priority 1), set by
        drag-and-drop/Move Up/Down (see reorder_lists/_swap_list_priority) and
        otherwise by insertion order. Entries from the same list (equal
        priority) keep plain queue (FIFO) order relative to each other."""

        list_order = {name: index for index, name in enumerate(self.lists)}

        best_index = 0
        best_priority = list_order.get(self._queue[0][0], len(list_order))

        for index in range(1, len(self._queue)):
            priority = list_order.get(self._queue[index][0], len(list_order))

            if priority < best_priority:
                best_priority = priority
                best_index = index

        entry = self._queue[best_index]
        del self._queue[best_index]

        return entry

    def _pump_queue(self):

        self._dispatch_timer_id = None

        if core.users.login_status == UserStatus.OFFLINE:
            self._dispatch_timer_id = events.schedule(delay=self.DISPATCH_DELAY, callback=self._pump_queue)
            return

        # Fill up to max_concurrent_downloads in one go rather than trickling a
        # single dispatch out every DISPATCH_DELAY — the pacing below still
        # applies between refill checks once at capacity, so a big list doesn't
        # flood the server, but reaching the user's chosen concurrency shouldn't
        # need waiting several times DISPATCH_DELAY just to get going
        headroom = self.max_concurrent_downloads - self._count_active_items()

        while headroom > 0 and self._queue:
            name, term = self._pop_next_queued_entry()
            download_list = self.lists.get(name)
            item = download_list.items.get(term) if download_list is not None else None

            if (download_list is None or item is None or not download_list.effective_auto_download
                    or item.status != DownloadListItemStatus.PENDING):
                continue

            self._dispatch_item(download_list, item)
            headroom -= 1

        if self._queue:
            self._dispatch_timer_id = events.schedule(delay=self.DISPATCH_DELAY, callback=self._pump_queue)

    def _sanitize_text(self, text):
        text = text.translate(self.REMOVED_SEARCH_CHARACTERS)
        return self.COLLAPSE_WHITESPACE_PATTERN.sub(" ", text).strip()

    @staticmethod
    def _fold_text(text):
        """Lowercase and strip accents ("Lágrimas" -> "lagrimas"), so a term and
        a filename that only differ in diacritics still match each other."""

        decomposed = unicodedata.normalize("NFKD", text)
        return "".join(char for char in decomposed if not unicodedata.combining(char)).lower()

    @classmethod
    def _text_tokens(cls, text):
        return cls.TEXT_TOKEN_PATTERN.findall(text)

    def _term_words(self, term):
        return [
            word for word in self._fold_text(self._sanitize_text(term)).split()
            if word and word not in self.IGNORED_TERM_WORDS
        ]

    def _effective_term_words(self, term, path_lower):
        """The term's words a candidate actually has to contain. A term with an
        "Artist - Title" (or a Spotify export's "Title - Artist, Artist, Artist")
        separator is matched per part: once at least one artist of a multi-artist
        part is found in the candidate's path, the remaining artists become
        optional. A peer's filename rarely lists every featured or remixing
        artist a playlist export does, and requiring all of them (with the
        default 80% threshold) made such tracks "Not Found" while a manual
        search plainly showed them. The title part (and at least one artist)
        stays required, so a different track by the same artist still fails."""

        segments = [segment for segment in self.TERM_SEGMENT_SPLIT_PATTERN.split(term) if segment.strip()]

        if len(segments) < 2:
            return self._term_words(term)

        path_tokens = self._text_tokens(path_lower)
        words = []

        for segment in segments:
            unit_words = [self._term_words(unit) for unit in self.EXTRA_ARTIST_SPLIT_PATTERN.split(segment)]
            unit_words = [unit for unit in unit_words if unit]
            matched_units = [
                unit for unit in unit_words
                if all(self._word_found(word, path_lower, path_tokens) for word in unit)
            ]

            if matched_units and len(matched_units) < len(unit_words):
                unit_words = matched_units

            for unit in unit_words:
                words.extend(unit)

        return words or self._term_words(term)

    def _send_search_text(self, item, raw_text):

        text = self._sanitize_text(raw_text)

        if not text:
            return

        # Deliberately no "-remix" exclusion here even when the term has no
        # "remix" in it: peers would then never send remixes back at all, and a
        # track that only exists as a remix (a request typed from memory, or a
        # Spotify title that already IS one) ended up Not Found although a
        # manual search showed pages of it. Remixes are instead kept as a
        # last-resort fallback locally (see _file_search_response)

        log.add_search(_('Searching for download list item "%s"'), text)

        core.send_message_to_network_thread(AddAllowedResponse(FileSearchResponse, item.token))
        core.send_message_to_server(FileSearch(item.token, text))

    def _dispatch_item(self, download_list, item):

        item.status = DownloadListItemStatus.SEARCHING
        item.searched_term = item.term
        item.variant_index = 0
        item.download_candidates = []
        item.near_misses = {}
        item.suggestions = []
        item.dispatch_time = time.time()

        item.token = self._next_token()
        self._token_map[item.token] = (download_list.name, item.term)

        if core.search is not None:
            # Borrow Search's response routing so its own handler doesn't discard
            # results for our (unrelated) token before we get to look at them
            core.search.searches[item.token] = item

        self._send_search_text(item, item.term)

        item.escalation_timer_id = events.schedule(
            delay=self.ESCALATION_DELAY,
            callback=lambda: self._escalate_item(download_list.name, item.term)
        )

        events.emit("update-download-list-item", download_list.name, item.term)

    def _get_term_variants(self, term):
        """Return a list of progressively broader versions of a search term,
        used to find matches when the exact term returns nothing (e.g.
        because of extra featured artists or remix tags peers don't have in
        their filenames)."""

        variants = [term]

        # Same term without accents: peers index their filenames verbatim, so a
        # term with diacritics finds nothing on a peer whose file has none (and
        # the folded text is what the local matching compares anyway)
        folded = self._fold_text(term)

        if folded != term.lower():
            variants.append(folded)

        # Drop bracketed/parenthetical content (e.g. "(Radio Edit)", "[Remix]")
        no_brackets = self.COLLAPSE_WHITESPACE_PATTERN.sub(
            " ", self.BRACKETED_CONTENT_PATTERN.sub(" ", term)).strip()

        if no_brackets and no_brackets.lower() != variants[-1].lower():
            variants.append(no_brackets)

        # Drop "feat./ft./featuring/with" clauses
        no_features = self.COLLAPSE_WHITESPACE_PATTERN.sub(
            " ", self.FEATURED_ARTIST_PATTERN.sub(" ", no_brackets)).strip()

        if no_features and no_features.lower() != variants[-1].lower():
            variants.append(no_features)

        # Drop additional artists joined by ",", "&", "x" or "vs" in an "Artist - Title" term
        if " - " in no_features:
            artist_part, _separator, title_part = no_features.partition(" - ")
            artist_part = self.EXTRA_ARTIST_SPLIT_PATTERN.split(artist_part, maxsplit=1)[0].strip()
            no_extra_artists = self.COLLAPSE_WHITESPACE_PATTERN.sub(
                " ", f"{artist_part} - {title_part}").strip()

            if no_extra_artists and no_extra_artists.lower() != variants[-1].lower():
                variants.append(no_extra_artists)

            # Last resort: search on just the artist name, then just the title. A
            # combined "artist title" query can come back with fewer/no results the
            # same way it sometimes does when searching manually — while a broader
            # single-part search often turns up plenty from peers whose tags/filenames
            # just don't line up neatly with a multi-word query. Which one actually
            # works varies by track (sometimes it's the artist alone, sometimes only
            # the title alone finds anything — there's no way to know in advance), so
            # both are tried. This is safe because matching still checks candidates
            # against every word of the *original* full term (see
            # _file_search_response), same as manually filtering broader results down
            # with "Include text" afterwards — it can only narrow things further,
            # never accept a result that doesn't actually belong to the original term.
            if artist_part and artist_part.lower() != variants[-1].lower():
                variants.append(artist_part)

            title_part = title_part.strip()

            if title_part and title_part.lower() not in (variant.lower() for variant in variants):
                variants.append(title_part)

        elif 3 <= len(self._sanitize_text(term).split()) <= 4:
            # A short, free-form term (typed rather than exported) has no part
            # to strip -- but a single misspelled word ("kelly clakson stronger")
            # makes peers return nothing at all. Leaving each word out in turn
            # finds the track by its remaining words; the local matching then
            # still checks every ORIGINAL word (tolerating one typo, see
            # _word_found), so a wrong track can't slip through
            words = self._sanitize_text(term).split()

            for index in range(len(words)):
                shorter = " ".join(words[:index] + words[index + 1:])

                if shorter.lower() not in (variant.lower() for variant in variants):
                    variants.append(shorter)

        return variants

    def _escalate_item(self, list_name, term):
        """Broaden the search for an item that hasn't found any candidates
        yet, by trying the next, less specific term variant."""

        download_list = self.lists.get(list_name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None or item.status != DownloadListItemStatus.SEARCHING:
            return

        item.escalation_timer_id = None

        if item.download_candidates:
            # Potential matches are already being collected, let that timer finalize things
            return

        variants = self._get_term_variants(item.term)
        item.variant_index += 1

        if item.variant_index >= len(variants):
            # Out of simplified variants to try, but that doesn't mean give up yet — a
            # term with nothing to broaden (e.g. a plain "Artist - Title") exhausts its
            # single variant on the very first escalation attempt, well before
            # SEARCH_TIMEOUT
            elapsed = time.time() - (item.dispatch_time or time.time())
            remaining = self.SEARCH_TIMEOUT - elapsed

            if remaining <= 0:
                item.status = DownloadListItemStatus.NOT_FOUND
                self._settle_suggestions(item)
                self._forget_search(item)

                events.emit("update-download-list-item", list_name, term)
                self._save()
                self._check_list_complete(list_name)
                return

            # A single search request's visibility on the network is time-limited —
            # peers that come online a bit later, or just take a while to respond,
            # won't be caught by passively waiting on the original request. Re-issue
            # the same (broadest) search text periodically instead, same as manually
            # searching again, until the remaining budget runs out
            self._send_search_text(item, variants[-1])

            item.escalation_timer_id = events.schedule(
                delay=min(self.ESCALATION_DELAY, remaining),
                callback=lambda: self._escalate_item(list_name, term))
            return

        next_text = variants[item.variant_index]
        item.searched_term = next_text

        log.add_search(
            _('No results yet for "%(term)s", broadening search to "%(text)s"'),
            {"term": term, "text": next_text}
        )

        self._send_search_text(item, next_text)

        item.escalation_timer_id = events.schedule(
            delay=self.ESCALATION_DELAY, callback=lambda: self._escalate_item(list_name, term))

    @staticmethod
    def _meets_quality_preference(quality, is_lossless, bitrate):

        if quality == "lossless":
            return is_lossless

        if quality == "high":
            return is_lossless or bitrate >= 320

        if quality == "good":
            return is_lossless or bitrate >= 192

        # "any"
        return True

    @staticmethod
    def _is_one_edit_away(word, other):
        """Whether two words differ by a single substituted, inserted or
        deleted character (a typo), e.g. "clakson" and "clarkson"."""

        if word == other:
            return True

        if abs(len(word) - len(other)) > 1:
            return False

        if len(word) == len(other):
            return sum(1 for char_a, char_b in zip(word, other) if char_a != char_b) == 1

        longer, shorter = (word, other) if len(word) > len(other) else (other, word)

        for index in range(len(longer)):
            if longer[:index] + longer[index + 1:] == shorter:
                return True

        return False

    @classmethod
    def _word_found(cls, word, text, text_tokens=None):
        """Whether a term word occurs in a candidate's (folded, lowercase)
        filename or path. Beyond the plain substring test, a longer word also
        counts when the file merely joins or splits it differently ("fourtet"
        vs "four tet") or differs from it by a single typo ("clakson" vs
        "clarkson") -- short words are exempt, since one wrong letter turns
        them into a different word entirely."""

        if word in text:
            return True

        if len(word) < cls.FUZZY_WORD_MIN_LENGTH:
            return False

        if word in text.replace(" ", ""):
            return True

        if text_tokens is None:
            text_tokens = cls._text_tokens(text)

        return any(
            len(token) >= cls.FUZZY_WORD_MIN_LENGTH - 1 and cls._is_one_edit_away(word, token)
            for token in text_tokens
        )

    @classmethod
    def _match_percentage(cls, term_words, filename_lower, path_lower):
        """Score how well the search term matches a candidate file. A word
        found in the filename itself counts in full; one found only in a
        parent folder counts for half, since many shares put the artist in
        the folder name and only the track title in the filename.

        Weighting the filename this way is what keeps a compilation/mix set
        whose FOLDER happens to be named after the search term (e.g. a "Carl
        Cox - Pure" radio show archive) from being treated as a 100% match
        for every individual, differently-titled track file inside it --
        those only ever match in the folder, so they top out at 50%, well
        below fuzzy_match_threshold's default of 70."""

        if not term_words:
            return 100.0

        filename_tokens = cls._text_tokens(filename_lower)
        path_tokens = cls._text_tokens(path_lower)
        score = 0.0

        for word in term_words:
            if cls._word_found(word, filename_lower, filename_tokens):
                score += 1.0
            elif cls._word_found(word, path_lower, path_tokens):
                score += 0.5

        return (score / len(term_words)) * 100

    def _purity_percentage(self, term_words, filename_lower):
        """A stricter, display-only match score (see download_match_percentage
        / the Verify Matches dialog) -- unlike _match_percentage (which only
        checks how many of the term's words were found, deliberately lenient
        so a good candidate isn't missed just for e.g. sitting in a
        differently-named folder), this also penalizes EXTRA words in the
        candidate's own filename beyond the term's, e.g. "(El Rancho Mix)"
        tacked onto "Carl Cox - Pure". A candidate can still be accepted and
        downloaded with a padded filename like that (see
        _matches_remix_requirement's docstring -- that's deliberate), but it
        shouldn't then look identical to a genuinely exact match: this is
        the Jaccard similarity between the term's words and the filename's
        (intersection over union), so missing OR extra words both pull it
        below 100%."""

        filename_stem = os.path.splitext(filename_lower)[0]
        filename_words = set(self._term_words(filename_stem))
        term_word_set = set(term_words)

        if not term_word_set and not filename_words:
            return 100.0

        if not term_word_set or not filename_words:
            return 0.0

        return (len(term_word_set & filename_words) / len(term_word_set | filename_words)) * 100

    @staticmethod
    def _matches_remix_requirement(term_words, filename_lower):
        """Whether a candidate's remix status agrees with the original search
        term's. If the term doesn't say "remix", a candidate that's actually
        some specific remix is a different version of the track and must not
        match -- and if the term does ask for a remix, the plain original
        mix must not match either. Checked against the filename specifically
        (not the full path), since a "Remixes" folder or similar shouldn't
        affect a track that isn't itself a remix. Deliberately narrow: only
        the literal word "remix" disqualifies a candidate -- a named/branded
        mix (e.g. "El Rancho Mix") is not treated as a different version,
        just a variant worth a lower match score (see _purity_percentage)."""

        term_has_remix = "remix" in term_words
        filename_has_remix = bool(re.search(r"\bremix\b", filename_lower))

        return term_has_remix == filename_has_remix

    @staticmethod
    def _parse_keywords(keywords_text):
        """"beatport, bp" -> ["beatport", "bp"]"""

        if not keywords_text:
            return []

        return [keyword.strip().lower() for keyword in keywords_text.split(",") if keyword.strip()]

    @classmethod
    def _matches_preferred_keywords(cls, keywords_text, path_lower):
        """Whether any preferred keyword appears as a whole word in the path, e.g. so
        "bp" matches a "BP Sep 2025" folder but not an unrelated "bpm128" filename."""

        keywords = cls._parse_keywords(keywords_text)

        if not keywords:
            return False

        return any(re.search(r"\b" + re.escape(keyword) + r"\b", path_lower) for keyword in keywords)

    def _file_search_response(self, msg):
        """Peer code 9."""

        if msg.token is None or msg.list is None:
            return

        entry = self._token_map.get(msg.token)

        if entry is None:
            return

        list_name, term = entry
        download_list = self.lists.get(list_name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None or item.status != DownloadListItemStatus.SEARCHING:
            return

        username = msg.username

        if core.network_filter.is_user_ignored(username):
            return

        ip_address, _port = msg.addr

        if core.network_filter.is_user_ip_ignored(username, ip_address):
            return

        full_term_words = self._term_words(item.term)
        term_wants_remix = "remix" in full_term_words
        # Only a structured "Title - Artist" / "Artist - Title" term (what a
        # Spotify export produces) names a specific version of a track. A
        # free-form term typed by hand is a keyword search: "remix" is just
        # another keyword if it's there, and nothing to vet against if it isn't
        vet_version = self.TERM_SEGMENT_SPLIT_PATTERN.search(item.term) is not None
        best_score = None
        best_candidate = None

        for fileinfo in msg.list:
            _code, virtual_path, size, _ext, attributes = fileinfo
            extension = os.path.splitext(virtual_path)[1].lower()

            if extension not in self.AUDIO_EXTENSIONS:
                continue

            if username + virtual_path in item.rejected_files:
                # Already downloaded once and failed the quality check
                continue

            path_lower = self._fold_text(virtual_path)
            filename_lower = path_lower.replace("\\", "/").rsplit("/", 1)[-1]
            term_words = self._effective_term_words(item.term, path_lower)
            match_percentage = self._match_percentage(term_words, filename_lower, path_lower)

            if match_percentage < download_list.effective_fuzzy_match_threshold:
                # Doesn't look enough like the original term, e.g. a search that was
                # broadened to find any results at all matched an unrelated track --
                # but if it's at all close, it may be what a mistyped term meant
                self._record_near_miss(item, match_percentage, virtual_path)
                continue

            version_matches = (
                not vet_version or self._matches_remix_requirement(full_term_words, filename_lower))

            if not version_matches and term_wants_remix:
                # The term asks for a remix and this is the plain track -- a
                # different version of the song, no matter how well its other
                # words otherwise match
                continue

            # The reverse case -- a remix although the term didn't ask for one --
            # is kept, but only as a fallback: version_matches leads the score
            # below, so ANY candidate of the right version outranks every remix.
            # It just no longer ends in Not Found when the track only exists as
            # that remix (a request typed from memory, or a Spotify title that
            # already is a remix without saying so)

            _h_quality, bitrate, _h_length, length = FileListMessage.parse_audio_quality_length(size, attributes)

            if length > self.MAX_REASONABLE_TRACK_LENGTH:
                # A continuous DJ mix/compilation, not the single track being searched for,
                # no matter how well its title otherwise matches -- see the constant above
                continue

            is_lossless = attributes.bit_depth is not None

            if not self._meets_quality_preference(download_list.effective_quality, is_lossless, bitrate):
                continue

            keyword_match = self._matches_preferred_keywords(download_list.effective_preferred_keywords, path_lower)

            # A lossless file's "bitrate" here is really sample_rate * bit_depth * channels
            # (e.g. ~1411 for 44.1kHz/16-bit), which dwarfs any real lossy bitrate (normally
            # <=320) and would otherwise decide every tie in favor of lossless regardless of
            # the preference below. Capping it keeps bitrate a genuine tiebreaker between
            # comparable candidates instead of an accidental format preference of its own
            capped_bitrate = min(bitrate, 320)

            score = (
                version_matches,
                round(match_percentage),
                bool(msg.freeulslots),
                keyword_match,
                is_lossless if download_list.effective_prefer_lossless else not is_lossless,
                capped_bitrate,
                length if download_list.effective_prefer_longer else 0,
                -msg.inqueue
            )

            if best_score is None or score > best_score:
                best_score = score
                best_candidate = (username, virtual_path, size, attributes)

        if best_candidate is None:
            return

        item.download_candidates.append((best_score, *best_candidate))

        if item.collect_timer_id is None:
            item.collect_timer_id = events.schedule(
                delay=self.COLLECTION_DELAY,
                callback=lambda: self._finalize_item(list_name, term)
            )

    def _finalize_item(self, list_name, term):
        """After the collection window for an item closes, download the best
        candidate found among all results received."""

        download_list = self.lists.get(list_name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None:
            return

        item.collect_timer_id = None
        candidates = item.download_candidates
        item.download_candidates = []

        if not candidates or item.status != DownloadListItemStatus.SEARCHING:
            return

        candidates.sort(key=itemgetter(0), reverse=True)

        # Best candidate from a peer that isn't busy (see _peer_is_busy); the
        # best one overall only when every source is
        chosen = next((candidate for candidate in candidates if not self._peer_is_busy(candidate[1])), candidates[0])
        _best_score, username, virtual_path, size, attributes = chosen

        if chosen is not candidates[0]:
            log.add_search(
                _('Taking "%(term)s" from %(user)s instead of busy peer %(busy_user)s'),
                {"term": term, "user": username, "busy_user": candidates[0][1]}
            )

        # Stop tracking the search itself; we're done with it now
        self._forget_search(item)

        core.downloads.enqueue_download(
            username, virtual_path, folder_path=download_list.effective_download_folder_path,
            size=size, file_attributes=attributes)

        item.status = DownloadListItemStatus.DOWNLOADING
        item.download_username = username
        item.download_virtual_path = virtual_path
        item.download_size = size
        item.download_attributes = attributes
        # Recomputed from the winning candidate's own filename, deliberately not just
        # best_score[0] (the lenient score that decided which candidate to accept) --
        # see _purity_percentage's docstring for why these are two different numbers
        filename_lower = virtual_path.lower().replace("\\", "/").rsplit("/", 1)[-1]
        item.download_match_percentage = round(self._purity_percentage(self._term_words(item.term), filename_lower))
        item.download_percent = 0
        item.download_start_time = time.time()
        item.stall_timer_id = events.schedule(
            delay=self.stall_timeout, callback=lambda: self._handle_stalled_download(list_name, term))

        transfer_key = username + virtual_path
        self._transfer_map[transfer_key] = (list_name, term)

        log.add_search(
            _('Downloading "%(file)s" from %(user)s for "%(term)s"'),
            {"file": virtual_path, "user": username, "term": term}
        )

        events.emit("update-download-list-item", list_name, term)
        self._save()

    @staticmethod
    def _transfer_percent(current_byte_offset, size):

        if not current_byte_offset or size <= 0:
            return 0

        if current_byte_offset >= size:
            return 100

        # Multiply first to avoid decimals
        return (100 * current_byte_offset) // size

    def _update_download(self, transfer, _update_parent):
        """Track progress and completion of an automatic download list transfer."""

        transfer_key = transfer.username + transfer.virtual_path
        entry = self._transfer_map.get(transfer_key)

        if entry is None:
            return

        list_name, term = entry
        download_list = self.lists.get(list_name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None:
            return

        if transfer.status != TransferStatus.FINISHED:
            percent = self._transfer_percent(transfer.current_byte_offset, transfer.size)

            if percent >= self.STALL_EXEMPT_PERCENT:
                # Close enough to done that whatever's left is finalization overhead
                # (moving out of the incomplete folder, hash-checking, etc.), not a
                # stall — and that can legitimately take a moment, during which speed
                # commonly reads 0 since nothing's actually being sent anymore. The
                # watchdog's own timer fires on its own schedule, independent of
                # progress updates, so an exact "== 100%" check isn't a wide enough
                # margin: it can still land in this window a hair before the last
                # byte is technically accounted for. Disarm it so it can't abort an
                # already-(near-)finished download out from under itself
                events.cancel_scheduled(item.stall_timer_id)
                item.stall_timer_id = None

            elif transfer.speed >= self.min_transfer_speed:
                # Healthy throughput observed just now: push the stall deadline back out.
                # Anything below the threshold (including exactly 0, e.g. still queued or
                # stuck) leaves the existing timer running toward its original deadline
                events.cancel_scheduled(item.stall_timer_id)
                item.stall_timer_id = events.schedule(
                    delay=self.stall_timeout, callback=lambda: self._handle_stalled_download(list_name, term))

            if percent == item.download_percent:
                return

            item.download_percent = percent
            events.emit("update-download-list-item", list_name, term)
            return

        del self._transfer_map[transfer_key]

        item.download_percent = 100
        self._forget_item(item)

        file_path = self._item_file_path(list_name, item)

        if (file_path is not None and self.quality_check_enabled
                and audioquality.REQUIRED_CUTOFF_HZ.get(download_list.effective_quality, 0) > 0):
            item.status = DownloadListItemStatus.QUALITY_CHECK
            events.emit("update-download-list-item", list_name, term)
            threading.Thread(
                target=self._run_quality_check, args=(list_name, term, file_path),
                name="DownloadListQualityCheck", daemon=True
            ).start()
            return

        self._complete_item(list_name, item)

    def _item_file_path(self, list_name, item):
        """Where an item's finished download is on disk, or None if it isn't there."""

        download_list = self.lists.get(list_name)

        if download_list is None or not item.download_username or not item.download_virtual_path:
            return None

        file_path, file_exists = core.downloads.get_complete_download_file_path(
            item.download_username, item.download_virtual_path, item.download_size,
            download_folder_path=download_list.effective_download_folder_path
        )
        return file_path if file_exists else None

    def _complete_item(self, list_name, item):

        item.status = DownloadListItemStatus.COMPLETED
        item.download_percent = 100

        events.emit("update-download-list-item", list_name, item.term)
        self._emit_item_finished(list_name, item)
        self._save()

        self._check_list_complete(list_name)

    def _run_quality_check(self, list_name, term, file_path):
        """Background thread: measure the file's spectral cutoff."""

        report = audioquality.analyze_file(file_path)
        events.invoke_main_thread(self._resolve_quality_check, list_name, term, file_path, report)

    def _resolve_quality_check(self, list_name, term, file_path, report):
        """Back on the main thread with the measured quality: keep the file,
        or set it aside and look for another source."""

        download_list = self.lists.get(list_name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None or item.status != DownloadListItemStatus.QUALITY_CHECK:
            return

        if report is None:
            # Couldn't be judged (unreadable, silent, too short): keep it
            self._complete_item(list_name, item)
            return

        item.quality_cutoff_hz = report.cutoff_hz

        if report.meets(download_list.effective_quality):
            log.add_search(
                _('Quality check passed for "%(term)s": %(result)s'),
                {"term": term, "result": report.describe()}
            )
            self._complete_item(list_name, item)
            return

        rejected_path = self._set_aside_rejected_file(download_list, file_path)
        item.rejected_files.add(item.download_username + item.download_virtual_path)
        item.num_quality_rejections += 1

        log.add(
            _('Quality check failed for "%(term)s" from %(user)s: %(result)s, moved to "%(path)s"'),
            {"term": term, "user": item.download_username, "result": report.describe(),
             "path": rejected_path or file_path}
        )

        if item.num_quality_rejections >= self.MAX_QUALITY_REJECTIONS:
            # Every source tried so far was a fake: give up rather than
            # collect more of them. The files are kept in the rejected folder
            item.status = DownloadListItemStatus.NOT_FOUND
            events.emit("update-download-list-item", list_name, term)
            self._save()
            self._check_list_complete(list_name)
            return

        rejected_files = item.rejected_files
        num_rejections = item.num_quality_rejections
        self._clear_item_progress(item)
        item.rejected_files = rejected_files
        item.num_quality_rejections = num_rejections

        if download_list.effective_auto_download:
            self._queue.appendleft((list_name, term))
            self._kick_queue()

        events.emit("update-download-list-item", list_name, term)
        self._save()

    def _set_aside_rejected_file(self, download_list, file_path):
        """Move a failed download out of the way (never delete: the user may
        still want it if nothing better turns up). Returns the new path."""

        folder_path = os.path.join(
            download_list.effective_download_folder_path or os.path.dirname(file_path),
            self.REJECTED_QUALITY_FOLDER_NAME)
        target_path = os.path.join(folder_path, os.path.basename(file_path))

        try:
            os.makedirs(encode_path(folder_path), exist_ok=True)
            os.replace(encode_path(file_path), encode_path(target_path))

        except OSError as error:
            log.add(_("Cannot move rejected download %(path)s: %(error)s"), {"path": file_path, "error": error})
            return None

        return target_path

    def _check_list_complete(self, list_name):

        download_list = self.lists.get(list_name)

        if download_list is not None and download_list.is_complete:
            events.emit("download-list-completed", list_name)

    def _handle_stalled_download(self, list_name, term):
        """A download whose transfer speed stayed below MIN_TRANSFER_SPEED for
        STALL_TIMEOUT seconds straight: give up on this candidate, cancel its
        transfer, and search again so a different source gets a chance."""

        download_list = self.lists.get(list_name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None or item.status != DownloadListItemStatus.DOWNLOADING:
            # Already moved on (completed, reset, removed) since this timer was scheduled
            return

        item.stall_timer_id = None

        transfer_key = item.download_username + item.download_virtual_path
        transfer = core.downloads.transfers.get(transfer_key)

        if transfer is not None and (
                transfer.status == TransferStatus.FINISHED
                or self._transfer_percent(transfer.current_byte_offset, transfer.size)
                >= self.STALL_EXEMPT_PERCENT):
            # Second safety net against the same race the STALL_EXEMPT_PERCENT check in
            # _update_download guards against: this timer runs on its own independent
            # schedule, so it could still fire in the exempt window (or even after
            # completion) if it was already in flight. Do nothing and let the ordinary
            # completion handling (or an already-delivered one) stand
            return

        if (transfer is not None
                and transfer.status in (TransferStatus.QUEUED, TransferStatus.GETTING_STATUS)
                and time.time() - (item.download_start_time or time.time()) < self.QUEUED_WAIT_LIMIT):
            # The peer has accepted the request into its upload queue (also how a
            # "Too many files" limit is presented, resuming by itself later): nothing
            # is being sent yet, but that's waiting a turn, not a stall. Cancelling
            # here used to re-find the same lone source and queue behind everyone
            # again, over and over -- until a re-search happened to catch a quiet
            # moment and marked a perfectly available track Not Found
            item.stall_timer_id = events.schedule(
                delay=self.stall_timeout, callback=lambda: self._handle_stalled_download(list_name, term))
            return

        log.add_search(
            _('Download stalled for "%(term)s" (no meaningful progress for %(seconds)s seconds), '
              "searching for a different source"),
            {"term": term, "seconds": self.stall_timeout}
        )

        if transfer is not None:
            core.downloads.abort_downloads([transfer], status=TransferStatus.CANCELLED)
            core.downloads.clear_downloads([transfer])

        self._forget_item(item)

        item.status = DownloadListItemStatus.PENDING
        item.download_username = None
        item.download_virtual_path = None
        item.download_size = 0
        item.download_attributes = None
        item.download_match_percentage = None
        item.download_percent = 0

        if download_list.effective_auto_download:
            self._queue.appendleft((list_name, term))
            self._kick_queue()

        events.emit("update-download-list-item", list_name, term)
        self._save()
