# SPDX-FileCopyrightText: 2026 Nicotine+ Contributors
# SPDX-License-Identifier: GPL-3.0-or-later

from gi.repository import Gtk

from pynicotine.config import config
from pynicotine.core import core
from pynicotine.downloadlists import DownloadLists
from pynicotine.events import events
from pynicotine.gtkgui.application import GTK_API_VERSION
from pynicotine.gtkgui.widgets import ui
from pynicotine.gtkgui.widgets.combobox import ComboBox
from pynicotine.gtkgui.widgets.dialogs import Dialog
from pynicotine.gtkgui.widgets.dialogs import EntryDialog
from pynicotine.gtkgui.widgets.dialogs import OptionDialog
from pynicotine.gtkgui.widgets.filechooser import FileChooserButton
from pynicotine.gtkgui.widgets.filechooser import FileChooserSave
from pynicotine.gtkgui.widgets.popupmenu import PopupMenu
from pynicotine.gtkgui.widgets.theme import add_css_class
from pynicotine.gtkgui.widgets.treeview import TreeView


class ListSettingsDialog(Dialog):
    """Per-list settings: destination folder, match quality/length/accuracy
    preferences, and whether the list actively downloads."""

    QUALITY_ITEMS = (
        (_("Any"), "any"),
        (_("Good (192 kbps+)"), "good"),
        (_("High (320 kbps+)"), "high"),
        (_("Lossless only (FLAC/WAV)"), "lossless")
    )

    def __init__(self, application, download_list, callback):

        self.download_list = download_list
        self.callback = callback

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
        self._add_quality_option()
        self._add_prefer_longer_option()
        self._add_fuzzy_option()
        self._add_auto_download_option()

    def destroy(self):
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

    def _add_folder_option(self):

        label = Gtk.Label(
            label=_("Download folder for this list:"), wrap=True, xalign=0, visible=True)
        self._append(label)

        row = Gtk.Box(visible=True)
        self._append(row)

        self.folder_chooser = FileChooserButton(row, self.application, chooser_type="folder")

        if self.download_list.download_folder_path:
            self.folder_chooser.set_path(self.download_list.download_folder_path)

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
        self.quality_combobox.set_selected_id(self.download_list.quality)
        label.set_mnemonic_widget(self.quality_combobox.widget)

    def _add_prefer_longer_option(self):

        self.prefer_longer_switch = Gtk.Switch(
            active=self.download_list.prefer_longer, valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(_("Prefer longer/extended versions of a song"), self.prefer_longer_switch)

    def _add_fuzzy_option(self):

        self.fuzzy_spinner = Gtk.SpinButton(
            adjustment=Gtk.Adjustment(
                value=self.download_list.fuzzy_match_threshold, lower=0, upper=100,
                step_increment=5, page_increment=10, page_size=0
            ),
            climb_rate=1, digits=0, valign=Gtk.Align.CENTER, visible=True
        )
        self._labeled_row(_("Minimum match accuracy (%):"), self.fuzzy_spinner)

    def _add_auto_download_option(self):

        self.auto_download_switch = Gtk.Switch(
            active=self.download_list.auto_download, valign=Gtk.Align.CENTER, visible=True)
        self._labeled_row(
            _("Automatically download matches"), self.auto_download_switch,
            tooltip_text=_("When off, this list is paused: nothing is searched for or downloaded "
                            "until you turn it back on.")
        )

    def on_cancel(self, *_args):
        self.close()

    def on_save(self, *_args):

        settings = {
            "download_folder_path": self.folder_chooser.get_path(),
            "quality": self.quality_combobox.get_selected_id(),
            "prefer_longer": self.prefer_longer_switch.get_active(),
            "fuzzy_match_threshold": self.fuzzy_spinner.get_value_as_int(),
            "auto_download": self.auto_download_switch.get_active()
        }

        self.callback(self.download_list.name, settings)
        self.close()


class WishlistSettingsDialog(Dialog):
    """Global wishlist settings: the watch folder that's polled for song
    list files exported by other applications."""

    def __init__(self, application, callback):

        self.callback = callback

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
            title=_("Wishlist Settings"),
            width=420,
            height=-1
        )

        self._add_watch_folder_option()

    def destroy(self):
        self.__dict__.clear()

    def _append(self, widget):
        if GTK_API_VERSION >= 4:
            self.primary_container.append(widget)  # pylint: disable=no-member
        else:
            self.primary_container.add(widget)      # pylint: disable=no-member

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

        folder_row = Gtk.Box(visible=True)
        self._append(folder_row)

        self.folder_chooser = FileChooserButton(folder_row, self.application, chooser_type="folder")

        if folder_path:
            self.folder_chooser.set_path(folder_path)

    def on_cancel(self, *_args):
        self.close()

    def on_save(self, *_args):

        self.callback(self.watch_enabled_switch.get_active(), self.folder_chooser.get_path())
        self.close()


class Wishlists:

    STATUS_LABELS = DownloadLists.STATUS_LABELS

    def __init__(self, window):

        (
            self.add_list_button,
            self.add_songs_button,
            self.container,
            self.current_list_label,
            self.export_summary_button,
            self.items_container,
            self.items_pane,
            self.list_settings_button,
            self.lists_container,
            self.lists_pane,
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

        if GTK_API_VERSION >= 4:
            window.wishlists_content.append(self.container)  # pylint: disable=no-member
        else:
            window.wishlists_content.add(self.container)     # pylint: disable=no-member

        # Unlike Downloads/Uploads, this page's content (namely the "Add List" button) should
        # always be reachable, even with zero lists, so always show it rather than the welcome
        # placeholder bound to its visibility
        window.wishlists_content.set_visible(True)

        self.lists_view = TreeView(
            window, parent=self.lists_container, select_row_callback=self.on_select_list_row,
            columns={
                "name": {
                    "column_type": "text",
                    "title": _("List"),
                    "width": 120,
                    "expand_column": True,
                    "iterator_key": True,
                    "default_sort_type": "ascending"
                },
                "summary": {
                    "column_type": "text",
                    "title": _("Progress"),
                    "width": 0,
                    "tabular": True
                }
            }
        )

        self.items_view = TreeView(
            window, parent=self.items_container, multi_select=True,
            delete_accelerator_callback=self.on_remove_item,
            columns={
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
                    "width": 90
                },
                "downloaded_file": {
                    "column_type": "text",
                    "title": _("Downloaded File"),
                    "width": 160
                },
                "quality": {
                    "column_type": "text",
                    "title": _("Quality"),
                    "width": 90
                },
                "length": {
                    "column_type": "text",
                    "title": _("Length"),
                    "width": 60
                }
            }
        )

        self.lists_popup_menu = PopupMenu(window.application, self.lists_view.widget)
        self.lists_popup_menu.add_items(
            ("#" + _("_Settings…"), self.on_list_settings),
            ("#" + _("Re_name…"), self.on_rename_list),
            ("", None),
            ("#" + _("_Remove"), self.on_remove_list)
        )

        self.items_popup_menu = PopupMenu(window.application, self.items_view.widget)
        self.items_popup_menu.add_items(
            ("#" + _("_Reset"), self.on_reset_item),
            ("", None),
            ("#" + _("_Remove"), self.on_remove_item)
        )

        self._show_list(None)

        for event_name, callback in (
            ("add-download-list", self.on_add_download_list_event),
            ("download-list-completed", self.on_download_list_completed),
            ("remove-download-list", self.on_remove_download_list_event),
            ("rename-download-list", self.on_rename_download_list_event),
            ("start", self.on_start),
            ("update-download-list", self.on_update_download_list_event),
            ("update-download-list-item", self.on_update_download_list_item_event)
        ):
            events.connect(event_name, callback)

    def destroy(self):

        self.lists_popup_menu.destroy()
        self.items_popup_menu.destroy()
        self.lists_view.destroy()
        self.items_view.destroy()
        self.__dict__.clear()

    def on_focus(self, *_args):
        self.lists_view.grab_focus()

    def on_select_list_row(self, list_view, iterator):

        if iterator is None:
            self._show_list(None)
            return

        name = list_view.get_row_value(iterator, "name")
        self._show_list(name)

    def on_start(self):

        if core.download_lists is None:
            return

        self.lists_view.freeze()

        for name in core.download_lists.lists:
            self._add_list_row(name)

        self.lists_view.unfreeze()

    # Row helpers #

    def _list_summary_text(self, download_list):

        total = len(download_list.items)
        completed = download_list.num_completed
        not_found = download_list.num_not_found

        if not download_list.auto_download:
            return _("%(completed)s/%(total)s (paused)") % {"completed": completed, "total": total}

        if not_found:
            return _("%(completed)s/%(total)s (%(missing)s not found)") % {
                "completed": completed, "total": total, "missing": not_found
            }

        return _("%(completed)s/%(total)s") % {"completed": completed, "total": total}

    def _add_list_row(self, name, select=False):

        download_list = core.download_lists.lists.get(name)

        if download_list is None:
            return

        self.lists_view.add_row([name, self._list_summary_text(download_list)], select_row=select)

    def _update_list_row(self, name):

        iterator = self.lists_view.iterators.get(name)

        if iterator is None:
            return

        download_list = core.download_lists.lists.get(name)

        if download_list is None:
            return

        self.lists_view.set_row_value(iterator, "summary", self._list_summary_text(download_list))

    def _item_row_values(self, item):

        return [
            item.term,
            item.searched_term or "",
            self.STATUS_LABELS.get(item.status, item.status),
            item.download_filename,
            item.h_quality,
            item.h_length
        ]

    def _add_item_row(self, item):
        self.items_view.add_row(self._item_row_values(item), select_row=False)

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
            ["searched_term", "status", "downloaded_file", "quality", "length"],
            [item.searched_term or "", self.STATUS_LABELS.get(item.status, item.status),
             item.download_filename, item.h_quality, item.h_length]
        )

    def _show_list(self, name):

        self.current_list_name = name
        has_list = name is not None

        self.add_songs_button.set_sensitive(has_list)
        self.list_settings_button.set_sensitive(has_list)
        self.export_summary_button.set_sensitive(has_list)
        self.current_list_label.set_text(name if has_list else _("No list selected"))

        self.items_view.freeze()
        self.items_view.clear()

        if has_list:
            download_list = core.download_lists.lists.get(name)

            if download_list is not None:
                for item in download_list.items.values():
                    self._add_item_row(item)

        self.items_view.unfreeze()

    # Core Events #

    def on_add_download_list_event(self, name):
        self._add_list_row(name, select=True)

    def on_remove_download_list_event(self, name):

        iterator = self.lists_view.iterators.get(name)

        if iterator is not None:
            self.lists_view.remove_row(iterator)

        if self.current_list_name == name:
            self._show_list(None)

    def on_rename_download_list_event(self, old_name, new_name):

        iterator = self.lists_view.iterators.get(old_name)

        if iterator is not None:
            self.lists_view.remove_row(iterator)

        was_current = (self.current_list_name == old_name)
        self._add_list_row(new_name, select=was_current)

        if was_current:
            self.current_list_name = new_name

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

    def on_add_list_response(self, dialog, _response_id, _data):

        name = dialog.get_entry_value().strip()

        if not name:
            return

        download_list = core.download_lists.add_list(name)

        if download_list is None:
            return

        ListSettingsDialog(self.window.application, download_list, self.on_list_settings_saved).present()

    def on_add_list(self, *_args):

        EntryDialog(
            application=self.window.application,
            title=_("Add List"),
            message=_("Enter a name for the new download list:"),
            action_button_label=_("_Add"),
            callback=self.on_add_list_response
        ).present()

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

    def on_wishlist_settings_saved(self, enabled, folder_path):
        core.download_lists.update_watch_folder_settings(enabled, folder_path)

    def on_wishlist_settings(self, *_args):
        WishlistSettingsDialog(self.window.application, self.on_wishlist_settings_saved).present()

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

        for iterator in self.lists_view.get_selected_rows():
            old_name = self.lists_view.get_row_value(iterator, "name")

            EntryDialog(
                application=self.window.application,
                title=_("Rename List"),
                message=_("Enter a new name:"),
                default=old_name,
                action_button_label=_("_Rename"),
                callback=self.on_rename_list_response,
                callback_data=old_name
            ).present()
            return

    def on_remove_list_response(self, _dialog, _response_id, name):
        core.download_lists.remove_list(name)

    def on_remove_list(self, *_args):

        for iterator in self.lists_view.get_selected_rows():
            name = self.lists_view.get_row_value(iterator, "name")

            OptionDialog(
                application=self.window.application,
                title=_("Remove List?"),
                message=_('Do you want to remove "%s"? Files already downloaded are not deleted, '
                          "only the list itself and its history.") % name,
                buttons=[
                    ("cancel", _("_Cancel")),
                    ("ok", _("Remove"))
                ],
                destructive_response_id="ok",
                callback=self.on_remove_list_response,
                callback_data=name
            ).present()
            return

    def on_reset_item(self, *_args):

        if self.current_list_name is None:
            return

        for iterator in self.items_view.get_selected_rows():
            term = self.items_view.get_row_value(iterator, "term")
            core.download_lists.reset_list_item(self.current_list_name, term)

    def on_remove_item(self, *_args):

        if self.current_list_name is None:
            return True

        for iterator in list(self.items_view.get_selected_rows()):
            term = self.items_view.get_row_value(iterator, "term")
            core.download_lists.remove_list_item(self.current_list_name, term)

        return True

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
