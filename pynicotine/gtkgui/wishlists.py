# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import shutil
import subprocess
import sys

from gi.repository import Gtk

from pynicotine import audioquality
from pynicotine.config import config
from pynicotine.core import core
from pynicotine.downloadlists import DownloadLists
from pynicotine.downloadlists import DownloadListItemStatus
from pynicotine.events import events
from pynicotine.gtkgui.application import GTK_API_VERSION
from pynicotine.gtkgui.widgets import ui
from pynicotine.gtkgui.widgets.combobox import ComboBox
from pynicotine.gtkgui.widgets.dialogs import Dialog
from pynicotine.gtkgui.widgets.dialogs import EntryDialog
from pynicotine.gtkgui.widgets.dialogs import OptionDialog
from pynicotine.gtkgui.widgets.filechooser import FileChooserButton
from pynicotine.gtkgui.widgets.filechooser import FileChooserSave
from pynicotine.gtkgui.widgets.filechooser import FolderChooser
from pynicotine.gtkgui.widgets.popupmenu import PopupMenu
from pynicotine.gtkgui.widgets.theme import add_css_class
from pynicotine.gtkgui.widgets.treeview import TreeView
from pynicotine.logfacility import log
from pynicotine.spotifywatch import SPOTIFY_SCRAPER_AVAILABLE
from pynicotine.utils import open_file_path


class ListSettingsDialog(Dialog):
    """Per-list settings: destination folder, match quality/length/accuracy
    preferences, and whether the list actively downloads. The matching/download
    preferences follow the overall Wishlist Settings defaults unless the
    "Override" switch is turned on for this list."""

    QUALITY_ITEMS = (
        (_("Any"), "any"),
        (_("Good (192 kbps+)"), "good"),
        (_("High (320 kbps+)"), "high"),
        (_("Lossless only (FLAC/WAV)"), "lossless")
    )

    def __init__(self, application, download_list, callback):

        self.download_list = download_list
        self.callback = callback
        self._override_widgets = []

        cancel_button = Gtk.Button(label=_("_Cancel"), use_underline=True, visible=True)
        cancel_button.connect("clicked", self.on_cancel)

        save_button = Gtk.Button(label=_("_Save"), use_underline=True, visible=True)
        save_button.connect("clicked", self.on_save)
        add_css_class(save_button, "suggested-action")

        self.primary_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, width_request=340, visible=True,
            margin_top=14, margin_bottom=14, margin_start=18, margin_end=18, spacing=18
        )

        super().__init__(
            application=application,
            content_box=self.primary_container,
            buttons_start=(cancel_button,),
            buttons_end=(save_button,),
            default_button=save_button,
            title=_("List Settings — %s") % download_list.name,
            width=420,
            height=-1
        )

        self._add_folder_option()
        self._add_override_switch()
        self._add_quality_option()
        self._add_prefer_longer_option()
        self._add_prefer_lossless_option()
        self._add_keywords_option()
        self._add_fuzzy_option()
        self._add_auto_download_option()
        self._add_name_subfolder_option()

        self._update_override_sensitivity()

    def destroy(self):
        self.__dict__.clear()

    def _append(self, widget):
        if GTK_API_VERSION >= 4:
            self.primary_container.append(widget)  # pylint: disable=no-member
        else:
            self.primary_container.add(widget)      # pylint: disable=no-member

    def _labeled_row(self, label_text, widget, tooltip_text=None, track_override=False):
        """A row with a label on the left and a control (switch, spinner,
        combobox container) on the right, label added first so it stays
        leftmost."""

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

        if track_override:
            self._override_widgets.append(widget)

        return row

    def _add_folder_option(self):

        label = Gtk.Label(
            label=_("Download folder for this list:"), wrap=True, xalign=0, visible=True)
        self._append(label)

        row = Gtk.Box(visible=True)
        self._append(row)

        self.folder_chooser = FileChooserButton(row, self.application, chooser_type="folder")

        if self.download_list.download_folder_path:
            self.folder_chooser.set_path(self.download_list.download_folder_path)

    def _add_override_switch(self):

        has_override = any(
            value is not None for value in (
                self.download_list.quality, self.download_list.prefer_longer,
                self.download_list.prefer_lossless, self.download_list.preferred_keywords,
                self.download_list.fuzzy_match_threshold, self.download_list.auto_download,
                self.download_list.use_name_subfolder
            )
        )

        self.override_switch = Gtk.Switch(active=has_override, valign=Gtk.Align.CENTER, visible=True)
        self.override_switch.connect("notify::active", self.on_override_toggled)
        self._labeled_row(
            _("Override overall settings for this list"), self.override_switch,
            tooltip_text=_("When off, this list follows the overall matching/download settings "
                            "from Wishlist Settings, and picks up any future changes to them."))

    def _add_quality_option(self):

        row = Gtk.Box(spacing=12, visible=True)
        label = Gtk.Label(
            label=_("Minimum file quality:"), hexpand=True, wrap=True, xalign=0, visible=True)

        if GTK_API_VERSION >= 4:
            row.append(label)  # pylint: disable=no-member
        else:
            row.add(label)     # pylint: disable=no-member

        self._append(row)

        self.quality_combobox = ComboBox(container=row, items=self.QUALITY_ITEMS)
        self.quality_combobox.set_selected_id(self.download_list.effective_quality)
        label.set_mnemonic_widget(self.quality_combobox.widget)

        self._override_widgets.append(self.quality_combobox.widget)

    def _add_prefer_longer_option(self):

        self.prefer_longer_switch = Gtk.Switch(
            active=self.download_list.effective_prefer_longer, valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Prefer longer/extended versions of a song"), self.prefer_longer_switch, track_override=True)

    def _add_prefer_lossless_option(self):

        self.prefer_lossless_switch = Gtk.Switch(
            active=self.download_list.effective_prefer_lossless, valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Prefer lossless (FLAC/WAV) over lossy when both are available"), self.prefer_lossless_switch,
            track_override=True,
            tooltip_text=_("On: a lossless result wins over an otherwise-equal lossy one. Off: a lossy "
                            "(e.g. mp3) result wins instead. Doesn't exclude either format by itself — "
                            "pair with a minimum file quality of \"Lossless only\" above to require lossless.")
        )

    def _add_keywords_option(self):

        label = Gtk.Label(
            label=_("Preferred source keywords (comma-separated):"), wrap=True, xalign=0, visible=True)
        self._append(label)

        self.keywords_entry = Gtk.Entry(
            placeholder_text=_("e.g. beatport, bp"), visible=True,
            text=self.download_list.effective_preferred_keywords or "",
            tooltip_text=_('Breaks ties in favor of results whose folder path contains one of these '
                            'words, e.g. "beatport, bp" prefers a result from a "BP Sep 2025" folder '
                            'over an otherwise-equal one that isn\'t. Leave blank to not prefer any source.')
        )
        label.set_mnemonic_widget(self.keywords_entry)
        self._append(self.keywords_entry)

        self._override_widgets.append(self.keywords_entry)

    def _add_fuzzy_option(self):

        self.fuzzy_spinner = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=self.download_list.effective_fuzzy_match_threshold, lower=0, upper=100,
                step_increment=5, page_increment=10, page_size=0
            ),
            climb_rate=1, digits=0, valign=Gtk.Align.CENTER, visible=True
        )
        self._labeled_row(_("Minimum match accuracy (%):"), self.fuzzy_spinner, track_override=True)

    def _add_auto_download_option(self):

        self.auto_download_switch = Gtk.Switch(
            active=self.download_list.effective_auto_download, valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Automatically download matches"), self.auto_download_switch, track_override=True,
            tooltip_text=_("When off, this list is paused: nothing is searched for or downloaded "
                            "until you turn it back on.")
        )

    def _add_name_subfolder_option(self):

        self.name_subfolder_switch = Gtk.Switch(
            active=self.download_list.effective_use_name_subfolder, valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Save into a subfolder named after this list"), self.name_subfolder_switch, track_override=True,
            tooltip_text=_('E.g. a list named "Beatport Charts" saves into a "Beatport Charts" '
                            "subfolder of the download folder above.")
        )

    def _update_override_sensitivity(self):

        is_overridden = self.override_switch.get_active()

        for widget in self._override_widgets:
            widget.set_sensitive(is_overridden)

    def on_override_toggled(self, *_args):
        self._update_override_sensitivity()

    def on_cancel(self, *_args):
        self.close()

    def on_save(self, *_args):

        is_overridden = self.override_switch.get_active()

        settings = {
            "download_folder_path": self.folder_chooser.get_path(),
            "quality": self.quality_combobox.get_selected_id() if is_overridden else None,
            "prefer_longer": self.prefer_longer_switch.get_active() if is_overridden else None,
            "prefer_lossless": self.prefer_lossless_switch.get_active() if is_overridden else None,
            "preferred_keywords": self.keywords_entry.get_text().strip() if is_overridden else None,
            "fuzzy_match_threshold": self.fuzzy_spinner.get_value_as_int() if is_overridden else None,
            "auto_download": self.auto_download_switch.get_active() if is_overridden else None,
            "use_name_subfolder": self.name_subfolder_switch.get_active() if is_overridden else None
        }

        self.callback(self.download_list.name, settings)
        self.close()


class WishlistSettingsDialog(Dialog):
    """Global wishlist settings: the watch folder that's polled for song list
    files exported by other applications, and the overall matching/download
    defaults that lists follow unless they override them individually."""

    def __init__(self, application, callback):

        self.callback = callback

        cancel_button = Gtk.Button(label=_("_Cancel"), use_underline=True, visible=True)
        cancel_button.connect("clicked", self.on_cancel)

        save_button = Gtk.Button(label=_("_Save"), use_underline=True, visible=True)
        save_button.connect("clicked", self.on_save)
        add_css_class(save_button, "suggested-action")

        self.primary_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, width_request=460, visible=True,
            margin_top=14, margin_bottom=14, margin_start=18, margin_end=18, spacing=18
        )
        self.scrolled_window = Gtk.ScrolledWindow(
            child=self.primary_container, hexpand=True, vexpand=True, min_content_height=300,
            hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC, visible=True
        )

        super().__init__(
            application=application,
            content_box=self.scrolled_window,
            buttons_start=(cancel_button,),
            buttons_end=(save_button,),
            default_button=save_button,
            title=_("Wishlist Settings"),
            width=560,
            height=700
        )

        self._add_watch_folder_option()
        self._append(Gtk.Separator(visible=True))
        self._add_spotify_watch_option()
        self._append(Gtk.Separator(visible=True))
        self._add_default_settings_options()
        self._append(Gtk.Separator(visible=True))
        self._add_stall_settings_options()

    def destroy(self):
        self.spotify_playlists_view.destroy()
        self.__dict__.clear()

    def _append(self, widget):
        if GTK_API_VERSION >= 4:
            self.primary_container.append(widget)  # pylint: disable=no-member
        else:
            self.primary_container.add(widget)      # pylint: disable=no-member

    def _labeled_row(self, label_text, widget, tooltip_text=None):
        """A row with a label on the left and a control (switch, spinner,
        combobox container) on the right, label added first so it stays
        leftmost."""

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

    def _add_default_settings_options(self):

        heading = Gtk.Label(
            label=_("Overall list defaults"), wrap=True, xalign=0, visible=True)
        add_css_class(heading, "heading")
        self._append(heading)

        transfers = config.sections["transfers"]

        row = Gtk.Box(spacing=12, visible=True)
        quality_label = Gtk.Label(
            label=_("Minimum file quality:"), hexpand=True, wrap=True, xalign=0, visible=True)

        if GTK_API_VERSION >= 4:
            row.append(quality_label)  # pylint: disable=no-member
        else:
            row.add(quality_label)     # pylint: disable=no-member

        self._append(row)

        self.default_quality_combobox = ComboBox(container=row, items=ListSettingsDialog.QUALITY_ITEMS)
        self.default_quality_combobox.set_selected_id(transfers["downloadlistdefaultquality"])
        quality_label.set_mnemonic_widget(self.default_quality_combobox.widget)

        self.default_prefer_longer_switch = Gtk.Switch(
            active=transfers["downloadlistdefaultpreferlonger"], valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(_("Prefer longer/extended versions of a song"), self.default_prefer_longer_switch)

        self.default_prefer_lossless_switch = Gtk.Switch(
            active=transfers["downloadlistdefaultpreferlossless"], valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Prefer lossless (FLAC/WAV) over lossy when both are available"), self.default_prefer_lossless_switch,
            tooltip_text=_("On: a lossless result wins over an otherwise-equal lossy one. Off: a lossy "
                            "(e.g. mp3) result wins instead. Doesn't exclude either format by itself — "
                            "pair with a minimum file quality of \"Lossless only\" above to require lossless."))

        keywords_label = Gtk.Label(
            label=_("Preferred source keywords (comma-separated):"), wrap=True, xalign=0, visible=True)
        self._append(keywords_label)

        self.default_keywords_entry = Gtk.Entry(
            placeholder_text=_("e.g. beatport, bp"), visible=True,
            text=transfers["downloadlistdefaultkeywords"],
            tooltip_text=_('Breaks ties in favor of results whose folder path contains one of these '
                            'words, e.g. "beatport, bp" prefers a result from a "BP Sep 2025" folder '
                            'over an otherwise-equal one that isn\'t. Leave blank to not prefer any source.')
        )
        keywords_label.set_mnemonic_widget(self.default_keywords_entry)
        self._append(self.default_keywords_entry)

        self.default_fuzzy_spinner = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=transfers["downloadlistdefaultfuzzy"], lower=0, upper=100,
                step_increment=5, page_increment=10, page_size=0
            ),
            climb_rate=1, digits=0, valign=Gtk.Align.CENTER, visible=True
        )
        self._labeled_row(_("Minimum match accuracy (%):"), self.default_fuzzy_spinner)

        self.default_auto_download_switch = Gtk.Switch(
            active=transfers["downloadlistdefaultautodownload"], valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Automatically download matches"), self.default_auto_download_switch,
            tooltip_text=_("Applies to any list that doesn't override this setting for itself."))

        self.default_name_subfolder_switch = Gtk.Switch(
            active=transfers["downloadlistdefaultnamesubfolder"], valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Save into a subfolder named after the list"), self.default_name_subfolder_switch)

        self.apply_to_existing_switch = Gtk.Switch(active=False, valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Apply to current lists"), self.apply_to_existing_switch,
            tooltip_text=_("Clears every existing list's own override for the settings above, switching "
                            "them all over to these defaults immediately. Off by default, since lists "
                            "with their own override normally keep it. Doesn't touch download folders.")
        )

    def _add_stall_settings_options(self):

        heading = Gtk.Label(
            label=_("Concurrent downloads"), wrap=True, xalign=0, visible=True)
        add_css_class(heading, "heading")
        self._append(heading)

        transfers = config.sections["transfers"]

        self.max_concurrent_spinner = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=transfers["downloadlistmaxconcurrent"], lower=1, upper=50,
                step_increment=1, page_increment=5, page_size=0
            ),
            climb_rate=1, digits=0, valign=Gtk.Align.CENTER, visible=True
        )
        self._labeled_row(
            _("Maximum songs being searched for or downloaded at once (across all lists):"),
            self.max_concurrent_spinner,
            tooltip_text=_("Raise this to work through a big list faster. Actual simultaneous "
                            "transfers may still be lower than this, since a given source can only "
                            "send you as many files at once as they allow.")
        )

        self.quality_check_switch = Gtk.Switch(
            active=transfers["downloadlistqualitycheck"], valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Check the real quality of finished downloads (spectrum analysis):"),
            self.quality_check_switch,
            tooltip_text=_("Measures where a finished file's audio content stops, the way Spek shows "
                            "it. A \"320 kbps\" or lossless file that was made from a 128 kbps encode "
                            "stops around 16 kHz instead of 19-20 kHz: it is moved to a \"Rejected "
                            "Quality\" folder and the song is searched for again from another "
                            "source. Needs ffmpeg (or afconvert on macOS).")
        )

        if not audioquality.decoder_available():
            self.quality_check_switch.set_sensitive(False)
            self.quality_check_switch.set_tooltip_text(_("ffmpeg was not found, so downloads can't be analyzed"))

        heading = Gtk.Label(
            label=_("Stalled downloads"), wrap=True, xalign=0, visible=True)
        add_css_class(heading, "heading")
        self._append(heading)

        self.stall_timeout_spinner = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=transfers["downloadliststalltimeout"], lower=5, upper=600,
                step_increment=5, page_increment=30, page_size=0
            ),
            climb_rate=1, digits=0, valign=Gtk.Align.CENTER, visible=True
        )
        self._labeled_row(
            _("Give up on a download after this many seconds below the minimum speed:"),
            self.stall_timeout_spinner,
            tooltip_text=_("A download stuck at this speed for longer than this is abandoned, and the "
                            "song is searched for again from a different source.")
        )

        self.min_speed_spinner = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=transfers["downloadlistminspeed"], lower=0, upper=10000,
                step_increment=1, page_increment=10, page_size=0
            ),
            climb_rate=1, digits=0, valign=Gtk.Align.CENTER, visible=True
        )
        self._labeled_row(
            _("Minimum acceptable speed (KiB/s):"), self.min_speed_spinner,
            tooltip_text=_("Raise this if your connection is generally fast and you want slow sources "
                            "dropped sooner; lower it (even to 0) if your own connection is slow, so "
                            "downloads that are merely as fast as your line allows aren't mistaken for "
                            "a stalled/bad source and abandoned. A download that's fully received but "
                            "still finalizing is never treated as stalled, regardless of this setting.")
        )

    def _add_watch_folder_option(self):

        enabled = config.sections["transfers"]["downloadlistwatchenabled"]
        folder_path = config.sections["transfers"]["downloadlistwatchfolder"]

        toggle_row = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER, visible=True)
        toggle_label = Gtk.Label(
            label=_("Watch folder for song list files"), hexpand=True, wrap=True, xalign=0, visible=True,
            tooltip_text=_("Periodically scan a folder for .txt/.csv song list files exported by other "
                            "applications, and import them as songs to search for."))

        self.watch_enabled_switch = Gtk.Switch(active=enabled, valign=Gtk.Align.CENTER, visible=True)
        toggle_label.set_mnemonic_widget(self.watch_enabled_switch)

        if GTK_API_VERSION >= 4:
            toggle_row.append(toggle_label)          # pylint: disable=no-member
            toggle_row.append(self.watch_enabled_switch)  # pylint: disable=no-member
        else:
            toggle_row.add(toggle_label)             # pylint: disable=no-member
            toggle_row.add(self.watch_enabled_switch)  # pylint: disable=no-member

        self._append(toggle_row)

        folder_label = Gtk.Label(label=_("Folder to watch:"), wrap=True, xalign=0, visible=True)
        self._append(folder_label)

        folder_row = Gtk.Box(spacing=6, visible=True)
        self._append(folder_row)

        self.folder_entry = Gtk.Entry(
            hexpand=True, placeholder_text=_("Paste or type a folder path…"), visible=True, text=folder_path or "")
        folder_label.set_mnemonic_widget(self.folder_entry)

        browse_button = Gtk.Button(tooltip_text=_("Browse…"), valign=Gtk.Align.CENTER, visible=True)
        browse_button.connect("clicked", self.on_browse_folder)

        if GTK_API_VERSION >= 4:
            browse_button.set_icon_name("folder-symbolic")  # pylint: disable=no-member
            folder_row.append(self.folder_entry)             # pylint: disable=no-member
            folder_row.append(browse_button)                 # pylint: disable=no-member
        else:
            browse_button.set_image(Gtk.Image(icon_name="folder-symbolic"))  # pylint: disable=no-member
            folder_row.add(self.folder_entry)                 # pylint: disable=no-member
            folder_row.add(browse_button)                     # pylint: disable=no-member

    def on_browse_folder_response(self, selected, _data):

        selected_path = next(iter(selected), None)

        if selected_path:
            self.folder_entry.set_text(selected_path)

    def on_browse_folder(self, *_args):

        FolderChooser(
            application=self.application,
            callback=self.on_browse_folder_response,
            initial_folder=self.folder_entry.get_text().strip()
        ).present()

    def _add_spotify_watch_option(self):
        """A second, independent way to auto-populate a wishlist: watch a
        Spotify playlist instead of a local folder of exported song list
        files. Reads public playlist data anonymously (see
        pynicotine/spotifywatch.py's module docstring) -- no Spotify
        account, login, or Developer app needed, but a private playlist
        can't be watched this way."""

        heading = Gtk.Label(label=_("Spotify playlist watcher"), wrap=True, xalign=0, visible=True)
        add_css_class(heading, "heading")
        self._append(heading)

        spotify = config.sections["spotify"]

        if not SPOTIFY_SCRAPER_AVAILABLE:
            unavailable_hint = Gtk.Label(
                label=_("The optional \"spotifyscraper\" package isn't installed -- install it with "
                        "\"pip install spotifyscraper\" to use this."),
                wrap=True, xalign=0, visible=True)
            add_css_class(unavailable_hint, "dim-label")
            self._append(unavailable_hint)

        self.spotify_ignore_radio_edit_switch = Gtk.Switch(
            active=spotify["watch_ignore_radio_edit"], valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _('Ignore "Radio Edit" — prefer the Extended/Original version'),
            self.spotify_ignore_radio_edit_switch,
            tooltip_text=_('Strips a "(Radio Edit)" tag from a track\'s title before searching for it, '
                            "so the longer Extended/Original version is found instead of specifically "
                            "requiring the shortened radio one. A version is still downloaded even if "
                            "no Extended/Original is found — this only affects which one is preferred.")
        )

        playlists_label = Gtk.Label(
            label=_("Watched playlists:"), wrap=True, xalign=0, visible=True,
            tooltip_text=_("Any track added to one of these playlists is added as a song to search "
                            "for, in a wishlist named after the playlist. You can watch as many "
                            "playlists as you like, your own or someone else's."))
        self._append(playlists_label)

        self.spotify_playlists_container = Gtk.ScrolledWindow(
            hexpand=True, min_content_height=120, max_content_height=160,
            hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC, visible=True)
        self._append(self.spotify_playlists_container)

        self.spotify_playlists_view = TreeView(
            self.application.window, parent=self.spotify_playlists_container,
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

        watched_buttons_row = Gtk.Box(spacing=6, visible=True)
        self._append(watched_buttons_row)

        add_playlist_button = Gtk.Button(label=_("_Add Playlist to Watch…"), use_underline=True, visible=True)
        add_playlist_button.connect("clicked", self.on_add_spotify_playlist)

        remove_playlist_button = Gtk.Button(label=_("_Remove"), use_underline=True, visible=True)
        remove_playlist_button.connect("clicked", self.on_remove_spotify_playlist)

        if GTK_API_VERSION >= 4:
            watched_buttons_row.append(add_playlist_button)     # pylint: disable=no-member
            watched_buttons_row.append(remove_playlist_button)  # pylint: disable=no-member
        else:
            watched_buttons_row.add(add_playlist_button)        # pylint: disable=no-member
            watched_buttons_row.add(remove_playlist_button)     # pylint: disable=no-member

    def _populate_spotify_playlists(self):

        self.spotify_playlists_view.freeze()
        self.spotify_playlists_view.clear()

        for playlist in core.spotify_watch.get_watched_playlists():
            self.spotify_playlists_view.add_row(
                [playlist["playlist_id"], playlist["list_name"]], select_row=False)

        self.spotify_playlists_view.unfreeze()

    def on_spotify_playlist_added(self, _list_name):
        self._populate_spotify_playlists()

    def on_add_spotify_playlist(self, *_args):
        SpotifyPlaylistPickerDialog(self.application, self.on_spotify_playlist_added).present()

    def on_remove_spotify_playlist(self, *_args):

        iterator = next(self.spotify_playlists_view.get_selected_rows(), None)

        if iterator is None:
            return

        playlist_id = self.spotify_playlists_view.get_row_value(iterator, "playlist_id")
        core.spotify_watch.remove_watched_playlist(playlist_id)
        self._populate_spotify_playlists()

    def on_cancel(self, *_args):
        self.close()

    def on_save(self, *_args):

        folder_path = os.path.expandvars(self.folder_entry.get_text().strip())

        self.callback(
            watch_enabled=self.watch_enabled_switch.get_active(),
            watch_folder_path=folder_path,
            quality=self.default_quality_combobox.get_selected_id(),
            prefer_longer=self.default_prefer_longer_switch.get_active(),
            prefer_lossless=self.default_prefer_lossless_switch.get_active(),
            preferred_keywords=self.default_keywords_entry.get_text().strip(),
            fuzzy_match_threshold=self.default_fuzzy_spinner.get_value_as_int(),
            auto_download=self.default_auto_download_switch.get_active(),
            use_name_subfolder=self.default_name_subfolder_switch.get_active(),
            apply_to_existing_lists=self.apply_to_existing_switch.get_active(),
            stall_timeout=self.stall_timeout_spinner.get_value_as_int(),
            min_speed_kib=self.min_speed_spinner.get_value_as_int(),
            max_concurrent=self.max_concurrent_spinner.get_value_as_int(),
            quality_check=self.quality_check_switch.get_active(),
            spotify_ignore_radio_edit=self.spotify_ignore_radio_edit_switch.get_active()
        )
        self.close()


class SpotifyPlaylistPickerDialog(Dialog):
    """Pick a Spotify playlist to watch -- either by pasting the URL/ID of
    any public playlist, or by searching Spotify for one by name (both work
    for anyone's playlist, not just any particular account's -- see
    pynicotine/spotifywatch.py's module docstring for why no login is
    needed, and why that means a PRIVATE playlist can't be read this way).
    Used both from Wishlist Settings ("Add Playlist to Watch…") and from
    Add List ("Add from Spotify Playlist…") -- in both places, picking a
    playlist starts watching it and creates a wishlist named after it,
    identically."""

    def __init__(self, application, on_playlist_added):

        self.on_playlist_added = on_playlist_added

        close_button = Gtk.Button(label=_("_Close"), use_underline=True, visible=True)
        close_button.connect("clicked", self.on_close)

        self.primary_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, width_request=420, visible=True,
            margin_top=14, margin_bottom=14, margin_start=18, margin_end=18, spacing=12
        )

        super().__init__(
            application=application,
            content_box=self.primary_container,
            buttons_end=(close_button,),
            default_button=close_button,
            title=_("Watch a Spotify Playlist"),
            width=460,
            height=480
        )

        url_label = Gtk.Label(
            label=_("Playlist URL or ID (any public playlist):"),
            wrap=True, xalign=0, visible=True)
        self._append(url_label)

        url_row = Gtk.Box(spacing=6, visible=True)
        self._append(url_row)

        self.url_entry = Gtk.Entry(
            hexpand=True, visible=True, placeholder_text=_("e.g. https://open.spotify.com/playlist/…"))
        self.url_entry.connect("activate", self.on_watch_url)
        url_label.set_mnemonic_widget(self.url_entry)

        watch_button = Gtk.Button(label=_("_Watch"), use_underline=True, visible=True)
        watch_button.connect("clicked", self.on_watch_url)

        if GTK_API_VERSION >= 4:
            url_row.append(self.url_entry)    # pylint: disable=no-member
            url_row.append(watch_button)      # pylint: disable=no-member
        else:
            url_row.add(self.url_entry)       # pylint: disable=no-member
            url_row.add(watch_button)         # pylint: disable=no-member

        self.status_label = Gtk.Label(wrap=True, xalign=0, visible=False)
        self._append(self.status_label)

        self._append(Gtk.Separator(visible=True))

        search_label = Gtk.Label(label=_("Or search Spotify for a playlist:"), wrap=True, xalign=0, visible=True)
        self._append(search_label)

        self.playlist_search_entry = Gtk.SearchEntry(
            placeholder_text=_("e.g. a mood, genre, artist, or playlist name…"), visible=True)
        self.playlist_search_entry.connect("search-changed", self.on_playlist_search_changed)
        search_label.set_mnemonic_widget(self.playlist_search_entry)
        self._append(self.playlist_search_entry)

        self.playlists_container = Gtk.ScrolledWindow(
            hexpand=True, vexpand=True, visible=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self._append(self.playlists_container)

        self.playlists_view = TreeView(
            application.window, parent=self.playlists_container,
            columns={
                "id": {
                    "iterator_key": True
                },
                "name": {
                    "column_type": "text",
                    "title": _("Playlist"),
                    "expand_column": True
                },
                "owner": {
                    "column_type": "text",
                    "title": _("Owner"),
                    "width": 120
                }
            },
            activate_row_callback=self.on_playlist_row_activated
        )

        if not SPOTIFY_SCRAPER_AVAILABLE:
            self._set_status(core.spotify_watch.unavailable_message())
            self.url_entry.set_sensitive(False)
            watch_button.set_sensitive(False)
            self.playlist_search_entry.set_sensitive(False)

    def destroy(self):
        self.playlists_view.destroy()
        self.__dict__.clear()

    def _append(self, widget):
        if GTK_API_VERSION >= 4:
            self.primary_container.append(widget)  # pylint: disable=no-member
        else:
            self.primary_container.add(widget)      # pylint: disable=no-member

    def _set_status(self, text):
        self.status_label.set_text(text)
        self.status_label.set_visible(bool(text))

    def on_playlist_search_changed(self, entry, *_args):

        query = entry.get_text().strip()

        if not query:
            self.playlists_view.freeze()
            self.playlists_view.clear()
            self.playlists_view.unfreeze()
            self._set_status("")
            return

        self._set_status(_("Searching…"))
        core.spotify_watch.search_playlists(query, self.on_search_results)

    def on_search_results(self, playlists, error):

        if not self.is_visible():
            return

        if error is not None:
            self._set_status(error)
            return

        self.playlists_view.freeze()
        self.playlists_view.clear()

        for playlist in playlists:
            self.playlists_view.add_row(
                [playlist["id"], playlist["name"], playlist["owner"]], select_row=False)

        self.playlists_view.unfreeze()

        self._set_status(_("No playlists found.") if not playlists else "")

    def _watch_playlist(self, playlist_url_or_id):

        self._set_status(_("Adding playlist…"))
        core.spotify_watch.add_watched_playlist(playlist_url_or_id, self.on_watch_result)

    def on_watch_result(self, success, message_or_list_name):

        if not self.is_visible():
            return

        if not success:
            self._set_status(message_or_list_name)
            return

        self._set_status("")
        self.url_entry.set_text("")
        self.on_playlist_added(message_or_list_name)

    def on_watch_url(self, *_args):

        playlist_url_or_id = self.url_entry.get_text().strip()

        if not playlist_url_or_id:
            return

        self._watch_playlist(playlist_url_or_id)

    def on_playlist_row_activated(self, list_view, iterator, _column_id):
        playlist_id = list_view.get_row_value(iterator, "id")
        self._watch_playlist(playlist_id)

    def on_close(self, *_args):
        self.close()


class AddListDialog(Dialog):
    """Create a new wishlist, either by typing a plain name (as before), or
    by picking a Spotify playlist to watch -- which creates and names the
    list automatically, the same way as adding a watched playlist from
    Wishlist Settings does."""

    def __init__(self, application, on_list_added):

        self.on_list_added = on_list_added

        cancel_button = Gtk.Button(label=_("_Cancel"), use_underline=True, visible=True)
        cancel_button.connect("clicked", self.on_cancel)

        self.add_button = Gtk.Button(label=_("_Add"), use_underline=True, visible=True)
        self.add_button.connect("clicked", self.on_add)
        add_css_class(self.add_button, "suggested-action")

        self.primary_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, width_request=400, visible=True,
            margin_top=14, margin_bottom=14, margin_start=18, margin_end=18, spacing=12
        )

        super().__init__(
            application=application,
            content_box=self.primary_container,
            buttons_start=(cancel_button,),
            buttons_end=(self.add_button,),
            default_button=self.add_button,
            title=_("Add List"),
            width=440,
            height=-1
        )

        name_label = Gtk.Label(
            label=_("Enter a name for the new download list:"), wrap=True, xalign=0, visible=True)
        self._append(name_label)

        self.name_entry = Gtk.Entry(visible=True)
        self.name_entry.connect("activate", self.on_add)
        name_label.set_mnemonic_widget(self.name_entry)
        self._append(self.name_entry)

        self._append(Gtk.Separator(visible=True))

        spotify_row = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER, visible=True)
        spotify_label = Gtk.Label(
            label=_("Or create it from a Spotify playlist you watch:"), hexpand=True, wrap=True, xalign=0,
            visible=True)

        spotify_button = Gtk.Button(
            label=_("Add from Spotify _Playlist…"), use_underline=True, valign=Gtk.Align.CENTER, visible=True)
        spotify_button.connect("clicked", self.on_add_from_spotify)

        if GTK_API_VERSION >= 4:
            spotify_row.append(spotify_label)   # pylint: disable=no-member
            spotify_row.append(spotify_button)  # pylint: disable=no-member
        else:
            spotify_row.add(spotify_label)      # pylint: disable=no-member
            spotify_row.add(spotify_button)     # pylint: disable=no-member

        self._append(spotify_row)

    def destroy(self):
        self.__dict__.clear()

    def _append(self, widget):
        if GTK_API_VERSION >= 4:
            self.primary_container.append(widget)  # pylint: disable=no-member
        else:
            self.primary_container.add(widget)      # pylint: disable=no-member

    def on_cancel(self, *_args):
        self.close()

    def on_add(self, *_args):

        name = self.name_entry.get_text().strip()

        if not name:
            return

        self.on_list_added(name)
        self.close()

    def on_spotify_playlist_added(self, list_name):
        # The playlist has already created/registered its own wishlist by
        # this point (SpotifyWatch.add_watched_playlist does that itself) --
        # just let the caller know a list now exists, and close up
        self.on_list_added(list_name, already_created=True)
        self.close()

    def on_add_from_spotify(self, *_args):
        SpotifyPlaylistPickerDialog(self.application, self.on_spotify_playlist_added).present()


class VerifyMatchesDialog(Dialog):
    """Review a list's completed items side by side: the original search
    term, the actual downloaded file, and its match percentage -- stripped
    down to just those three columns so a mismatched download (see
    DownloadLists._purity_percentage's docstring for why the percentage
    shown here can be less than 100% even for an accepted match) is easy to
    spot and double check, without every other column's noise in the way."""

    def __init__(self, application, download_list):

        self.download_list = download_list

        close_button = Gtk.Button(label=_("_Close"), use_underline=True, visible=True)
        close_button.connect("clicked", self.on_close)

        self.list_container = Gtk.ScrolledWindow(hexpand=True, vexpand=True, visible=True)

        super().__init__(
            application=application,
            content_box=self.list_container,
            buttons_end=(close_button,),
            default_button=close_button,
            title=_("Verify Matches — %s") % download_list.name,
            width=760,
            height=500
        )

        self.list_view = TreeView(
            application.window, parent=self.list_container,
            columns={
                "match": {
                    "column_type": "number",
                    "title": _("Match %"),
                    "width": 100,
                    "default_sort_type": "ascending"
                },
                "term": {
                    "column_type": "text",
                    "title": _("Search Term"),
                    "width": 260,
                    "iterator_key": True
                },
                "downloaded_file": {
                    "column_type": "text",
                    "title": _("Downloaded File"),
                    "width": 340,
                    "expand_column": True
                }
            }
        )

        self._populate()

    def destroy(self):
        self.list_view.destroy()
        self.__dict__.clear()

    def _populate(self):

        self.list_view.freeze()

        for item in self.download_list.items.values():
            if item.status != DownloadListItemStatus.COMPLETED:
                continue

            self.list_view.add_row(
                [item.h_match_percentage, item.term, item.download_filename], select_row=False)

        self.list_view.unfreeze()

    def on_close(self, *_args):
        self.close()


class Wishlists:

    STATUS_LABELS = DownloadLists.STATUS_LABELS
    PAUSE_LABEL = _("_Pause")
    RESUME_LABEL = _("_Resume")
    PIN_LABEL = _("_Pin")
    UNPIN_LABEL = _("_Unpin")

    def __init__(self, window):

        (
            self.add_list_button,
            self.add_songs_button,
            self.container,
            self.current_list_label,
            self.export_summary_button,
            self.folders_completed_lists_container,
            self.folders_completed_section,
            self.folders_heading,
            self.folders_lists_container,
            self.items_container,
            self.items_pane,
            self.items_search_entry,
            self.list_settings_button,
            self.lists_pane,
            self.pause_resume_button,
            self.retry_not_found_button,
            self.spotify_category_section,
            self.spotify_completed_lists_container,
            self.spotify_completed_section,
            self.spotify_lists_container,
            self.verify_matches_button,
            self.wishlist_settings_button,
            self.wishlists_paned
        ) = self.widgets = ui.load(scope=self, path="wishlists.ui")

        self.window = window
        self.page = window.wishlists_page
        self.page.id = "wishlists"
        self.toolbar = window.wishlists_toolbar
        self.toolbar_start_content = window.wishlists_title
        self.toolbar_end_content = window.wishlists_end
        self.toolbar_default_widget = self.add_list_button

        self.current_list_name = None
        self.items_search_query = ""
        self._suppress_selection_sync = False

        if GTK_API_VERSION >= 4:
            window.wishlists_content.append(self.container)  # pylint: disable=no-member
        else:
            window.wishlists_content.add(self.container)     # pylint: disable=no-member

        # Unlike Downloads/Uploads, this page's content (namely the "Add List" button) should
        # always be reachable, even with zero lists, so always show it rather than the welcome
        # placeholder bound to its visibility
        window.wishlists_content.set_visible(True)

        # The sidebar splits lists into two categories -- Folders (manually
        # created, or populated by Watch Folder) and Spotify Playlists (each
        # backed by a watched Spotify playlist, see SpotifyWatch) -- each
        # with its own Active/Completed pair, identical in behavior to one
        # another. Active lists are shown in priority order (top = priority
        # 1, pinned lists always grouped ahead of unpinned ones), reorderable
        # by dragging a row or via Move Up/Down within its own pinned/
        # unpinned group — see DownloadLists.reorder_lists/move_list_up/down
        # — so they're intentionally left unsorted here rather than
        # alphabetically. Completed lists have no priority order to reorder,
        # so alphabetical is more useful there instead.
        def _active_columns():
            return {
                "pin": {
                    "column_type": "text",
                    "title": "",
                    "width": 20
                },
                "name": {
                    "column_type": "text",
                    "title": _("List"),
                    "width": 120,
                    "expand_column": True,
                    "iterator_key": True
                },
                "summary": {
                    "column_type": "text",
                    "title": _("Progress"),
                    "width": 0,
                    "tabular": True
                }
            }

        def _completed_columns():
            columns = _active_columns()
            columns["name"]["default_sort_type"] = "ascending"
            return columns

        self.folders_lists_view = TreeView(
            window, parent=self.folders_lists_container, select_row_callback=self.on_select_list_row,
            reorder_callback=self.on_folders_lists_view_reordered, multi_select=True,
            delete_accelerator_callback=self.on_remove_list, columns=_active_columns()
        )
        self.folders_completed_lists_view = TreeView(
            window, parent=self.folders_completed_lists_container, select_row_callback=self.on_select_list_row,
            multi_select=True, delete_accelerator_callback=self.on_remove_list, columns=_completed_columns()
        )
        self.spotify_lists_view = TreeView(
            window, parent=self.spotify_lists_container, select_row_callback=self.on_select_list_row,
            reorder_callback=self.on_spotify_lists_view_reordered, multi_select=True,
            delete_accelerator_callback=self.on_remove_list, columns=_active_columns()
        )
        self.spotify_completed_lists_view = TreeView(
            window, parent=self.spotify_completed_lists_container, select_row_callback=self.on_select_list_row,
            multi_select=True, delete_accelerator_callback=self.on_remove_list, columns=_completed_columns()
        )

        self.items_view = TreeView(
            window, parent=self.items_container, multi_select=True,
            delete_accelerator_callback=self.on_remove_item,
            columns={
                "position": {
                    "column_type": "number",
                    "title": _("#"),
                    "width": 40
                },
                "term": {
                    "column_type": "text",
                    "title": _("Search Term"),
                    "width": 160,
                    "expand_column": True,
                    "iterator_key": True,
                    "default_sort_type": "ascending"
                },
                "searched_term": {
                    "column_type": "text",
                    "title": _("Searched As"),
                    "width": 140
                },
                "status": {
                    "column_type": "text",
                    "title": _("Status"),
                    "width": 90,
                    "tooltip_callback": self.on_status_tooltip
                },
                "progress": {
                    "column_type": "progress",
                    "title": _("Progress"),
                    "width": 110
                },
                "downloaded_file": {
                    "column_type": "text",
                    "title": _("Downloaded File"),
                    "width": 200
                },
                "match": {
                    "column_type": "number",
                    "title": _("Match %"),
                    "width": 100
                },
                "quality": {
                    "column_type": "text",
                    "title": _("Quality"),
                    "width": 90,
                    "tooltip_callback": self.on_quality_tooltip
                },
                "length": {
                    "column_type": "text",
                    "title": _("Length"),
                    "width": 60
                }
            }
        )

        active_menu_items = (
            ("#" + self.PIN_LABEL, self.on_pin_unpin_list),
            ("#" + self.PAUSE_LABEL, self.on_pause_resume_list),
            ("#" + _("Move _Up"), self.on_move_list_up),
            ("#" + _("Move _Down"), self.on_move_list_down),
            ("#" + _("_Settings…"), self.on_list_settings),
            ("#" + _("Re_name…"), self.on_rename_list),
            ("", None),
            ("#" + _("_Remove"), self.on_remove_list)
        )
        # Completed lists have no priority order to reorder, but everything else
        # (pin to bring back to Active, settings, rename, remove) still applies
        completed_menu_items = (
            ("#" + self.PIN_LABEL, self.on_pin_unpin_list),
            ("#" + self.PAUSE_LABEL, self.on_pause_resume_list),
            ("#" + _("_Settings…"), self.on_list_settings),
            ("#" + _("Re_name…"), self.on_rename_list),
            ("", None),
            ("#" + _("_Remove"), self.on_remove_list)
        )

        self.folders_lists_popup_menu = PopupMenu(
            window.application, self.folders_lists_view.widget, self.on_popup_lists_menu)
        self.folders_lists_popup_menu.add_items(*active_menu_items)

        self.folders_completed_lists_popup_menu = PopupMenu(
            window.application, self.folders_completed_lists_view.widget, self.on_popup_lists_menu)
        self.folders_completed_lists_popup_menu.add_items(*completed_menu_items)

        self.spotify_lists_popup_menu = PopupMenu(
            window.application, self.spotify_lists_view.widget, self.on_popup_lists_menu)
        self.spotify_lists_popup_menu.add_items(*active_menu_items)

        self.spotify_completed_lists_popup_menu = PopupMenu(
            window.application, self.spotify_completed_lists_view.widget, self.on_popup_lists_menu)
        self.spotify_completed_lists_popup_menu.add_items(*completed_menu_items)

        # Filled in per item on right-click (see on_popup_items_menu) with the
        # closest files a Not Found item's searches did see
        self.similar_results_menu = PopupMenu(window.application)

        self.items_popup_menu = PopupMenu(window.application, self.items_view.widget, self.on_popup_items_menu)
        self.items_popup_menu.add_items(
            ("#" + _("_Search"), self.on_search_item),
            ("#" + _("Start _Next"), self.on_start_next_item),
            ("#" + _("_Reset"), self.on_reset_item),
            (">" + _("Search _Instead For"), self.similar_results_menu),
            ("", None),
            ("#" + _("Open in Spe_k"), self.on_open_in_spek),
            ("", None),
            ("#" + _("_Remove"), self.on_remove_item)
        )

        self._show_list(None)

        for event_name, callback in (
            ("add-download-list", self.on_add_download_list_event),
            ("download-list-completed", self.on_download_list_completed),
            ("remove-download-list", self.on_remove_download_list_event),
            ("rename-download-list", self.on_rename_download_list_event),
            ("reorder-download-lists", self.on_reorder_download_lists_event),
            ("start", self.on_start),
            ("update-download-list", self.on_update_download_list_event),
            ("update-download-list-item", self.on_update_download_list_item_event)
        ):
            events.connect(event_name, callback)

    def destroy(self):

        self.folders_lists_popup_menu.destroy()
        self.folders_completed_lists_popup_menu.destroy()
        self.spotify_lists_popup_menu.destroy()
        self.spotify_completed_lists_popup_menu.destroy()
        self.items_popup_menu.destroy()
        self.folders_lists_view.destroy()
        self.folders_completed_lists_view.destroy()
        self.spotify_lists_view.destroy()
        self.spotify_completed_lists_view.destroy()
        self.items_view.destroy()
        self.__dict__.clear()

    def on_focus(self, *_args):
        self.folders_lists_view.grab_focus()

    def on_items_search_changed(self, entry, *_args):

        self.items_search_query = entry.get_text().strip().lower()

        if self.current_list_name is not None:
            self._show_list(self.current_list_name, reset_search=False)

    def on_items_search_stop(self, entry, *_args):
        entry.set_text("")

    def _all_lists_views(self):
        return (
            self.folders_lists_view, self.folders_completed_lists_view,
            self.spotify_lists_view, self.spotify_completed_lists_view
        )

    def _selected_list_names(self):
        """Names of every currently selected list, in visual (top-to-bottom)
        order -- selection is exclusive across the four category/status
        views (see on_select_list_row), so at most one of them ever has a
        non-empty selection at a time."""

        for view in self._all_lists_views():
            if not view.is_selection_empty():
                return [view.get_row_value(iterator, "name") for iterator in view.get_selected_rows()]

        return []

    def on_select_list_row(self, list_view, iterator):

        if self._suppress_selection_sync:
            return

        if iterator is None:
            self._show_list(None)
            return

        # Only one row across all four category/status views can be selected
        # at a time; clear whichever ones didn't just receive this selection.
        # Suppress selection events while doing so, since unselecting fires
        # "changed" too, and would otherwise wipe out the selection we're in
        # the middle of setting
        self._suppress_selection_sync = True

        for other_view in self._all_lists_views():
            if other_view is not list_view:
                other_view.unselect_all_rows()

        self._suppress_selection_sync = False

        name = list_view.get_row_value(iterator, "name")
        self._show_list(name)

    def _active_list_names_by_category(self, is_spotify):
        return [
            name for name, download_list in core.download_lists.lists.items()
            if self._is_active_list(download_list) and self._is_spotify_list(name) == is_spotify
        ]

    def _apply_category_reorder(self, ordered_names, is_spotify):
        """The user dragged a row to a new position within one category's
        Active section — top of that category's list is its priority 1.
        DownloadLists.reorder_lists needs every current Active-section list
        across both categories at once (or it's a no-op), so the untouched
        category's existing relative order is appended unchanged — the
        interleaving between the two categories doesn't matter to it, only
        each one's own internal order (it regroups pinned-vs-unpinned itself
        regardless of how the two are interleaved here)."""

        other_names = self._active_list_names_by_category(not is_spotify)
        core.download_lists.reorder_lists(ordered_names + other_names)

    def on_folders_lists_view_reordered(self, ordered_names):
        self._apply_category_reorder(ordered_names, is_spotify=False)

    def on_spotify_lists_view_reordered(self, ordered_names):
        self._apply_category_reorder(ordered_names, is_spotify=True)

    def on_start(self):
        self._rebuild_lists_view()

    # Row helpers #

    def _is_spotify_list(self, name):
        """Whether name is currently backed by a watched Spotify playlist --
        determines which sidebar category (Folders vs. Spotify Playlists) it
        belongs in. Unwatching a playlist doesn't delete its list, just moves
        it back to Folders on the next row refresh (see
        SpotifyWatch.remove_watched_playlist)."""

        watched_names = {playlist["list_name"] for playlist in core.spotify_watch.get_watched_playlists()}
        return name in watched_names

    def _is_active_list(self, download_list):
        """Whether a list belongs in the Active section — everything except a
        completed, unpinned list. Pinned lists never move to Completed, even
        once every item is downloaded or not found."""

        return download_list.pinned or not download_list.is_complete

    def _list_view_for(self, download_list):

        is_active = self._is_active_list(download_list)

        if self._is_spotify_list(download_list.name):
            return self.spotify_lists_view if is_active else self.spotify_completed_lists_view

        return self.folders_lists_view if is_active else self.folders_completed_lists_view

    def _current_view_for_name(self, name):

        for view in self._all_lists_views():
            if name in view.iterators:
                return view

        return None

    def _update_category_visibility(self):
        """The Spotify Playlists category (heading, Active list, and its own
        Completed sub-section) only shows up once it actually has a list in
        it -- most users who never touch Spotify Watch see the sidebar
        exactly as before, with no "Folders" heading either, since a lone
        category doesn't need a label to distinguish it from anything."""

        has_spotify_lists = bool(self.spotify_lists_view.iterators or self.spotify_completed_lists_view.iterators)

        self.spotify_category_section.set_visible(has_spotify_lists)
        self.folders_heading.set_visible(has_spotify_lists)
        self.folders_completed_section.set_visible(bool(self.folders_completed_lists_view.iterators))
        self.spotify_completed_section.set_visible(bool(self.spotify_completed_lists_view.iterators))

    def _rebuild_lists_view(self):
        """Repopulate every Active/Completed x Folders/Spotify section from
        scratch, in current list priority order. Used at startup, and after a
        reorder that can't be expressed as a simple row move (e.g. Move Up/
        Down), or a category change (e.g. a playlist being unwatched)."""

        if core.download_lists is None:
            return

        selected_name = self.current_list_name

        self._suppress_selection_sync = True

        for view in self._all_lists_views():
            view.freeze()
            view.clear()

        # TreeView.add_row() always inserts at the top (for performance), so for
        # this unsorted, priority-ordered view, lists must be added lowest
        # priority first — each subsequent add then pushes it further down,
        # leaving the highest-priority list on top once every row is in
        for name in reversed(list(core.download_lists.lists)):
            self._add_list_row(name)

        for view in self._all_lists_views():
            view.unfreeze()

        self._suppress_selection_sync = False

        self._update_category_visibility()

        download_list = core.download_lists.lists.get(selected_name) if selected_name is not None else None

        if download_list is None:
            self._show_list(None)
            return

        target_view = self._list_view_for(download_list)
        iterator = target_view.iterators.get(selected_name)

        if iterator is not None:
            target_view.select_row(iterator)

    def _list_summary_text(self, download_list):

        total = len(download_list.items)
        # A song matched from the Lexicon library is just as "done" as one
        # that downloaded -- the user has it either way
        completed = download_list.num_completed + download_list.num_in_library
        not_found = download_list.num_not_found

        if not download_list.effective_auto_download:
            return _("%(completed)s/%(total)s (paused)") % {"completed": completed, "total": total}

        if not_found:
            return _("%(completed)s/%(total)s (%(missing)s not found)") % {
                "completed": completed, "total": total, "missing": not_found
            }

        return _("%(completed)s/%(total)s") % {"completed": completed, "total": total}

    PIN_GLYPH = "\U0001F4CC"  # 📌

    def _add_list_row(self, name, select=False):

        download_list = core.download_lists.lists.get(name)

        if download_list is None:
            return

        pin_glyph = self.PIN_GLYPH if download_list.pinned else ""
        target_view = self._list_view_for(download_list)
        target_view.add_row(
            [pin_glyph, name, self._list_summary_text(download_list)], select_row=select)
        self._update_category_visibility()

    def _update_list_row(self, name):

        download_list = core.download_lists.lists.get(name)

        if download_list is None:
            return

        current_view = self._current_view_for_name(name)

        if current_view is None:
            return

        target_view = self._list_view_for(download_list)

        if current_view is not target_view:
            if target_view in (self.folders_lists_view, self.spotify_lists_view):
                # Moving back to Active (or switching category): a plain add
                # would drop it to the bottom instead of its actual priority
                # position, so rebuild instead
                self._rebuild_lists_view()
                return

            # Moving to Completed: alphabetically sorted, so appending is fine
            was_selected = (self.current_list_name == name)
            current_view.remove_row(current_view.iterators[name])
            target_view.add_row(
                [self.PIN_GLYPH if download_list.pinned else "", name, self._list_summary_text(download_list)],
                select_row=was_selected
            )
            self._update_category_visibility()
            return

        iterator = current_view.iterators.get(name)

        if iterator is None:
            return

        current_view.set_row_values(
            iterator,
            ["pin", "summary"],
            [self.PIN_GLYPH if download_list.pinned else "", self._list_summary_text(download_list)]
        )

    def _item_row_values(self, item, position):

        return [
            str(position),
            item.term,
            item.searched_term or "",
            self.STATUS_LABELS.get(item.status, item.status),
            item.download_percent,
            item.download_filename,
            item.h_match_percentage,
            item.h_quality,
            item.h_length
        ]

    def _matches_items_search(self, item):
        """Whether an item matches the current items-list search query — checked
        against the search term, what it was actually searched as, its status,
        and the downloaded filename, so e.g. typing an artist name, "not found",
        or part of a filename all work."""

        query = self.items_search_query

        if not query:
            return True

        haystack = " ".join((
            item.term,
            item.searched_term or "",
            self.STATUS_LABELS.get(item.status, item.status),
            item.download_filename
        )).lower()

        return query in haystack

    def _add_item_row(self, item, position):

        if not self._matches_items_search(item):
            return

        self.items_view.add_row(self._item_row_values(item, position), select_row=False)

    def _update_item_row(self, name, term):

        if name != self.current_list_name:
            return

        iterator = self.items_view.iterators.get(term)

        if iterator is None:
            return

        download_list = core.download_lists.lists.get(name)
        item = download_list.items.get(term) if download_list is not None else None

        if item is None:
            return

        self.items_view.set_row_values(
            iterator,
            ["searched_term", "status", "progress", "downloaded_file", "match", "quality", "length"],
            [item.searched_term or "", self.STATUS_LABELS.get(item.status, item.status),
             item.download_percent, item.download_filename, item.h_match_percentage,
             item.h_quality, item.h_length]
        )

    def _pause_resume_label(self, download_list):
        is_paused = download_list is not None and not download_list.effective_auto_download
        return self.RESUME_LABEL if is_paused else self.PAUSE_LABEL

    def _update_pause_resume_button(self, download_list):
        """Reflect whether the given list (None if none selected) is currently paused
        in the toolbar button. The context menu item is updated separately, right
        before it's shown (see on_popup_lists_menu), since its label can only be
        changed once the popup model has been built at least once."""

        self.pause_resume_button.set_sensitive(download_list is not None)
        self.pause_resume_button.set_label(self._pause_resume_label(download_list))
        self.pause_resume_button.set_use_underline(True)

    def _show_list(self, name, reset_search=True):
        """reset_search is False when re-showing the same list after a background
        update (e.g. an item's status changed), so an in-progress search isn't
        wiped out from under the user; switching to a different list always
        starts that list's view unfiltered."""

        is_new_list = reset_search and name != self.current_list_name

        self.current_list_name = name
        has_list = name is not None
        download_list = core.download_lists.lists.get(name) if has_list else None

        if is_new_list:
            self.items_search_query = ""
            self.items_search_entry.set_text("")

        self.items_search_entry.set_sensitive(has_list)
        self.add_songs_button.set_sensitive(has_list)
        self.list_settings_button.set_sensitive(has_list)
        self.verify_matches_button.set_sensitive(has_list)
        self.export_summary_button.set_sensitive(has_list)
        self.current_list_label.set_text(name if has_list else _("No list selected"))
        self._update_pause_resume_button(download_list)

        self.items_view.freeze()
        self.items_view.clear()

        if download_list is not None:
            # Position reflects the order items were added to the list (dicts keep
            # insertion order), independent of the current search filter or any
            # column sort applied in the view — "3" always means the 3rd song added
            for position, item in enumerate(download_list.items.values(), start=1):
                self._add_item_row(item, position)

        self.items_view.unfreeze()

    # Core Events #

    def on_add_download_list_event(self, name):
        # A new list is always lowest priority (the very back of self.lists), but
        # a plain add_row() would put its row at the top instead (see
        # _rebuild_lists_view) — rebuild so it lands in its correct position
        self.current_list_name = name
        self._rebuild_lists_view()

    def on_remove_download_list_event(self, name):

        for view in self._all_lists_views():
            iterator = view.iterators.get(name)

            if iterator is not None:
                view.remove_row(iterator)
                break

        self._update_category_visibility()

        if self.current_list_name == name:
            self._show_list(None)

    def on_rename_download_list_event(self, old_name, new_name):

        if self.current_list_name == old_name:
            self.current_list_name = new_name

        # A plain remove+re-add would drop the row to the back of its section
        # instead of keeping its priority position, so rebuild instead
        self._rebuild_lists_view()

    def on_reorder_download_lists_event(self):
        self._rebuild_lists_view()

    def on_update_download_list_event(self, name):

        self._update_list_row(name)

        if name == self.current_list_name:
            self._show_list(name)

    def on_update_download_list_item_event(self, name, term):
        self._update_list_row(name)
        self._update_item_row(name, term)

    def on_download_list_completed(self, _name):

        if self.window.current_page_id != self.page.id:
            self.window.notebook.request_tab_changed(self.page, is_important=True)

    # Callbacks #

    def on_add_list_response(self, name, already_created=False):

        if already_created:
            # A Spotify-playlist-backed list creates and populates itself
            # (SpotifyWatch.add_watched_playlist) -- nothing left to do here
            return

        download_list = core.download_lists.add_list(name)

        if download_list is None:
            return

        ListSettingsDialog(self.window.application, download_list, self.on_list_settings_saved).present()

    def on_add_list(self, *_args):
        AddListDialog(self.window.application, self.on_add_list_response).present()

    def on_add_songs_response(self, dialog, _response_id, list_name):

        terms = dialog.get_entry_value().split("\n")
        core.download_lists.add_list_items(list_name, terms)

    def on_add_songs(self, *_args):

        if self.current_list_name is None:
            return

        EntryDialog(
            application=self.window.application,
            title=_("Add Songs"),
            message=_('Enter a list of songs to add to "%s", one per line:') % self.current_list_name,
            action_button_label=_("_Add"),
            multiline=True,
            callback=self.on_add_songs_response,
            callback_data=self.current_list_name
        ).present()

    def on_list_settings_saved(self, name, settings):
        core.download_lists.update_list_settings(name, **settings)

    def on_wishlist_settings_saved(self, watch_enabled, watch_folder_path, quality, prefer_longer,
                                   prefer_lossless, preferred_keywords, fuzzy_match_threshold,
                                   auto_download, use_name_subfolder, apply_to_existing_lists,
                                   stall_timeout, min_speed_kib, max_concurrent, quality_check,
                                   spotify_ignore_radio_edit):
        core.download_lists.update_watch_folder_settings(watch_enabled, watch_folder_path)
        core.download_lists.update_quality_check_setting(quality_check)
        core.download_lists.update_wishlist_default_settings(
            quality, prefer_longer, prefer_lossless, preferred_keywords, fuzzy_match_threshold,
            auto_download, use_name_subfolder, apply_to_existing_lists=apply_to_existing_lists)
        core.download_lists.update_stall_settings(stall_timeout, min_speed_kib)
        core.download_lists.update_max_concurrent_downloads(max_concurrent)
        core.spotify_watch.update_ignore_radio_edit(spotify_ignore_radio_edit)

    def on_wishlist_settings(self, *_args):
        WishlistSettingsDialog(self.window.application, self.on_wishlist_settings_saved).present()

    def on_retry_not_found(self, *_args):
        """Search again for every Not Found song across all lists, after
        confirming how many that is."""

        num_not_found = sum(
            download_list.num_not_found for download_list in core.download_lists.lists.values())

        if not num_not_found:
            log.add(_("No songs are marked Not Found"))
            return

        OptionDialog(
            application=self.window.application,
            title=_("Retry Not Found Songs?"),
            message=_(
                "Search again for the %(num)s song(s) marked Not Found across all lists? Each one is "
                "queued as if it had just been added, and lists that are paused stay paused."
            ) % {"num": num_not_found},
            buttons=[
                ("cancel", _("_Cancel")),
                ("ok", _("_Retry"))
            ],
            callback=self.on_retry_not_found_response
        ).present()

    def on_retry_not_found_response(self, _dialog, response_id, _data):

        if response_id != "ok":
            return

        num_reset = core.download_lists.reset_not_found_items()
        log.add(_("Searching again for %(num)s song(s) previously marked Not Found"), {"num": num_reset})

    def on_popup_lists_menu(self, menu, _widget):
        """Right-clicking a row selects it first, so by the time this fires,
        the popup's target list is whatever's currently selected — in either
        the Active or Completed section."""

        download_list = core.download_lists.lists.get(self.current_list_name)

        menu.update_item_label(self.PAUSE_LABEL, self._pause_resume_label(download_list))
        menu.update_item_label(
            self.PIN_LABEL,
            self.UNPIN_LABEL if download_list is not None and download_list.pinned else self.PIN_LABEL
        )

    def on_pin_unpin_list(self, *_args):
        """Pins/unpins every selected list at once. Mixed selections (some
        pinned, some not) all follow the first selected list's own state --
        e.g. selecting one pinned and two unpinned lists and pinning
        unpins all three, matching what the popup menu's Pin/Unpin label
        (set from that same first list, see on_popup_lists_menu) promises."""

        names = self._selected_list_names()

        if not names:
            return

        first_list = core.download_lists.lists.get(names[0])

        if first_list is None:
            return

        target_pinned = not first_list.pinned

        for name in names:
            download_list = core.download_lists.lists.get(name)

            if download_list is not None and download_list.pinned != target_pinned:
                core.download_lists.set_list_pinned(name, target_pinned)

    def _move_single_list_priority(self, name, direction):
        """Swap one list with its neighbor within its own category (Folders
        or Spotify Playlists) -- mirrors DownloadLists._swap_list_priority's
        own pinned/unpinned tier restriction, but scoped further to the
        category, since the two categories are now displayed in fully
        separate sidebar sections: swapping against a list in the other
        category wouldn't move anything visibly, even though it would
        still change the underlying global priority order DownloadLists.
        move_list_up/down operates on directly."""

        download_list = core.download_lists.lists.get(name)

        if download_list is None:
            return

        is_spotify = self._is_spotify_list(name)
        category_names = self._active_list_names_by_category(is_spotify)
        same_tier = [
            list_name for list_name in category_names
            if core.download_lists.lists[list_name].pinned == download_list.pinned
        ]

        index = same_tier.index(name)
        swap_index = index + direction

        if swap_index < 0 or swap_index >= len(same_tier):
            return

        swap_name = same_tier[swap_index]
        i, j = category_names.index(name), category_names.index(swap_name)
        category_names[i], category_names[j] = category_names[j], category_names[i]

        self._apply_category_reorder(category_names, is_spotify)

    def _move_list_priority(self, direction):
        """Moves every selected list by one position, as a block. Processed
        top-to-bottom for "up" and bottom-to-top for "down" -- the standard
        multi-select reorder order, so an earlier move in the pass never
        gets bumped right back by a later one cascading past it (each
        individual swap re-reads the list order fresh, so this stays
        correct even as earlier moves in the same pass change it)."""

        names = self._selected_list_names()

        if not names:
            return

        ordered_names = names if direction < 0 else list(reversed(names))

        for name in ordered_names:
            self._move_single_list_priority(name, direction)

    def on_move_list_up(self, *_args):
        self._move_list_priority(-1)

    def on_move_list_down(self, *_args):
        self._move_list_priority(1)

    def on_pause_resume_list(self, *_args):
        """Pauses/resumes every selected list at once, all following the
        first selected list's own state -- same "first list decides" rule
        as on_pin_unpin_list, matching the popup menu's Pause/Resume label."""

        names = self._selected_list_names()

        if not names:
            return

        first_list = core.download_lists.lists.get(names[0])

        if first_list is None:
            return

        pause = first_list.effective_auto_download

        for name in names:
            download_list = core.download_lists.lists.get(name)

            if download_list is None:
                continue

            if pause:
                if download_list.effective_auto_download:
                    core.download_lists.pause_list(name)
            elif not download_list.effective_auto_download:
                core.download_lists.resume_list(name)

    def on_list_settings(self, *_args):

        if self.current_list_name is None:
            return

        download_list = core.download_lists.lists.get(self.current_list_name)

        if download_list is None:
            return

        ListSettingsDialog(self.window.application, download_list, self.on_list_settings_saved).present()

    def on_rename_list_response(self, dialog, _response_id, old_name):

        new_name = dialog.get_entry_value().strip()

        if not new_name or new_name == old_name:
            return

        core.download_lists.rename_list(old_name, new_name)

    def on_rename_list(self, *_args):

        old_name = self.current_list_name

        if old_name is None:
            return

        EntryDialog(
            application=self.window.application,
            title=_("Rename List"),
            message=_("Enter a new name:"),
            default=old_name,
            action_button_label=_("_Rename"),
            callback=self.on_rename_list_response,
            callback_data=old_name
        ).present()

    def on_remove_list_response(self, _dialog, _response_id, names):
        for name in names:
            core.download_lists.remove_list(name)

    def on_remove_list(self, *_args):

        names = self._selected_list_names()

        if not names:
            return

        if len(names) == 1:
            message = _('Do you want to remove "%s"? Files already downloaded are not deleted, '
                        "only the list itself and its history.") % names[0]
        else:
            message = _(
                "Do you want to remove these %(count)s lists? Files already downloaded are not "
                "deleted, only the lists themselves and their history.\n\n%(names)s"
            ) % {"count": len(names), "names": "\n".join(names)}

        OptionDialog(
            application=self.window.application,
            title=_("Remove List?") if len(names) == 1 else _("Remove Lists?"),
            message=message,
            buttons=[
                ("cancel", _("_Cancel")),
                ("ok", _("Remove"))
            ],
            destructive_response_id="ok",
            callback=self.on_remove_list_response,
            callback_data=names
        ).present()

    def _selected_item(self):

        if self.current_list_name is None:
            return None

        iterator = next(self.items_view.get_selected_rows(), None)

        if iterator is None:
            return None

        term = self.items_view.get_row_value(iterator, "term")
        download_list = core.download_lists.lists.get(self.current_list_name)
        return download_list.items.get(term) if download_list is not None else None

    @staticmethod
    def _suggestion_label(suggestion):
        # A menu label's "_" marks a mnemonic; a literal one is written "__"
        filename = suggestion["filename"].replace("\\", "/").rsplit("/", 1)[-1].replace("_", "__")
        return _("%(file)s  (%(match)s%% match, searched as \u201c%(term)s\u201d)") % {
            "file": filename, "match": suggestion["match"], "term": suggestion["searched_term"]}

    def on_popup_items_menu(self, _menu, _widget):
        """Right-clicking a row selects it first, so the target item is the
        selected one. Rebuild the "Search Instead For" submenu from its
        suggestions: the closest files its searches saw without a good
        enough match -- typically what a mistyped term actually meant."""

        item = self._selected_item()
        suggestions = item.suggestions if item is not None else []

        self.similar_results_menu.clear()

        if not suggestions:
            label = _("(nothing similar was seen)")
            self.similar_results_menu.add_items(("#" + label, None))
            self.similar_results_menu.update_model()
            self.similar_results_menu.actions[label].set_enabled(False)
            return

        for suggestion in suggestions:
            self.similar_results_menu.add_items(
                ("#" + self._suggestion_label(suggestion), self.on_search_instead_for, item.term, suggestion))

        self.similar_results_menu.update_model()

    def on_search_instead_for(self, _action, _parameter, term, suggestion):
        """Replace the item's term with one made from the suggested file's
        name and search for it afresh -- in place, keeping its position."""

        new_term = core.download_lists.suggestion_term(suggestion["filename"])
        log.add(_('Searching for "%(new)s" instead of "%(old)s"'), {"new": new_term, "old": term})
        core.download_lists.retarget_list_item(self.current_list_name, term, new_term)

    def on_quality_tooltip(self, treeview, iterator):
        """The claimed quality next to what the spectrum check measured."""

        download_list = core.download_lists.lists.get(self.current_list_name)
        term = treeview.get_row_value(iterator, "term")
        item = download_list.items.get(term) if download_list is not None else None

        if item is None or not item.h_quality:
            return None

        lines = [_("Claimed: %(quality)s") % {"quality": item.h_quality}]

        if item.quality_cutoff_hz is not None:
            report = audioquality.QualityReport(item.quality_cutoff_hz)
            lines.append(_("Measured: %(result)s") % {"result": report.describe()})

        if item.num_quality_rejections:
            lines.append(_("%(num)s earlier download(s) failed the quality check") % {
                "num": item.num_quality_rejections})

        return "\n".join(lines)

    def on_open_in_spek(self, *_args):
        """Show the selected item's downloaded file in Spek, for a look at
        its spectrogram by eye."""

        item = self._selected_item()
        file_path = core.download_lists._item_file_path(self.current_list_name, item) if item else None

        if file_path is None:
            log.add(_("This song has no finished download to open"))
            return

        if sys.platform == "darwin" and os.path.exists("/Applications/Spek.app"):
            subprocess.Popen(["open", "-a", "Spek", file_path])  # pylint: disable=consider-using-with
            return

        spek_path = shutil.which("spek")

        if spek_path is not None:
            subprocess.Popen([spek_path, file_path])  # pylint: disable=consider-using-with
            return

        open_file_path(file_path)

    def on_status_tooltip(self, treeview, iterator):
        """A Not Found row's status tooltip lists what its searches did see,
        so a mistyped term stands out from a track nobody shares."""

        download_list = core.download_lists.lists.get(self.current_list_name)
        term = treeview.get_row_value(iterator, "term")
        item = download_list.items.get(term) if download_list is not None else None

        if item is None:
            return None

        if item.status != DownloadListItemStatus.NOT_FOUND:
            return self.STATUS_LABELS.get(item.status, item.status)

        if not item.suggestions:
            return _("Not Found\n\nNo file resembling this term was seen in any of its searches.")

        lines = [
            _("%(file)s: %(match)s%% match, searched as \u201c%(term)s\u201d") % {
                "file": suggestion["filename"].replace("\\", "/").rsplit("/", 1)[-1],
                "match": suggestion["match"], "term": suggestion["searched_term"]}
            for suggestion in item.suggestions
        ]
        return _("Not Found\n\nSimilar files seen while searching:\n%(files)s\n\n"
                 "Right-click \u2192 Search Instead For to use one of them.") % {"files": "\n".join(lines)}

    def on_search_item(self, *_args):
        """Run a fresh Search Files search for the (first) selected item's
        search term, and switch to that tab -- the same lookup this item's
        own automatic search is already trying, just user-driven and visible."""

        iterator = next(self.items_view.get_selected_rows(), None)

        if iterator is None:
            return

        term = self.items_view.get_row_value(iterator, "term")
        core.search.do_search(term, mode="global")

    def on_start_next_item(self, *_args):

        if self.current_list_name is None:
            return

        # Selected rows are given in view (visual/sort) order, so working through
        # them in reverse and always inserting at the front leaves the first
        # selected row as the very next one dispatched
        for iterator in reversed(list(self.items_view.get_selected_rows())):
            term = self.items_view.get_row_value(iterator, "term")
            core.download_lists.start_item_next(self.current_list_name, term)

    def on_reset_item(self, *_args):

        if self.current_list_name is None:
            return

        for iterator in list(self.items_view.get_selected_rows()):
            term = self.items_view.get_row_value(iterator, "term")
            core.download_lists.reset_list_item(self.current_list_name, term)

    def on_remove_item(self, *_args):

        if self.current_list_name is None:
            return True

        for iterator in list(self.items_view.get_selected_rows()):
            term = self.items_view.get_row_value(iterator, "term")
            core.download_lists.remove_list_item(self.current_list_name, term)

        return True

    def on_verify_matches(self, *_args):

        if self.current_list_name is None:
            return

        download_list = core.download_lists.lists.get(self.current_list_name)

        if download_list is None:
            return

        VerifyMatchesDialog(self.window.application, download_list).present()

    def on_export_summary_selected(self, selected, list_name):

        file_path = next(iter(selected), None)

        if not file_path:
            return

        core.download_lists.export_summary_csv(list_name, file_path)

    def on_export_summary(self, *_args):

        if self.current_list_name is None:
            return

        safe_name = self.current_list_name.replace("/", "-").replace("\\", "-")

        FileChooserSave(
            application=self.window.application,
            callback=self.on_export_summary_selected,
            callback_data=self.current_list_name,
            initial_folder=config.sections["transfers"]["downloaddir"],
            initial_file=f"{safe_name}.csv"
        ).present()
