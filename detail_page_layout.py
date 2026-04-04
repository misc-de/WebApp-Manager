from gi.repository import Adw, Gtk, GLib, Pango

from focus_guard import focus_neutral_widget, should_prevent_input_autofocus
from logger_setup import get_logger
from ui_flow_state import detail_neutral_focus_slot

LOG = get_logger(__name__)


class DetailPageLayoutMixin:
    def _adaptive_wrap_page(self, child, maximum_size=820, tightening_threshold=560):
            if hasattr(Adw, 'Clamp'):
                clamp = Adw.Clamp()
                clamp.set_hexpand(True)
                clamp.set_valign(Gtk.Align.START)
                if hasattr(clamp, 'set_maximum_size'):
                    clamp.set_maximum_size(maximum_size)
                if hasattr(clamp, 'set_tightening_threshold'):
                    clamp.set_tightening_threshold(tightening_threshold)
                clamp.set_child(child)
                return clamp
            return child

    def _effective_layout_width(self):
            page_stack = getattr(self, 'page_stack', None)
            grid = getattr(self, 'grid', None)
            return max(
                int(self.get_width() or 0),
                int(page_stack.get_width() or 0) if page_stack is not None else 0,
                int(grid.get_width() or 0) if grid is not None else 0,
            )

    def set_compact_mode_override(self, enabled=None):
            override = None if enabled is None else bool(enabled)
            if self._compact_mode_override == override:
                return
            self._compact_mode_override = override
            self._apply_adaptive_layout(force=True)
            self._update_tabbed_navigation_state()

    def _is_compact_layout(self):
            if self._compact_mode_override is not None:
                return self._compact_mode_override
            width = self._effective_layout_width()
            return width > 0 and width < 620

    def _current_page_name(self):
            try:
                return self.page_stack.get_visible_child_name() or 'main'
            except (AttributeError, TypeError):
                return 'main'

    def _desktop_tab_target(self, page_name=None):
            current = page_name or self._current_page_name()
            return current if current in {'main', 'options', 'css_assets', 'javascript_assets'} else 'main'

    def _sync_desktop_tab_buttons(self, page_name=None):
            target = self._desktop_tab_target(page_name)
            self._desktop_tabs_syncing = True
            try:
                for name, button in self.desktop_tab_buttons.items():
                    button.set_active(name == target)
            finally:
                self._desktop_tabs_syncing = False

    def _on_desktop_tab_toggled(self, button, page_name):
            if self._desktop_tabs_syncing or not button.get_active():
                return
            self._show_tab_page(page_name)

    def _move_widget_to_box(self, widget, box):
            if widget is None or box is None:
                return
            parent = widget.get_parent()
            if parent is box:
                return
            if parent is not None:
                try:
                    parent.remove(widget)
                except Exception:
                    pass
            box.append(widget)

    def _mount_options_section(self, compact, force=False):
            if not force and self._options_section_compact == compact:
                return
            self._options_section_compact = compact
            target_box = self.options_page_content
            self._move_widget_to_box(self.options_section, target_box)

    def _clear_grid(self):
            child = self.grid.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                self.grid.remove(child)
                child = next_child

    def _rebuild_form_layout(self, force=False):
            compact = self._is_compact_layout()
            if not force and getattr(self, '_form_compact', None) == compact:
                return
            self._form_compact = compact
            self._clear_grid()
            self.grid.set_margin_top(22 if not compact else 16)
            self.grid.set_column_spacing(10 if not compact else 8)
            self.grid.set_row_spacing(8 if not compact else 8)

            fields = [
                (self.title_label, self.title_entry),
                (self.description_label, self.description_entry),
                (self.address_label, self.address_entry),
                (self.engine_label, self.engine_dropdown),
                (self.user_agent_label, self.user_agent_dropdown),
                (self.mode_label, self.mode_dropdown),
                (self.color_scheme_label, self.color_scheme_dropdown),
                (self.default_zoom_label, self.default_zoom_dropdown),
            ]
            row = 0
            for label, widget in fields:
                label.set_wrap(False)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_valign(Gtk.Align.CENTER)
                label.set_hexpand(False)
                widget.set_hexpand(True)
                self.grid.attach(label, 0, row, 1, 1)
                self.grid.attach(widget, 1, row, 1, 1)
                row += 1
                if widget is self.address_entry:
                    self.url_status_label.set_margin_start(0 if compact else 10)
                    self.grid.attach(self.url_status_label, 1, row, 1, 1)
                    row += 1
                    self.grid.attach(self.engine_spacer, 0, row, 2, 1)
                    row += 1

    def _subpage_side_inset(self, compact):
            if compact:
                return 0
            return 18

    def _apply_subpage_adaptive_layout(self, force=False):
            compact = self._is_compact_layout()
            if not force and self._subpage_compact == compact:
                return
            self._subpage_compact = compact

            vertical_margin = 8 if compact else 12
            side_margin = 20 if compact else 12
            inner_margin = 8 if compact else 12
            side_inset = self._subpage_side_inset(compact)

            self.content_box.set_margin_top(vertical_margin)
            self.content_box.set_margin_bottom(vertical_margin)
            self.content_box.set_margin_start(side_margin)
            self.content_box.set_margin_end(side_margin)

            self.options_page.set_margin_top(vertical_margin)
            self.options_page.set_margin_bottom(vertical_margin)
            self.options_page.set_margin_start(side_margin)
            self.options_page.set_margin_end(side_margin)
            self.options_page_content.set_margin_top(0 if compact else inner_margin)
            self.options_page_content.set_margin_bottom(inner_margin)
            self.options_page_content.set_margin_start(side_inset)
            self.options_page_content.set_margin_end(side_inset)

            self.icon_page.set_margin_top(vertical_margin)
            self.icon_page.set_margin_bottom(vertical_margin)
            self.icon_page.set_margin_start(side_margin)
            self.icon_page.set_margin_end(side_margin)
            self.icon_page_content.set_margin_top(inner_margin)
            self.icon_page_content.set_margin_bottom(inner_margin)
            self.icon_page_content.set_margin_start(side_inset)
            self.icon_page_content.set_margin_end(side_inset)
            self.icon_page_content.set_spacing(8 if compact else 4)
            self.icon_page_progress_box.set_margin_top(8 if compact else 10)
            self.icon_page_progress_box.set_margin_bottom(10 if compact else 12)
            self.icon_page_preview_frame.set_size_request(80 if compact else 92, 80 if compact else 92)
            self.icon_page_preview_canvas.set_size_request(80 if compact else 92, 80 if compact else 92)
            for button in self._icon_page_buttons:
                button.set_hexpand(compact)
                button.set_halign(Gtk.Align.FILL if compact else Gtk.Align.CENTER)

            for state in getattr(self, '_asset_page_state', {}).values():
                page = state['page']
                content = state['content']
                selector_row = state['selector_row']
                add_button = state['add_button']
                page.set_margin_top(vertical_margin)
                page.set_margin_bottom(vertical_margin)
                page.set_margin_start(side_margin)
                page.set_margin_end(side_margin)
                content.set_margin_top(0)
                content.set_margin_bottom(inner_margin)
                content.set_margin_start(side_inset)
                content.set_margin_end(side_inset)
                content.set_spacing(8 if compact else 10)
                selector_row.set_orientation(Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL)
                selector_row.set_spacing(8)
                add_button.set_hexpand(compact)
                add_button.set_halign(Gtk.Align.FILL if compact else Gtk.Align.START)
                inline_scrolled = state.get('inline_scrolled')
                if inline_scrolled is not None:
                    inline_scrolled.set_min_content_height(150 if compact else 220)
                inline_buffer = state.get('inline_buffer')
                if inline_buffer is not None:
                    for editor in getattr(self, '_code_editors', []):
                        if editor.get('buffer') is inline_buffer:
                            self._sync_code_editor_line_number_visibility(editor)
                            break

    def _apply_adaptive_layout(self, force=False):
            compact = self._is_compact_layout()
            if force or getattr(self, '_top_row_compact', None) != compact:
                self._top_row_compact = compact
                self.top_row.set_orientation(Gtk.Orientation.HORIZONTAL)
                self.top_row.set_spacing(10 if compact else 12)
                self.top_row.set_halign(Gtk.Align.FILL)
                self.top_row.set_hexpand(True)
                self.header_main_row.set_spacing(10 if compact else 12)
                self.header_main_row.set_hexpand(True)
                self.header_main_row.set_halign(Gtk.Align.FILL)
                self.icon_button.set_size_request(64 if compact else 72, 64 if compact else 72)
                self.icon_button.set_valign(Gtk.Align.START)
                self.title_meta_box.set_valign(Gtk.Align.START)
                self.title_meta_box.set_margin_bottom(2 if compact else 0)
                self.header_name_label.set_max_width_chars(36 if compact else 28)
                self.header_profile_label.set_max_width_chars(36 if compact else 28)
                self.header_name_label.set_wrap(False)
                self.header_profile_label.set_wrap(False)
                self.header_name_label.set_valign(Gtk.Align.START)
                self.header_profile_label.set_valign(Gtk.Align.START)
                self.switch_box.set_halign(Gtk.Align.END)
                self.switch_box.set_hexpand(False)
                self.switch_box.set_valign(Gtk.Align.END)
                self.switch_box.set_margin_top(0)
                self.switch_box.set_margin_bottom(2 if compact else 0)
            if force or getattr(self, '_action_row_compact', None) != compact:
                self._action_row_compact = compact
                self.custom_assets_row.set_orientation(Gtk.Orientation.HORIZONTAL)
                self.custom_assets_row.set_spacing(0)
                self.custom_assets_row.set_homogeneous(True)
                self.add_js_button.set_margin_start(20 if compact else 0)
                self.export_import_row.set_orientation(Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL)
                self.export_import_row.set_spacing(8 if compact else 0)
                self.export_import_row.set_homogeneous(not compact)
                self.export_import_row.set_margin_top(34)
                self.desktop_tab_bar.set_spacing(4 if compact else 8)
                self.desktop_tab_bar.set_homogeneous(False)
                self.desktop_tab_bar.set_halign(Gtk.Align.FILL if compact else Gtk.Align.START)
                self.desktop_tab_bar.set_margin_top(6 if compact else 10)
                self.desktop_tab_bar.set_margin_start(4 if compact else 12)
                self.desktop_tab_bar.set_margin_end(4 if compact else 12)
                self.desktop_tab_bar.set_margin_bottom(23 if compact else 10)
                for button in self.desktop_tab_buttons.values():
                    button.set_hexpand(False)
                    button.set_halign(Gtk.Align.CENTER if compact else Gtk.Align.START)
            self._mount_options_section(compact, force=force)
            self.custom_assets_row.set_visible(False)
            self.detail_tab_scroller.set_visible(self._current_page_name() != 'icon')
            self.desktop_tab_bar.set_visible(self._current_page_name() != 'icon')
            self._sync_desktop_tab_buttons()
            self._apply_subpage_adaptive_layout(force=force)
            self._rebuild_form_layout(force=force)
            self._rebuild_options_layout(force=force)

    def _queue_options_layout_rebuild(self):
            if self._options_rebuild_source_id:
                return

            def run_rebuild():
                self._options_rebuild_source_id = 0
                self._apply_adaptive_layout(force=False)
                return False

            self._options_rebuild_source_id = GLib.timeout_add(60, run_rebuild)

    def _on_layout_width_changed(self, *args):
            self._queue_options_layout_rebuild()

    def _finish_initial_detail_setup(self):
            self._reload_options_cache_from_db()
            self._apply_adaptive_layout(force=True)
            self._apply_option_values_to_controls()
            self._update_tabbed_navigation_state()
            self._schedule_mobile_focus_reset()
            self._suspend_change_handlers = False
            return False

    def _focus_mobile_neutral_target(self):
            if not self._is_compact_layout() and not should_prevent_input_autofocus():
                return False
            page_name = self._current_page_name()
            asset_state = getattr(self, '_asset_page_state', {})
            candidates = {
                'icon_button': getattr(self, 'icon_button', None),
                'first_icon_page_button': (getattr(self, '_icon_page_buttons', []) or [None])[0],
                'main_tab_button': (getattr(self, 'desktop_tab_buttons', {}) or {}).get('main'),
                'options_tab_button': (getattr(self, 'desktop_tab_buttons', {}) or {}).get('options'),
                'css_tab_button': (getattr(self, 'desktop_tab_buttons', {}) or {}).get('css_assets'),
                'css_add_button': (asset_state.get('css', {}) or {}).get('add_button'),
                'css_dropdown': (asset_state.get('css', {}) or {}).get('dropdown'),
                'javascript_tab_button': (getattr(self, 'desktop_tab_buttons', {}) or {}).get('javascript_assets'),
                'javascript_add_button': (asset_state.get('javascript', {}) or {}).get('add_button'),
                'javascript_dropdown': (asset_state.get('javascript', {}) or {}).get('dropdown'),
            }
            target = None
            for slot in detail_neutral_focus_slot(page_name):
                candidate = candidates.get(slot)
                if candidate is not None:
                    target = candidate
                    break
            if target is None:
                return False
            return focus_neutral_widget(self, target)

    def _schedule_mobile_focus_reset(self):
            if not self._is_compact_layout() and not should_prevent_input_autofocus():
                return
            GLib.idle_add(self._focus_mobile_neutral_target)

    def _notify_navigation_changed(self):
            if callable(self.on_navigation_changed):
                try:
                    self.on_navigation_changed(self)
                except Exception:
                    LOG.debug('Failed to notify detail navigation change', exc_info=True)

    def _on_page_stack_visible_child_changed(self, *args):
            self._schedule_mobile_focus_reset()
            self._notify_navigation_changed()

    def _update_tabbed_navigation_state(self):
            current_name = self._current_page_name()
            tab_bar_visible = current_name != 'icon'
            self.detail_tab_scroller.set_visible(tab_bar_visible)
            self.desktop_tab_bar.set_visible(tab_bar_visible)
            self.custom_assets_row.set_visible(False)
            self._sync_desktop_tab_buttons(current_name)
            self._notify_navigation_changed()

    def is_subpage_visible(self):
            current_name = self._current_page_name()
            return current_name not in {'main', 'options', 'css_assets', 'javascript_assets'}

    def _capture_main_page_scroll_position(self):
            adjustment = None
            try:
                adjustment = self.scrolled.get_vadjustment()
            except Exception:
                adjustment = None
            if adjustment is None:
                self._detail_main_scroll_position = 0.0
                return
            try:
                self._detail_main_scroll_position = max(0.0, float(adjustment.get_value()))
            except Exception:
                self._detail_main_scroll_position = 0.0

    def _restore_main_page_scroll_position(self):
            if getattr(self, '_detail_main_scroll_restore_source_id', 0):
                GLib.source_remove(self._detail_main_scroll_restore_source_id)
                self._detail_main_scroll_restore_source_id = 0

            def apply_restore():
                self._detail_main_scroll_restore_source_id = 0
                adjustment = None
                try:
                    adjustment = self.scrolled.get_vadjustment()
                except Exception:
                    adjustment = None
                if adjustment is None:
                    return False
                try:
                    upper = float(adjustment.get_upper())
                    page_size = float(adjustment.get_page_size())
                    target = max(0.0, min(float(getattr(self, '_detail_main_scroll_position', 0.0)), max(0.0, upper - page_size)))
                    adjustment.set_value(target)
                except Exception:
                    pass
                return False

            self._detail_main_scroll_restore_source_id = GLib.idle_add(apply_restore)

    def show_main_page(self):
            self._show_tab_page('main')
            self._suspend_change_handlers = False

    def show_asset_page(self, asset_type):
            page_name = 'css_assets' if asset_type == 'css' else 'javascript_assets'
            self._show_tab_page(page_name)

    def _show_tab_page(self, page_name):
            current_name = self._current_page_name()
            if current_name == 'main' and page_name != 'main':
                self._capture_main_page_scroll_position()
            self.page_stack.set_visible_child_name(page_name)
            if page_name == 'main':
                self._restore_main_page_scroll_position()
            elif page_name == 'css_assets':
                self._refresh_asset_page('css')
            elif page_name == 'javascript_assets':
                self._refresh_asset_page('javascript')
            self._sync_desktop_tab_buttons(page_name)
            self._update_tabbed_navigation_state()
