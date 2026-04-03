from gi.repository import Adw, Gio, GLib, Gdk, Gtk, Pango

from detail_page import DetailPage
from engine_support import available_engines, engine_icon_name
from focus_guard import schedule_neutral_focus, should_prevent_input_autofocus
from i18n import t
from logger_setup import get_logger
from ui_flow_state import next_search_toggle_state
from ui_icons import create_image_from_ref
from app_identity import APP_VERSION
ENGINES = available_engines()
from app_models import Entry
from desktop_entries import delete_managed_entry_artifacts
from webapp_constants import ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY

LOG = get_logger(__name__)


class MainWindowOverviewMixin:
    def _header_detail_delete_target(self):
        current_detail = self._overview_detail_visible_child()
        if isinstance(current_detail, DetailPage) and self._is_overview_child_visible(current_detail):
            return current_detail
        return None

    def _set_header_detail_delete_visible(self, visible=None):
        button = getattr(self, 'delete_button', None)
        if button is None:
            return
        if visible is None:
            visible = self._header_detail_delete_target() is not None
        button.set_visible(bool(visible))

    def on_header_delete_clicked(self, button):
        detail_page = self._header_detail_delete_target()
        if detail_page is None:
            return
        detail_page.on_delete_clicked(button)

    def _build_list_title_widget(self):
        label = Gtk.Label(xalign=0)
        label.set_text(t('app_title'))
        label.add_css_class('title-4')
        label.add_css_class('overview-title')
        label.set_halign(Gtk.Align.CENTER)
        label.set_valign(Gtk.Align.CENTER)
        return label

    def _overview_detail_visible_child(self):
        try:
            return self.content_stack.get_visible_child()
        except (AttributeError, TypeError):
            return None

    def _adaptive_overview_showing_content(self):
        if not self._adaptive_split_enabled or self.overview_split_view is None:
            return False
        try:
            return bool(self.overview_split_view.get_show_content())
        except (AttributeError, TypeError):
            return False

    def _adaptive_real_detail_visible(self):
        current_detail = self._overview_detail_visible_child()
        return bool(
            self._adaptive_split_enabled
            and current_detail is not None
            and current_detail is not self.detail_placeholder
            and self._adaptive_overview_showing_content()
        )

    def _is_overview_child_visible(self, child):
        if child is None:
            return False
        if self._adaptive_split_enabled:
            return self._overview_detail_visible_child() is child and self._adaptive_overview_showing_content()
        try:
            return self.stack.get_visible_child_name() == 'overview_page' and self.content_stack.get_visible_child() is child
        except (AttributeError, TypeError):
            return False

    def _add_overview_detail_page(self, child, name):
        try:
            self.content_stack.add_named(child, name)
        except (AttributeError, TypeError):
            pass

    def _remove_overview_page_widget(self, child):
        try:
            if child is not None and child.get_parent() is self.content_stack:
                self.content_stack.remove(child)
        except (AttributeError, TypeError):
            pass

    def _set_overview_placeholder_visible(self):
        if self._adaptive_split_enabled and self._adaptive_narrow_mode:
            try:
                self.overview_split_view.set_show_content(False)
            except (AttributeError, TypeError):
                pass
        else:
            try:
                self.content_stack.set_visible_child_name('detail_placeholder')
            except (AttributeError, TypeError, GLib.Error):
                pass
            if self._adaptive_split_enabled and self.overview_split_view is not None:
                try:
                    self.overview_split_view.set_show_content(True)
                except (AttributeError, TypeError):
                    pass
        try:
            self.content_navigation_page.set_title(t('app_title'))
        except (AttributeError, TypeError):
            pass
        self._show_overview_header()
        self._set_header_detail_delete_visible(False)

    def _set_overview_detail_visible(self, child, title=''):
        try:
            self.content_stack.set_visible_child(child)
        except (AttributeError, TypeError, GLib.Error):
            return
        if self._adaptive_split_enabled and self.overview_split_view is not None:
            try:
                self.content_navigation_page.set_title(title or t('app_title'))
            except (AttributeError, TypeError):
                pass
            try:
                self.overview_split_view.set_show_content(True)
            except (AttributeError, TypeError):
                pass
            self._show_overview_header()
            self._set_header_detail_delete_visible()
            return
        self._show_back_only_header()
        self._set_header_detail_delete_visible()

    def _show_overview_root_page(self):
        if self._adaptive_split_enabled:
            self._set_overview_placeholder_visible()
            if not self.search_visible:
                self._show_overview_header()
            return
        try:
            self.content_stack.set_visible_child_name('list_page')
        except (AttributeError, TypeError, GLib.Error):
            pass
        if not self.search_visible:
            self._show_overview_header()

    def on_home_clicked(self, _button):
        self._show_overview_root_page()
        try:
            self.stack.set_visible_child_name('overview_page')
        except (AttributeError, TypeError, GLib.Error):
            pass
        try:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        except (AttributeError, TypeError):
            pass

    def _configure_adaptive_breakpoints(self):
        if not self._adaptive_split_enabled:
            return
        try:
            self.overview_split_view.set_collapsed(bool(self._adaptive_narrow_mode))
            self.overview_split_view.set_show_content(False if self._adaptive_narrow_mode else True)
        except (AttributeError, TypeError):
            pass
        try:
            condition = Adw.BreakpointCondition.parse(self._adaptive_collapse_condition)
            self._adaptive_breakpoint = Adw.Breakpoint.new(condition)
            self._adaptive_breakpoint.connect('apply', self._on_adaptive_breakpoint_apply)
            self._adaptive_breakpoint.connect('unapply', self._on_adaptive_breakpoint_unapply)
            self.add_breakpoint(self._adaptive_breakpoint)
        except (AttributeError, TypeError, GLib.Error):
            self._adaptive_breakpoint = None
        self._schedule_adaptive_breakpoint_fallback()

    def _schedule_adaptive_breakpoint_fallback(self):
        if not self._adaptive_split_enabled:
            return
        if self._adaptive_breakpoint_fallback_id:
            return
        self._adaptive_breakpoint_fallback_id = GLib.timeout_add(250, self._adaptive_breakpoint_fallback_tick)

    def _adaptive_breakpoint_fallback_tick(self):
        self._adaptive_breakpoint_fallback_id = 0
        if not self._adaptive_split_enabled:
            return False
        try:
            width = int(self.get_width())
        except (AttributeError, TypeError, ValueError):
            width = 0
        if width <= 0:
            self._adaptive_breakpoint_fallback_id = GLib.timeout_add(250, self._adaptive_breakpoint_fallback_tick)
            return False
        self._set_adaptive_narrow_mode(width <= 860)
        return False

    def _on_adaptive_breakpoint_apply(self, *_args):
        self._set_adaptive_narrow_mode(True)

    def _on_adaptive_breakpoint_unapply(self, *_args):
        self._set_adaptive_narrow_mode(False)

    def _set_adaptive_narrow_mode(self, enabled):
        if not self._adaptive_split_enabled or self.overview_split_view is None:
            return
        enabled = bool(enabled)
        self._adaptive_narrow_mode = enabled
        try:
            self.overview_split_view.set_collapsed(enabled)
        except (AttributeError, TypeError):
            pass
        if enabled:
            visible_detail = self._overview_detail_visible_child()
            show_content = visible_detail is not None and visible_detail is not self.detail_placeholder
            try:
                self.overview_split_view.set_show_content(show_content)
            except (AttributeError, TypeError):
                pass
        for detail_page in self.detail_pages.values():
            try:
                detail_page.set_compact_mode_override(enabled)
            except AttributeError:
                continue
        self._show_overview_header()

    def _on_overview_split_changed(self, *_args):
        self._show_overview_header()

    def _build_welcome_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page.set_vexpand(True)
        page.set_hexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)
        content.set_margin_top(36)
        content.set_margin_bottom(36)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_size_request(280, -1)

        title = Gtk.Label(label=t('welcome_title'))
        title.add_css_class('title-2')
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.set_xalign(0.5)
        content.append(title)

        subtitle = Gtk.Label(label=t('welcome_subtitle'))
        subtitle.add_css_class('dim-label')
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_xalign(0.5)
        content.append(subtitle)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        actions.set_halign(Gtk.Align.CENTER)
        actions.set_hexpand(False)

        new_button = Gtk.Button(label=t('welcome_new_button'))
        new_button.set_size_request(220, -1)
        new_button.connect('clicked', lambda _button: self._create_empty_entry())
        actions.append(new_button)

        import_button = Gtk.Button(label=t('welcome_import_button'))
        import_button.set_size_request(220, -1)
        import_button.connect('clicked', lambda _button: self._open_import_wapp_dialog())
        actions.append(import_button)

        content.append(actions)
        page.append(content)
        return page

    def _read_app_version_label(self):
        return APP_VERSION

    def on_overview_logo_clicked(self, button):
        selection = self.selection.get_selected()
        if selection != Gtk.INVALID_LIST_POSITION:
            entry = self.filtered_model.get_item(selection)
            if entry is not None:
                self.launch_entry(entry)
                return
        if self.filtered_model.get_n_items() == 1:
            entry = self.filtered_model.get_item(0)
            if entry is not None:
                self.launch_entry(entry)

    def close_event(self, *args):
        self.db.close()
        Gtk.Window.close(self, *args)

    def on_search_clicked(self, button):
        state = next_search_toggle_state(
            current_visible=self.search_visible,
            current_text=self.search_entry.get_text(),
        )
        self.search_visible = bool(state['search_visible'])
        self.search_entry.set_visible(self.search_visible)
        if state['show_back_header']:
            self._show_back_only_header()
            if state['autofocus_search_entry'] and not should_prevent_input_autofocus():
                self.search_entry.grab_focus()
            else:
                schedule_neutral_focus(self, self._main_neutral_focus_target)
            return
        if state['clear_entry_text']:
            self.search_entry.set_text('')
        if state['reset_search_text']:
            self.search_text = ''
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()
        if state['restore_header_actions']:
            self._restore_overview_header_actions()

    def on_search_entry_changed(self, entry):
        self.search_text = entry.get_text().strip().lower()
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def filter_entries(self, item):
        if not self.search_text:
            return True
        haystack = f'{item.title} {item.description}'.lower()
        return self.search_text in haystack

    def update_empty_state(self):
        self.empty_label.set_visible(self.filtered_model.get_n_items() == 0)

    def on_factory_setup(self, factory, list_item):
        try:
            list_item.set_activatable(False)
        except (AttributeError, TypeError):
            pass
        try:
            list_item.set_selectable(False)
        except (AttributeError, TypeError):
            pass
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class('entry-card')
        box.set_margin_top(0)
        box.set_margin_bottom(0)
        box.set_margin_start(0)
        box.set_margin_end(0)
        box.set_halign(Gtk.Align.FILL)
        box.set_valign(Gtk.Align.START)

        icon_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        icon_frame.add_css_class('overview-icon-frame')
        icon_frame.set_halign(Gtk.Align.START)
        icon_frame.set_valign(Gtk.Align.START)
        icon_frame.append(create_image_from_ref('', pixel_size=28, fallback_icon='applications-internet'))

        icon_button = Gtk.Button()
        icon_button.add_css_class('flat')
        icon_button.add_css_class('overview-icon-button')
        icon_button.set_focus_on_click(False)
        icon_button.set_can_focus(False)
        icon_button.set_tooltip_text(t('launch_webapp'))
        icon_button.set_child(icon_frame)

        status_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        status_column.set_halign(Gtk.Align.END)
        status_column.set_valign(Gtk.Align.START)
        status_column.set_hexpand(False)
        status_column.set_vexpand(False)

        active_dot = Gtk.Box()
        active_dot.add_css_class('overview-active-dot')
        active_dot.set_size_request(10, 10)
        active_dot.set_halign(Gtk.Align.END)
        active_dot.set_valign(Gtk.Align.START)
        active_dot.set_margin_bottom(2)

        engine_image = Gtk.Image.new_from_icon_name('applications-internet-symbolic')
        engine_image.set_pixel_size(18)
        engine_image.add_css_class('overview-engine-icon')
        engine_image.set_halign(Gtk.Align.END)
        engine_image.set_valign(Gtk.Align.START)
        engine_image.set_margin_top(2)
        engine_image.set_margin_bottom(2)

        profile_size_label = Gtk.Label(xalign=1.0, yalign=0.0)
        profile_size_label.add_css_class('profile-size-label')
        profile_size_label.set_halign(Gtk.Align.END)
        profile_size_label.set_valign(Gtk.Align.START)
        profile_size_label.set_ellipsize(Pango.EllipsizeMode.END)

        status_column.append(active_dot)
        status_column.append(engine_image)
        status_column.append(profile_size_label)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_hexpand(True)
        text_box.set_valign(Gtk.Align.START)

        title = Gtk.Label(xalign=0, yalign=0.0)
        title.add_css_class('title-4')
        title.add_css_class('entry-title')
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_hexpand(True)
        title.set_halign(Gtk.Align.START)
        title.set_valign(Gtk.Align.START)

        description = Gtk.Label(xalign=0, yalign=0.0)
        description.add_css_class('dim-label')
        description.add_css_class('entry-subtitle')
        description.set_wrap(True)
        description.set_wrap_mode(2)
        description.set_max_width_chars(80)
        description.set_valign(Gtk.Align.START)
        description.set_halign(Gtk.Align.START)
        description.set_hexpand(True)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_row.set_hexpand(True)
        title_row.set_valign(Gtk.Align.START)
        title_row.append(title)

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_box.set_halign(Gtk.Align.END)
        status_box.set_valign(Gtk.Align.START)
        status_box.set_hexpand(False)
        title_row.append(status_box)

        subtitle_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        subtitle_row.set_hexpand(True)
        subtitle_row.set_valign(Gtk.Align.START)
        subtitle_row.append(description)

        text_box.append(title_row)
        text_box.append(subtitle_row)

        content_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content_box.set_hexpand(True)
        content_box.set_valign(Gtk.Align.START)
        content_box.append(text_box)
        content_box.append(status_column)

        content_click_gesture = Gtk.GestureClick()
        content_click_gesture.set_button(Gdk.BUTTON_PRIMARY)
        content_click_gesture.connect('released', self._on_overview_content_released)
        content_box.add_controller(content_click_gesture)

        box.append(icon_button)
        box.append(content_box)
        list_item.set_child(box)
        list_item._overview_widgets = {
            'icon_button': icon_button,
            'icon_frame': icon_frame,
            'content_box': content_box,
            'text_box': text_box,
            'status_column': status_column,
            'title_row': title_row,
            'subtitle_row': subtitle_row,
            'title_label': title,
            'status_box': status_box,
            'description_label': description,
            'active_dot': active_dot,
            'engine_image': engine_image,
            'profile_size_label': profile_size_label,
        }

    def _log_overview_icon_event(self, phase, button):
        entry = getattr(button, '_bound_entry', None) if button is not None else None
        suppress_entry_id = getattr(self, '_suppress_next_overview_activate_entry_id', None)
        suppress_until = getattr(self, '_suppress_next_overview_activate_until_us', 0)
        try:
            selected = self.selection.get_selected()
        except (AttributeError, TypeError):
            selected = None
        LOG.info(
            'Overview icon %s widget_id=%s entry_id=%s title=%r selected=%r suppress_entry_id=%r suppress_until_us=%r sensitive=%r visible=%r',
            phase,
            id(button) if button is not None else None,
            getattr(entry, 'id', None),
            getattr(entry, 'title', None),
            selected,
            suppress_entry_id,
            suppress_until,
            button.get_sensitive() if button is not None else None,
            button.get_visible() if button is not None else None,
        )

    def _on_overview_icon_pressed(self, gesture, _n_press, _x, _y):
        try:
            button = gesture.get_widget()
        except (AttributeError, TypeError):
            button = None
        if button is None:
            LOG.info('Overview icon press detected, but no widget was attached')
            return
        self._log_overview_icon_event('pressed', button)
        entry = getattr(button, '_bound_entry', None)
        self._launch_entry_from_icon(entry)

    def _on_overview_icon_released(self, gesture, _n_press, _x, _y):
        try:
            button = gesture.get_widget()
        except (AttributeError, TypeError):
            button = None
        if button is None:
            LOG.info('Overview icon release detected, but no widget was attached')
            return
        self._log_overview_icon_event('released', button)

    def _on_overview_icon_clicked(self, button):
        self._log_overview_icon_event('clicked', button)
        entry = getattr(button, '_bound_entry', None)
        if entry is None:
            LOG.info('Overview icon clicked without bound entry')
            return
        self._launch_entry_from_icon(entry)

    def _on_overview_content_released(self, gesture, _n_press, _x, _y):
        try:
            widget = gesture.get_widget()
        except (AttributeError, TypeError):
            widget = None
        entry = getattr(widget, '_bound_entry', None) if widget is not None else None
        if entry is None:
            LOG.info('Overview content tap detected, but no entry was attached')
            return
        LOG.info(
            'Overview content tap opening detail for entry_id=%s title=%r',
            getattr(entry, 'id', None),
            getattr(entry, 'title', None),
        )
        self.on_entry_activated(entry)

    def _clear_overview_icon_button_handler(self, icon_button):
        button_handler = getattr(icon_button, '_click_handler_id', None)
        if button_handler is None:
            return
        try:
            icon_button.disconnect(button_handler)
        except (AttributeError, TypeError, GLib.Error):
            pass
        icon_button._click_handler_id = None

    def _bind_overview_icon_button(self, icon_button, entry):
        icon_button._bound_entry = entry
        self._clear_overview_icon_button_handler(icon_button)
        icon_button._click_handler_id = icon_button.connect('clicked', self._on_overview_icon_clicked)

    def on_list_view_activate(self, list_view, position):
        try:
            entry = self.filtered_model.get_item(position)
        except (AttributeError, TypeError):
            entry = None
        if entry is None:
            return
        suppress_entry_id = getattr(self, '_suppress_next_overview_activate_entry_id', None)
        suppress_deadline = int(getattr(self, '_suppress_next_overview_activate_until_us', 0) or 0)
        now_us = 0
        try:
            now_us = int(GLib.get_monotonic_time())
        except (AttributeError, TypeError, ValueError):
            now_us = 0
        if suppress_entry_id == getattr(entry, 'id', None) and now_us and now_us <= suppress_deadline:
            self._suppress_next_overview_activate_entry_id = None
            self._suppress_next_overview_activate_until_us = 0
            try:
                self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
            except (AttributeError, TypeError, GLib.Error):
                pass
            return
        self.on_entry_activated(entry)
        try:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
        except (AttributeError, TypeError, GLib.Error):
            pass

    def on_factory_bind(self, factory, list_item):
        entry = list_item.get_item()
        widgets = getattr(list_item, '_overview_widgets', {})
        icon_button = widgets.get('icon_button')
        icon_frame = widgets.get('icon_frame')
        content_box = widgets.get('content_box')
        status_box = widgets.get('status_box')
        title_label = widgets.get('title_label')
        description_label = widgets.get('description_label')
        engine_image = widgets.get('engine_image')
        active_dot = widgets.get('active_dot')
        profile_size_label = widgets.get('profile_size_label')
        if not all((icon_button, icon_frame, content_box, status_box, title_label, description_label, engine_image, active_dot, profile_size_label)):
            return
        handlers = getattr(list_item, '_entry_handlers', [])
        old_entry = getattr(list_item, '_bound_entry', None)
        if old_entry is not None:
            for handler_id in handlers:
                try:
                    old_entry.disconnect(handler_id)
                except (AttributeError, TypeError):
                    pass
        self._clear_overview_icon_button_handler(icon_button)
        list_item._bound_entry = entry
        self._bind_overview_icon_button(icon_button, entry)
        content_box._bound_entry = entry
        title_label.set_text(entry.title)
        description_label.set_text(entry.description)
        self._set_overview_icon(icon_frame, entry.id)
        self._set_status_indicators(status_box, entry.id, entry.active, engine_image, active_dot)
        self._set_profile_size_label(profile_size_label, entry.id)
        list_item._entry_handlers = [
            entry.connect('notify::title', lambda e, pspec: self._on_entry_changed(e, icon_frame, status_box, profile_size_label, title_label, description_label, engine_image, active_dot)),
            entry.connect('notify::description', lambda e, pspec: self._on_entry_changed(e, icon_frame, status_box, profile_size_label, title_label, description_label, engine_image, active_dot)),
            entry.connect('notify::active', lambda e, pspec: self._on_entry_changed(e, icon_frame, status_box, profile_size_label, title_label, description_label, engine_image, active_dot)),
        ]

    def _set_overview_icon(self, icon_frame, entry_id):
        old_icon = icon_frame.get_first_child()
        if old_icon is not None:
            icon_frame.remove(old_icon)
        icon_ref = self._get_options_dict(entry_id).get(ICON_PATH_KEY, '')
        if icon_ref:
            new_icon = create_image_from_ref(icon_ref, pixel_size=40, fallback_icon='applications-internet')
        else:
            new_icon = create_image_from_ref('', pixel_size=28, fallback_icon='applications-internet')
        icon_frame.prepend(new_icon)

    def _set_profile_size_label(self, profile_size_label, entry_id):
        if profile_size_label is None:
            return
        options = self._get_options_dict(entry_id)
        profile_path = options.get(PROFILE_PATH_KEY, '')
        profile_size_label._entry_id = entry_id
        profile_size_label._profile_path = profile_path
        size_text = self._get_profile_size_text_cached(entry_id, profile_path)
        profile_size_label.set_text(size_text)
        profile_size_label.set_visible(bool(size_text))
        self._schedule_profile_size_refresh(entry_id, profile_path, profile_size_label)

    def _set_status_indicators(self, status_box, entry_id, active=False, engine_widget=None, active_dot=None):
        child = status_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            status_box.remove(child)
            child = next_child
        options = self._get_options_dict(entry_id)
        engine_name = options.get('EngineName', '') or ''
        if engine_widget is not None:
            engine_widget.set_from_icon_name(engine_icon_name(engine_name) if engine_name else 'applications-internet-symbolic')
            engine_widget.set_visible(bool(engine_name))
        if active_dot is not None:
            active_dot.remove_css_class('active')
            active_dot.remove_css_class('inactive')
            active_dot.add_css_class('active' if active else 'inactive')
            active_dot.set_visible(True)

    def _on_entry_changed(self, entry, icon_frame, status_box, profile_size_label, title_label, description_label, engine_image=None, active_dot=None):
        title_label.set_text(entry.title)
        description_label.set_text(entry.description)
        self._set_overview_icon(icon_frame, entry.id)
        self._set_status_indicators(status_box, entry.id, entry.active, engine_image, active_dot)
        self._set_profile_size_label(profile_size_label, entry.id)
        self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
        self.update_empty_state()

    def update_header_title(self, entry):
        self._reposition_entry_in_store(entry)
        if self._is_overview_child_visible(self.detail_pages.get(entry.id)):
            try:
                self.content_navigation_page.set_title(entry.title or t('app_title'))
            except (AttributeError, TypeError):
                pass
            self._show_overview_header()

    def refresh_entry_visual(self, entry):
        self._invalidate_entry_cache(entry.id, clear_profile_size=True)
        entry.notify('title')
        entry.notify('description')

    def _on_detail_navigation_changed(self, detail_page):
        if not self._is_overview_child_visible(detail_page):
            return
        if self._adaptive_split_enabled:
            if not self._adaptive_narrow_mode:
                try:
                    self.content_navigation_page.set_title(getattr(getattr(detail_page, 'entry', None), 'title', '') or t('app_title'))
                except (AttributeError, TypeError):
                    pass
                self._restore_overview_header_actions()
                self._set_header_detail_delete_visible()
                return
            self._show_overview_header()
        self._set_header_detail_delete_visible()

    def on_entry_activated(self, entry, show_busy=True):
        if show_busy:
            self._show_busy(t('loading'))
        self._show_overview_root_page()
        self.stack.set_visible_child_name('overview_page')

        def _open_detail():
            try:
                if entry.id not in self.detail_pages:
                    detail_page = DetailPage(
                        entry,
                        self.db,
                        on_back=self.show_list_page,
                        on_delete=self.confirm_delete,
                        on_title_changed=self.update_header_title,
                        on_visual_changed=self.refresh_entry_visual,
                        on_overlay_notification=self.show_overlay_notification,
                        on_navigation_changed=self._on_detail_navigation_changed,
                    )
                    detail_page.set_compact_mode_override(self._adaptive_narrow_mode if self._adaptive_split_enabled else None)
                    self.detail_pages[entry.id] = detail_page
                    self._add_overview_detail_page(detail_page, f'detail_{entry.id}')
                else:
                    self.detail_pages[entry.id].set_compact_mode_override(self._adaptive_narrow_mode if self._adaptive_split_enabled else None)
                self._set_overview_detail_visible(self.detail_pages[entry.id], entry.title or t('app_title'))
            except (GLib.Error, OSError, TypeError, ValueError) as error:
                LOG.error('Failed to open detail page for entry %s: %s', entry.id, error, exc_info=True)
                self.show_overlay_notification(t('detail_view_load_failed'), timeout_ms=3500)
                self._show_overview_root_page()
                self.stack.set_visible_child_name('overview_page')
            finally:
                if show_busy:
                    self._hide_busy()
            return False

        GLib.idle_add(_open_detail)

    def confirm_delete(self, entry):
        self.delete_entry(entry)

    def delete_entry(self, entry):
        options = self._get_options_dict(entry.id)
        delete_managed_entry_artifacts(
            entry.id,
            entry.title,
            ENGINES,
            LOG,
            delete_profiles=True,
            stored_profile_path=options.get(PROFILE_PATH_KEY, ''),
            stored_profile_name=options.get(PROFILE_NAME_KEY, ''),
        )
        self.db.delete_entry(entry.id)
        index_to_remove = None
        for index in range(self.entries_store.get_n_items()):
            if self.entries_store.get_item(index).id == entry.id:
                index_to_remove = index
                break
        if index_to_remove is not None:
            self.entries_store.remove(index_to_remove)
        if entry.id in self.detail_pages:
            page = self.detail_pages[entry.id]
            if self._is_overview_child_visible(page):
                self._show_overview_root_page()
                self.stack.set_visible_child_name('overview_page')
            GLib.idle_add(self._cleanup_detail_pages, [page])
            del self.detail_pages[entry.id]
        self.update_empty_state()
        self.show_list_page()

    def _release_detail_page(self, page):
        if page is None:
            return
        try:
            page.release_resources()
        except (AttributeError, TypeError):
            LOG.debug('Detail page cleanup failed before release', exc_info=True)
        entry_id = getattr(getattr(page, 'entry', None), 'id', None)
        if entry_id in self.detail_pages and self.detail_pages.get(entry_id) is page:
            del self.detail_pages[entry_id]
        GLib.idle_add(self._cleanup_detail_pages, [page])

    def show_list_page(self, *args):
        current_name = None
        try:
            current_name = self.stack.get_visible_child_name()
        except (AttributeError, TypeError):
            current_name = None
        if self.search_visible:
            self.search_visible = False
            self.search_entry.set_visible(False)
            if self.search_entry.get_text():
                self.search_entry.set_text('')
            self.search_text = ''
            self.custom_filter.changed(Gtk.FilterChange.DIFFERENT)
            self.update_empty_state()
            self._restore_overview_header_actions()
            return
        current_detail = self._overview_detail_visible_child()
        if isinstance(current_detail, DetailPage) and current_detail.is_subpage_visible():
            current_detail.show_main_page()
            self._show_overview_header()
            return
        if self._adaptive_split_enabled:
            if current_detail is getattr(self, 'settings_assets_page', None):
                self._return_to_overview_from_settings_assets()
                return
            if current_detail is getattr(self, 'settings_about_page', None):
                self._return_to_overview_from_settings_subpage()
                return
            if current_detail is getattr(self, 'settings_security_privacy_page', None):
                self._return_to_overview_from_settings_subpage()
                return
            if current_detail is getattr(self, 'settings_page', None):
                self._hide_global_toast()
                self._show_overview_root_page()
                self.stack.set_visible_child_name('overview_page')
                self._restore_overview_header_actions()
                return
        if current_name == 'settings_assets_page':
            self._return_to_overview_from_settings_assets()
            return
        if current_name == 'settings_about_page':
            self._return_to_overview_from_settings_subpage()
            return
        if current_name == 'settings_security_privacy_page':
            self._return_to_overview_from_settings_subpage()
            return
        if current_name == 'settings_page':
            self._restore_overview_header_actions()
            self.stack.set_visible_child_name('overview_page')
            return
        if isinstance(current_detail, DetailPage):
            self._release_detail_page(current_detail)
        self._hide_global_toast()
        self._show_overview_root_page()
        self.stack.set_visible_child_name('overview_page')
        self._restore_overview_header_actions()

    def on_add_entry(self, button):
        if self._creating_entry:
            return
        self._present_add_choice_dialog()

    def _present_add_choice_dialog(self):
        def handle_response(response_id):
            if response_id == 'new':
                self._create_empty_entry()
            elif response_id == 'import':
                self._open_import_wapp_dialog()

        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog.new(
                t('add_webapp_dialog_title'),
                t('add_webapp_dialog_body'),
            )
            dialog.add_response('cancel', t('dialog_cancel'))
            dialog.add_response('import', t('add_webapp_dialog_import_wapp'))
            dialog.add_response('new', t('add_webapp_dialog_manual'))
            dialog.set_default_response('new')
            dialog.set_close_response('cancel')
            dialog.set_response_appearance('cancel', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect('response', lambda _d, response: handle_response(response))
            dialog.present(self)
            return

        dialog = Adw.MessageDialog.new(self, t('add_webapp_dialog_title'), t('add_webapp_dialog_body'))
        dialog.add_response('cancel', t('dialog_cancel'))
        dialog.add_response('import', t('add_webapp_dialog_import_wapp'))
        dialog.add_response('new', t('add_webapp_dialog_manual'))
        dialog.set_default_response('new')
        dialog.set_close_response('cancel')
        dialog.set_response_appearance('cancel', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', lambda _d, response: handle_response(response))
        dialog.present()

    def _create_empty_entry(self):
        self._creating_entry = True
        self.add_button.set_sensitive(False)
        try:
            new_id = self.db.add_entry('')
            if new_id is not None:
                entry = Entry(new_id, '')
                self.entries_store.append(entry)
                self.on_entry_activated(entry, show_busy=False)
            try:
                self.selection.set_selected(Gtk.INVALID_LIST_POSITION)
            except (AttributeError, TypeError, GLib.Error):
                pass
        finally:
            self._creating_entry = False
            self.add_button.set_sensitive(True)
