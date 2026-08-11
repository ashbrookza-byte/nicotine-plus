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
import time

from collections import deque
from operator import itemgetter

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
from pynicotine.utils import write_file_and_backup


class DownloadListItemStatus:
    PENDING = "pending"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    NOT_FOUND = "not_found"


class DownloadListItem:
    __slots__ = (
        "term", "list_name", "time_added", "status", "searched_term", "token",
        "download_username", "download_virtual_path", "download_size", "download_attributes",
        "download_candidates", "variant_index", "collect_timer_id", "escalation_timer_id"
    )

    def __init__(self, term, list_name, time_added=None, status=DownloadListItemStatus.PENDING,
                 searched_term=None, download_username=None, download_virtual_path=None,
                 download_size=0, download_attributes=None):

        self.term = term
        self.list_name = list_name
        self.time_added = time_added if time_added is not None else int(time.time())
        self.searched_term = searched_term

        if status in (DownloadListItemStatus.SEARCHING, DownloadListItemStatus.DOWNLOADING):
            # In-flight state can't resume across restarts (its bookkeeping isn't
            # persisted), so pick up as pending instead
            status = DownloadListItemStatus.PENDING

        self.status = status
        self.download_username = download_username
        self.download_virtual_path = download_virtual_path
        self.download_size = download_size
        self.download_attributes = download_attributes

        # Transient, session-only state
        self.token = None
        self.download_candidates = []
        self.variant_index = 0
        self.collect_timer_id = None
        self.escalation_timer_id = None

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
            return ""

        return self.download_virtual_path.replace("\\", "/").rsplit("/", 1)[-1]

    def as_dict(self):

        attributes = self.download_attributes

        return {
            "term": self.term,
            "time_added": self.time_added,
            "status": self.status,
            "searched_term": self.searched_term,
            "download_username": self.download_username,
            "download_virtual_path": self.download_virtual_path,
            "download_size": self.download_size,
            "download_bitrate": attributes.bitrate if attributes else None,
            "download_length": attributes.length if attributes else None,
            "download_vbr": attributes.vbr if attributes else None,
            "download_sample_rate": attributes.sample_rate if attributes else None,
            "download_bit_depth": attributes.bit_depth if attributes else None
        }


class DownloadList:
    __slots__ = (
        "name", "download_folder_path", "quality", "prefer_longer", "fuzzy_match_threshold",
        "auto_download", "time_added", "items"
    )

    def __init__(self, name, download_folder_path=None, quality="good", prefer_longer=True,
                 fuzzy_match_threshold=70, auto_download=True, time_added=None, items=None):

        self.name = name
        self.download_folder_path = download_folder_path or None
        self.quality = quality
        self.prefer_longer = prefer_longer
        self.fuzzy_match_threshold = fuzzy_match_threshold
        self.auto_download = auto_download
        self.time_added = time_added if time_added is not None else int(time.time())
        self.items = items if items is not None else {}

    @property
    def num_completed(self):
        return sum(1 for item in self.items.values() if item.status == DownloadListItemStatus.COMPLETED)

    @property
    def num_not_found(self):
        return sum(1 for item in self.items.values() if item.status == DownloadListItemStatus.NOT_FOUND)

    @property
    def num_pending(self):
        return sum(
            1 for item in self.items.values()
            if item.status in (
                DownloadListItemStatus.PENDING, DownloadListItemStatus.SEARCHING,
                DownloadListItemStatus.DOWNLOADING
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
            "fuzzy_match_threshold": self.fuzzy_match_threshold,
            "auto_download": self.auto_download,
            "time_added": self.time_added,
            "items": [item.as_dict() for item in self.items.values()]
        }


class DownloadLists:
    __slots__ = (
        "lists", "file_path", "_token", "_queue", "_dispatch_timer_id",
        "_token_map", "_transfer_map", "_allow_saving", "_watch_timer_id", "_watch_snapshots"
    )

    FILE_BASENAME = "download_lists.json"

    # How often the watch folder is polled for new song list files
    WATCH_INTERVAL = 30

    # Song list files dropped in the watch folder are moved here once imported
    WATCH_IMPORTED_FOLDER_NAME = "imported"

    WATCH_FILE_EXTENSIONS = (".txt", ".csv")

    # Extensions considered when picking a file to automatically download
    AUDIO_EXTENSIONS = {
        ".mp3", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma", ".ape", ".aiff", ".alac", ".mp4"
    }

    # Pacing between dispatching each queued item's initial search, so a big list doesn't flood the server
    DISPATCH_DELAY = 8

    # How long to keep collecting results for an item before picking the best match
    COLLECTION_DELAY = 15

    # How long to wait for results before broadening an item's search term
    ESCALATION_DELAY = 10

    QUALITY_LABELS = {
        "any": _("Any"),
        "good": _("Good (192 kbps+)"),
        "high": _("High (320 kbps+)"),
        "lossless": _("Lossless only (FLAC/WAV)")
    }

    STATUS_LABELS = {
        DownloadListItemStatus.PENDING: _("Pending"),
        DownloadListItemStatus.SEARCHING: _("Searching…"),
        DownloadListItemStatus.DOWNLOADING: _("Downloading…"),
        DownloadListItemStatus.COMPLETED: _("Completed"),
        DownloadListItemStatus.NOT_FOUND: _("Not Found")
    }

    # Progressive search term simplification, used to broaden an item's search when no
    # results come back for the exact term (e.g. extra featured artists, remix tags)
    BRACKETED_CONTENT_PATTERN = re.compile(r"[(\[][^)\]]*[)\]]")
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

        # watch folder file path -> (size, modification time) seen on the previous scan
        self._watch_snapshots = {}

        for event_name, callback in (
            ("file-search-response", self._file_search_response),
            ("quit", self._quit),
            ("start", self._start),
            ("update-download", self._update_download)
        ):
            events.connect(event_name, callback)

    def _start(self):

        self._load()
        self._allow_saving = True

        # Save download lists every 3 minutes
        events.schedule(delay=180, callback=self._save, repeat=True)

        # Poll the watch folder for song list files exported by other applications
        self._watch_timer_id = events.schedule(
            delay=self.WATCH_INTERVAL, callback=self._scan_watch_folder, repeat=True)

    def _quit(self):

        for download_list in self.lists.values():
            for item in download_list.items.values():
                events.cancel_scheduled(item.collect_timer_id)
                events.cancel_scheduled(item.escalation_timer_id)

        events.cancel_scheduled(self._dispatch_timer_id)
        events.cancel_scheduled(self._watch_timer_id)

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
                    download_username=item_data.get("download_username"),
                    download_virtual_path=item_data.get("download_virtual_path"),
                    download_size=item_data.get("download_size", 0),
                    download_attributes=attributes
                )

            self.lists[name] = DownloadList(
                name=name,
                download_folder_path=list_data.get("download_folder_path"),
                quality=list_data.get("quality", "good"),
                prefer_longer=list_data.get("prefer_longer", True),
                fuzzy_match_threshold=list_data.get("fuzzy_match_threshold", 70),
                auto_download=list_data.get("auto_download", True),
                time_added=list_data.get("time_added"),
                items=items
            )

        # Re-queue anything left pending from a previous session
        for download_list in self.lists.values():
            if not download_list.auto_download:
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

    def add_list(self, name, download_folder_path=None, quality="good", prefer_longer=True,
                 fuzzy_match_threshold=70, auto_download=True):

        name = name.strip()

        if not name or name in self.lists:
            return None

        self.lists[name] = download_list = DownloadList(
            name=name, download_folder_path=download_folder_path, quality=quality,
            prefer_longer=prefer_longer, fuzzy_match_threshold=fuzzy_match_threshold,
            auto_download=auto_download
        )

        events.emit("add-download-list", name)
        self._save()

        return download_list

    def update_list_settings(self, name, download_folder_path=None, quality=None, prefer_longer=None,
                             fuzzy_match_threshold=None, auto_download=None):

        download_list = self.lists.get(name)

        if download_list is None:
            return

        was_auto_download = download_list.auto_download

        if download_folder_path is not None:
            download_list.download_folder_path = download_folder_path or None

        if quality is not None:
            download_list.quality = quality

        if prefer_longer is not None:
            download_list.prefer_longer = prefer_longer

        if fuzzy_match_threshold is not None:
            download_list.fuzzy_match_threshold = fuzzy_match_threshold

        if auto_download is not None:
            download_list.auto_download = auto_download

        if download_list.auto_download and not was_auto_download:
            # Resuming a paused list: re-queue anything still pending
            for item in download_list.items.values():
                if item.status == DownloadListItemStatus.PENDING:
                    self._queue.append((name, item.term))

            self._kick_queue()

        events.emit("update-download-list", name)
        self._save()

    def rename_list(self, old_name, new_name):

        new_name = new_name.strip()

        if not new_name or old_name not in self.lists or new_name in self.lists:
            return False

        download_list = self.lists.pop(old_name)
        download_list.name = new_name

        for item in download_list.items.values():
            item.list_name = new_name

        self.lists[new_name] = download_list

        self._queue = deque(
            (new_name if list_name == old_name else list_name, term) for list_name, term in self._queue)

        for token, (list_name, term) in list(self._token_map.items()):
            if list_name == old_name:
                self._token_map[token] = (new_name, term)

        for key, (list_name, term) in list(self._transfer_map.items()):
            if list_name == old_name:
                self._transfer_map[key] = (new_name, term)

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

        added_any = False

        for term in terms:
            term = term.strip()

            if not term or term in download_list.items:
                continue

            download_list.items[term] = DownloadListItem(term=term, list_name=name)
            added_any = True

            if download_list.auto_download:
                self._queue.append((name, term))

        if not added_any:
            return

        events.emit("update-download-list", name)
        self._save()

        if download_list.auto_download:
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

    def reset_list_item(self, name, term):
        """Forget an item's search/download progress so it can be retried."""

        download_list = self.lists.get(name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None:
            return

        self._forget_item(item)

        item.status = DownloadListItemStatus.PENDING
        item.searched_term = None
        item.download_username = None
        item.download_virtual_path = None
        item.download_size = 0
        item.download_attributes = None

        if download_list.auto_download:
            self._queue.append((name, term))
            self._kick_queue()

        events.emit("update-download-list-item", name, term)
        self._save()

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

        transfer_key = next(
            (key for key, value in self._transfer_map.items() if value == (item.list_name, item.term)), None)

        if transfer_key is not None:
            del self._transfer_map[transfer_key]

    def _kick_queue(self):

        if self._dispatch_timer_id is not None:
            return

        self._pump_queue()

    def _pump_queue(self):

        self._dispatch_timer_id = None

        if core.users.login_status == UserStatus.OFFLINE:
            self._dispatch_timer_id = events.schedule(delay=self.DISPATCH_DELAY, callback=self._pump_queue)
            return

        while self._queue:
            name, term = self._queue.popleft()
            download_list = self.lists.get(name)
            item = download_list.items.get(term) if download_list is not None else None

            if (download_list is None or item is None or not download_list.auto_download
                    or item.status != DownloadListItemStatus.PENDING):
                continue

            self._dispatch_item(download_list, item)
            break

        if self._queue:
            self._dispatch_timer_id = events.schedule(delay=self.DISPATCH_DELAY, callback=self._pump_queue)

    def _sanitize_text(self, text):
        text = text.translate(self.REMOVED_SEARCH_CHARACTERS)
        return self.COLLAPSE_WHITESPACE_PATTERN.sub(" ", text).strip()

    def _term_words(self, term):
        return [word for word in self._sanitize_text(term).lower().split() if word]

    def _send_search_text(self, item, raw_text):

        text = self._sanitize_text(raw_text)

        if not text:
            return

        log.add_search(_('Searching for download list item "%s"'), text)

        core.send_message_to_network_thread(AddAllowedResponse(FileSearchResponse, item.token))
        core.send_message_to_server(FileSearch(item.token, text))

    def _dispatch_item(self, download_list, item):

        item.status = DownloadListItemStatus.SEARCHING
        item.searched_term = item.term
        item.variant_index = 0
        item.download_candidates = []

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
            # Exhausted all simplified variants without any acceptable matches
            item.status = DownloadListItemStatus.NOT_FOUND
            self._forget_search(item)

            events.emit("update-download-list-item", list_name, term)
            self._save()
            self._check_list_complete(list_name)
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
    def _match_percentage(term_words, path_lower):

        if not term_words:
            return 100.0

        num_matched = sum(1 for word in term_words if word in path_lower)
        return (num_matched / len(term_words)) * 100

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

        term_words = self._term_words(item.term)
        best_score = None
        best_candidate = None

        for fileinfo in msg.list:
            _code, virtual_path, size, _ext, attributes = fileinfo
            extension = os.path.splitext(virtual_path)[1].lower()

            if extension not in self.AUDIO_EXTENSIONS:
                continue

            path_lower = virtual_path.lower()
            match_percentage = self._match_percentage(term_words, path_lower)

            if match_percentage < download_list.fuzzy_match_threshold:
                # Doesn't look enough like the original term, e.g. a search that was
                # broadened to find any results at all matched an unrelated track
                continue

            _h_quality, bitrate, _h_length, length = FileListMessage.parse_audio_quality_length(size, attributes)
            is_lossless = attributes.bit_depth is not None

            if not self._meets_quality_preference(download_list.quality, is_lossless, bitrate):
                continue

            score = (
                round(match_percentage),
                bool(msg.freeulslots),
                is_lossless,
                bitrate,
                length if download_list.prefer_longer else 0,
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
        _score, username, virtual_path, size, attributes = candidates[0]

        # Stop tracking the search itself; we're done with it now
        self._forget_search(item)

        core.downloads.enqueue_download(
            username, virtual_path, folder_path=download_list.download_folder_path,
            size=size, file_attributes=attributes)

        item.status = DownloadListItemStatus.DOWNLOADING
        item.download_username = username
        item.download_virtual_path = virtual_path
        item.download_size = size
        item.download_attributes = attributes

        transfer_key = username + virtual_path
        self._transfer_map[transfer_key] = (list_name, term)

        log.add_search(
            _('Downloading "%(file)s" from %(user)s for "%(term)s"'),
            {"file": virtual_path, "user": username, "term": term}
        )

        events.emit("update-download-list-item", list_name, term)
        self._save()

    def _update_download(self, transfer, _update_parent):
        """Track completion of an automatic download list transfer."""

        transfer_key = transfer.username + transfer.virtual_path
        entry = self._transfer_map.get(transfer_key)

        if entry is None:
            return

        if transfer.status != TransferStatus.FINISHED:
            return

        del self._transfer_map[transfer_key]
        list_name, term = entry
        download_list = self.lists.get(list_name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None:
            return

        item.status = DownloadListItemStatus.COMPLETED
        self._forget_item(item)

        events.emit("update-download-list-item", list_name, term)
        self._save()

        self._check_list_complete(list_name)

    def _check_list_complete(self, list_name):

        download_list = self.lists.get(list_name)

        if download_list is not None and download_list.is_complete:
            events.emit("download-list-completed", list_name)
