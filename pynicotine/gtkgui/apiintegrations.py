# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""API Integrations tab: one place for every link between Nicotine+ and an
outside service -- currently the Spotify playlist watcher (spotifywatch.py)
and Lexicon DJ sync (lexiconsync.py).

Everything applies immediately: switches save on toggle, text fields save
when you press Enter (or when Test Connection / Sync Now is clicked, which
read the current field values first). There is no Save button."""

from gi.repository import Gtk

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.gtkgui.application import GTK_API_VERSION
from pynicotine.gtkgui.widgets.dialogs import OptionDialog
from pynicotine.gtkgui.widgets.theme import add_css_class
from pynicotine.gtkgui.widgets.treeview import TreeView
from pynicotine.spotifywatch import SPOTIFY_SCRAPER_AVAILABLE


class ApiIntegrations:

    def __init__(self, window):

        self.window = window
        self.page = window.apiintegrations_page
        self.page.id = "apiintegrations"
        self.toolbar = window.apiintegrations_toolbar
        self.toolbar_start_content = window.apiintegrations_title
        self.toolbar_end_content = window.apiintegrations_end

        # Toolbar: manual sync trigger
        self.sync_now_button = Gtk.Button(label=_("Sync with Lexicon _Now"), use_underline=True, visible=True)
        self.sync_now_button.set_tooltip_text(
            _("Re-checks every list's smartlist and imports any finished downloads still waiting, "
              "without waiting for the automatic once-a-minute retry."))
        self.sync_now_button.connect("clicked", self.on_sync_now)
        self.toolbar_default_widget = self.sync_now_button

        if GTK_API_VERSION >= 4:
            self.toolbar_end_content.append(self.sync_now_button)  # pylint: disable=no-member
        else:
            self.toolbar_end_content.add(self.sync_now_button)     # pylint: disable=no-member

        # Content
        self.primary_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, width_request=460, halign=Gtk.Align.CENTER,
            margin_top=18, margin_bottom=18, margin_start=24, margin_end=24, spacing=18, visible=True
        )
        scrolled_window = Gtk.ScrolledWindow(
            child=self.primary_container, hexpand=True, vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC, visible=True
        )
        self.container = scrolled_window

        if GTK_API_VERSION >= 4:
            window.apiintegrations_content.append(self.container)  # pylint: disable=no-member
        else:
            window.apiintegrations_content.add(self.container)     # pylint: disable=no-member

        # Like Wishlists, this page has no empty-state placeholder: its
        # content is the page
        window.apiintegrations_content.set_visible(True)

        self._add_lexicon_section()
        self._append(Gtk.Separator(visible=True))
        self._add_spotify_section()

        events.connect("add-download-list", self._on_lists_changed)
        events.connect("remove-download-list", self._on_lists_changed)
        events.connect("lexicon-unreachable", self.on_lexicon_unreachable)
        events.connect("lexicon-import-progress", self.on_lexicon_import_progress)

    # Layout helpers #

    def _append(self, widget):
        if GTK_API_VERSION >= 4:
            self.primary_container.append(widget)  # pylint: disable=no-member
        else:
            self.primary_container.add(widget)      # pylint: disable=no-member

    def _heading(self, label_text):
        heading = Gtk.Label(label=label_text, wrap=True, xalign=0, visible=True)
        add_css_class(heading, "heading")
        self._append(heading)

    def _labeled_row(self, label_text, widget, tooltip_text=None):

        row = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER, visible=True)
        label = Gtk.Label(
            label=label_text, hexpand=True, wrap=True, xalign=0, visible=True,
            mnemonic_widget=widget, tooltip_text=tooltip_text or "")

        if GTK_API_VERSION >= 4:
            row.append(label)   # pylint: disable=no-member
            row.append(widget)  # pylint: disable=no-member
        else:
            row.add(label)      # pylint: disable=no-member
            row.add(widget)     # pylint: disable=no-member

        self._append(row)
        return row

    def _switch(self, active, callback):
        switch = Gtk.Switch(active=active, valign=Gtk.Align.CENTER, visible=True)
        switch.connect("notify::active", callback)
        return switch

    # Lexicon section #

    def _add_lexicon_section(self):

        self._heading(_("Lexicon DJ"))

        hint = Gtk.Label(
            label=_("Mirrors every download list as a smartlist in Lexicon and adds finished downloads "
                    "to its library. Needs the Local API turned on in Lexicon itself, under "
                    "Settings → Integrations. Lexicon doesn't have to be running: anything still to "
                    "sync is retried once a minute until it is."),
            wrap=True, xalign=0, visible=True)
        add_css_class(hint, "dim-label")
        self._append(hint)

        # Connection status row
        status_row = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER, visible=True)
        self.lexicon_status_label = Gtk.Label(
            label=_("Connection not checked yet"), hexpand=True, wrap=True, xalign=0, visible=True)

        test_button = Gtk.Button(label=_("_Test Connection"), use_underline=True, visible=True)
        test_button.connect("clicked", self.on_test_connection)

        # Same action as the title-bar button, where it's easy to miss
        inline_sync_button = Gtk.Button(label=_("_Sync Now"), use_underline=True, visible=True)
        inline_sync_button.set_tooltip_text(self.sync_now_button.get_tooltip_text())
        inline_sync_button.connect("clicked", self.on_sync_now)

        if GTK_API_VERSION >= 4:
            status_row.append(self.lexicon_status_label)  # pylint: disable=no-member
            status_row.append(test_button)                # pylint: disable=no-member
            status_row.append(inline_sync_button)         # pylint: disable=no-member
        else:
            status_row.add(self.lexicon_status_label)     # pylint: disable=no-member
            status_row.add(test_button)                   # pylint: disable=no-member
            status_row.add(inline_sync_button)            # pylint: disable=no-member

        self._append(status_row)

        # Import-queue progress, only visible while a pass is running
        self.lexicon_progress_bar = Gtk.ProgressBar(
            hexpand=True, show_text=True, visible=False, valign=Gtk.Align.CENTER)
        self._append(self.lexicon_progress_bar)

        lexicon = config.sections["lexicon"]

        self.lexicon_enabled_switch = self._switch(lexicon["sync_enabled"], self.on_lexicon_setting_changed)
        self._labeled_row(
            _("Mirror lists as smartlists in Lexicon"), self.lexicon_enabled_switch,
            tooltip_text=_("Creates a smartlist in Lexicon for every download list, inside the playlist "
                            'folder named below, with a "file location contains this list\'s download '
                            "folder\" rule. Removing a list here never deletes its smartlist."))

        self.lexicon_auto_import_switch = self._switch(lexicon["auto_import"], self.on_lexicon_setting_changed)
        self._labeled_row(
            _("Add each finished download to the Lexicon library"), self.lexicon_auto_import_switch,
            tooltip_text=_("Imports every completed song into Lexicon as soon as it lands and adds it "
                            "to its list's playlist, with no manual import step in Lexicon."))

        self.lexicon_library_first_switch = self._switch(
            lexicon["library_first"], self.on_lexicon_setting_changed)
        self._labeled_row(
            _("Use songs already in the Lexicon library instead of downloading"),
            self.lexicon_library_first_switch,
            tooltip_text=_("A song newly added to a list is looked up in your Lexicon library before "
                            "any Soulseek search. If any version of it is already there, that track "
                            "goes straight into the list's Lexicon playlist and the item is marked "
                            '"In Library" — nothing is re-downloaded. While Lexicon is unreachable, '
                            "new songs wait as \"Checking Library…\" and you'll be asked whether to "
                            "keep waiting or download without the check."))

        self.lexicon_dedupe_switch = self._switch(lexicon["dedupe_enabled"], self.on_lexicon_setting_changed)
        self._labeled_row(
            _("Replace duplicates with the preferred version"), self.lexicon_dedupe_switch,
            tooltip_text=_("After importing a song, checks the Lexicon library for another version of "
                            "it (same artist and title, ignoring \"(Extended Mix)\"-style qualifiers). "
                            "Only the preferred version is kept; the other is removed from the Lexicon "
                            "library only — never deleted from disk — after its playlist placements "
                            "are moved over and its rating/tags copied across. Cue points are not "
                            "copied, since they wouldn't line up between different-length versions."))

        self.lexicon_prefer_longer_switch = self._switch(
            lexicon["dedupe_prefer_longer"], self.on_lexicon_setting_changed)
        self._labeled_row(
            _("Prefer the longer (Extended) version"), self.lexicon_prefer_longer_switch,
            tooltip_text=_("A version that's meaningfully longer (30+ seconds) wins the duplicate "
                            "check, even against a higher-quality shorter one — an extended mix beats "
                            "a lossless radio edit."))

        self.lexicon_prefer_lossless_switch = self._switch(
            lexicon["dedupe_prefer_lossless"], self.on_lexicon_setting_changed)
        self._labeled_row(
            _("Prefer lossless / higher bitrate"), self.lexicon_prefer_lossless_switch,
            tooltip_text=_("Between versions of similar length, a lossless file (FLAC/WAV/AIFF) beats "
                            "a lossy one, and a higher bitrate beats a lower one — so a FLAC replaces "
                            "an existing MP3 of the same song."))

        folder_label = Gtk.Label(
            label=_("Playlist folder in Lexicon (press Enter to apply):"), wrap=True, xalign=0, visible=True)
        self._append(folder_label)

        self.lexicon_folder_entry = Gtk.Entry(
            hexpand=True, text=lexicon["parent_folder"], visible=True,
            tooltip_text=_("All mirrored smartlists are kept inside this playlist folder in Lexicon, "
                            "created automatically if it doesn't exist yet."))
        self.lexicon_folder_entry.connect("activate", self.on_lexicon_setting_changed)
        folder_label.set_mnemonic_widget(self.lexicon_folder_entry)
        self._append(self.lexicon_folder_entry)

        url_label = Gtk.Label(
            label=_("Lexicon Local API address (press Enter to apply):"), wrap=True, xalign=0, visible=True)
        self._append(url_label)

        self.lexicon_url_entry = Gtk.Entry(
            hexpand=True, text=lexicon["api_url"], visible=True,
            tooltip_text=_("Leave as http://localhost:48624 unless Lexicon says otherwise."))
        self.lexicon_url_entry.connect("activate", self.on_lexicon_setting_changed)
        url_label.set_mnemonic_widget(self.lexicon_url_entry)
        self._append(self.lexicon_url_entry)

    def _apply_lexicon_settings(self):

        if core.lexicon_sync is None:
            return

        core.lexicon_sync.update_settings(
            sync_enabled=self.lexicon_enabled_switch.get_active(),
            api_url=self.lexicon_url_entry.get_text().strip() or "http://localhost:48624",
            parent_folder=self.lexicon_folder_entry.get_text().strip() or "nicotine",
            auto_import=self.lexicon_auto_import_switch.get_active(),
            library_first=self.lexicon_library_first_switch.get_active(),
            dedupe_enabled=self.lexicon_dedupe_switch.get_active(),
            dedupe_prefer_longer=self.lexicon_prefer_longer_switch.get_active(),
            dedupe_prefer_lossless=self.lexicon_prefer_lossless_switch.get_active()
        )

    def on_lexicon_setting_changed(self, *_args):
        self._apply_lexicon_settings()

    def _set_lexicon_status(self, reachable, detail):

        if reachable:
            self.lexicon_status_label.set_label(_("Connected to Lexicon at %s") % detail)
        else:
            self.lexicon_status_label.set_label(
                _("Lexicon is not reachable — is it running, with the Local API enabled under "
                  "Settings → Integrations? (%s)") % detail)

    def on_test_connection(self, *_args):

        if core.lexicon_sync is None:
            return

        self._apply_lexicon_settings()
        self.lexicon_status_label.set_label(_("Checking connection…"))
        core.lexicon_sync.check_connection(self._set_lexicon_status)

    def on_sync_now(self, *_args):

        if core.lexicon_sync is None:
            return

        self._apply_lexicon_settings()
        core.lexicon_sync.sync_now()
        self.on_test_connection()

    # Spotify section #

    def _add_spotify_section(self):

        self._heading(_("Spotify playlist watcher"))

        hint = Gtk.Label(
            label=_("Each watched playlist keeps a download list of the same name filled with its "
                    "tracks as they're added on Spotify. Works with any public playlist — no Spotify "
                    "account or login needed."),
            wrap=True, xalign=0, visible=True)
        add_css_class(hint, "dim-label")
        self._append(hint)

        if not SPOTIFY_SCRAPER_AVAILABLE:
            unavailable_hint = Gtk.Label(
                label=_("The optional \"spotifyscraper\" package isn't installed -- install it with "
                        "\"pip install spotifyscraper\" to use this."),
                wrap=True, xalign=0, visible=True)
            add_css_class(unavailable_hint, "dim-label")
            self._append(unavailable_hint)

        spotify = config.sections["spotify"]

        self.spotify_ignore_radio_edit_switch = self._switch(
            spotify["watch_ignore_radio_edit"], self.on_spotify_ignore_radio_edit)
        self._labeled_row(
            _('Ignore "Radio Edit" — prefer the Extended/Original version'),
            self.spotify_ignore_radio_edit_switch,
            tooltip_text=_('Strips a "(Radio Edit)" tag from a track\'s title before searching for it, '
                            "so the longer Extended/Original version is found instead of specifically "
                            "requiring the shortened radio one."))

        playlists_label = Gtk.Label(label=_("Watched playlists:"), wrap=True, xalign=0, visible=True)
        self._append(playlists_label)

        playlists_container = Gtk.ScrolledWindow(
            hexpand=True, min_content_height=140, max_content_height=200,
            hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC, visible=True)
        self._append(playlists_container)

        self.spotify_playlists_view = TreeView(
            self.window, parent=playlists_container,
            columns={
                "playlist_id": {
                    "iterator_key": True
                },
                "name": {
                    "column_type": "text",
                    "title": _("Playlist"),
                    "expand_column": True
                }
            }
        )
        playlists_label.set_mnemonic_widget(self.spotify_playlists_view.widget)
        self._populate_spotify_playlists()

        buttons_row = Gtk.Box(spacing=6, visible=True)

        add_playlist_button = Gtk.Button(label=_("_Add Playlist to Watch…"), use_underline=True, visible=True)
        add_playlist_button.connect("clicked", self.on_add_spotify_playlist)

        remove_playlist_button = Gtk.Button(label=_("_Remove"), use_underline=True, visible=True)
        remove_playlist_button.connect("clicked", self.on_remove_spotify_playlist)

        if GTK_API_VERSION >= 4:
            buttons_row.append(add_playlist_button)     # pylint: disable=no-member
            buttons_row.append(remove_playlist_button)  # pylint: disable=no-member
        else:
            buttons_row.add(add_playlist_button)        # pylint: disable=no-member
            buttons_row.add(remove_playlist_button)     # pylint: disable=no-member

        self._append(buttons_row)

    def _populate_spotify_playlists(self):

        if core.spotify_watch is None:
            return

        self.spotify_playlists_view.freeze()
        self.spotify_playlists_view.clear()

        for playlist in core.spotify_watch.get_watched_playlists():
            self.spotify_playlists_view.add_row(
                [playlist["playlist_id"], playlist["list_name"]], select_row=False)

        self.spotify_playlists_view.unfreeze()

    def on_spotify_ignore_radio_edit(self, *_args):

        if core.spotify_watch is not None:
            core.spotify_watch.update_ignore_radio_edit(
                self.spotify_ignore_radio_edit_switch.get_active())

    def on_spotify_playlist_added(self, _list_name):
        self._populate_spotify_playlists()

    def on_add_spotify_playlist(self, *_args):
        # Imported here: wishlists.py is a sibling page, only needed for this dialog
        from pynicotine.gtkgui.wishlists import SpotifyPlaylistPickerDialog
        SpotifyPlaylistPickerDialog(self.window.application, self.on_spotify_playlist_added).present()

    def on_remove_spotify_playlist(self, *_args):

        if core.spotify_watch is None:
            return

        iterator = next(self.spotify_playlists_view.get_selected_rows(), None)

        if iterator is None:
            return

        playlist_id = self.spotify_playlists_view.get_row_value(iterator, "playlist_id")
        core.spotify_watch.remove_watched_playlist(playlist_id)
        self._populate_spotify_playlists()

    def on_lexicon_import_progress(self, done, total):
        """Live progress of the Lexicon import queue, from the sync worker."""

        if total <= 0:
            return

        if done >= total:
            self.lexicon_progress_bar.set_fraction(1.0)
            self.lexicon_progress_bar.set_text(
                _("Lexicon import finished (%(total)s file(s))") % {"total": total})

            # Leave the finished state visible briefly, then tidy up --
            # unless a new pass has started updating the bar again
            def hide_if_done():
                if self.lexicon_progress_bar.get_fraction() >= 1.0:
                    self.lexicon_progress_bar.set_visible(False)

            events.schedule(delay=5, callback=lambda: events.invoke_main_thread(hide_if_done))
            return

        self.lexicon_progress_bar.set_visible(True)
        self.lexicon_progress_bar.set_fraction(done / total)
        self.lexicon_progress_bar.set_text(
            _("Importing into Lexicon: %(done)s / %(total)s") % {"done": done, "total": total})

    # Library-first prompt #

    def on_lexicon_unreachable_response(self, dialog, response_id, _data):

        if core.lexicon_sync is None or core.download_lists is None:
            return

        if response_id == "retry":
            core.lexicon_sync.retry_library_check()
            return

        if response_id == "download":
            core.download_lists.release_library_check_items()

    def on_lexicon_unreachable(self, num_waiting):
        """LexiconSync wants a library check but Lexicon isn't reachable:
        shown once per outage. Retry re-probes (reopening Lexicon first is on
        the user); downloading without the check releases the waiting songs."""

        OptionDialog(
            application=self.window.application,
            title=_("Lexicon Not Reachable"),
            message=_("%(num)s song(s) are waiting to be checked against your Lexicon library "
                      "before downloading, but Lexicon isn't reachable.\n\nOpen Lexicon (with its "
                      "Local API enabled under Settings → Integrations) and retry, or download "
                      "without the check — songs you already own may be re-downloaded.") % {
                "num": num_waiting},
            buttons=[
                ("retry", _("_Retry")),
                ("download", _("Continue and _Download"))
            ],
            callback=self.on_lexicon_unreachable_response
        ).present()

    # Page callbacks #

    def _on_lists_changed(self, *_args):
        # Watched playlists and their lists are joined at the hip: removing a
        # list unwatches its playlist, so refresh the view either way
        self._populate_spotify_playlists()

    def on_focus(self, *_args):

        self._populate_spotify_playlists()

        if core.lexicon_sync is not None:
            self.lexicon_status_label.set_label(_("Checking connection…"))
            core.lexicon_sync.check_connection(self._set_lexicon_status)

        self.sync_now_button.grab_focus()
        return True
