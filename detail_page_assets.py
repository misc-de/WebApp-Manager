from gi.repository import Gio, Gtk, Pango, GLib
try:
    from gi.repository import GtkSource
except (ImportError, ValueError):
    GtkSource = None
from custom_assets import ASSET_OPTION_KEY_BY_TYPE, CUSTOM_CSS_LINKS_KEY, CUSTOM_JS_LINKS_KEY, INLINE_CUSTOM_CSS_KEY, INLINE_CUSTOM_JS_KEY, INLINE_CUSTOM_CSS_HASH_KEY, INLINE_CUSTOM_JS_HASH_KEY, asset_content_sha256_from_text, encode_linked_asset_ids, format_asset_date, get_custom_asset, list_custom_assets, normalize_linked_asset_ids
from i18n import t


class DetailPageAssetsMixin:
    def _asset_option_key(self, asset_type):
            return ASSET_OPTION_KEY_BY_TYPE['css' if asset_type == 'css' else 'javascript']

    def _linked_asset_ids(self, asset_type):
            key = self._asset_option_key(asset_type)
            return normalize_linked_asset_ids(self._get_option_value(key), asset_type=asset_type)

    def _linked_assets(self, asset_type):
            items = []
            for asset_id in self._linked_asset_ids(asset_type):
                asset = get_custom_asset(asset_id)
                if asset is not None:
                    items.append(asset)
            return items

    def _set_linked_assets(self, asset_type, asset_ids):
            key = self._asset_option_key(asset_type)
            encoded = encode_linked_asset_ids(asset_ids, asset_type=asset_type)
            self._set_option_value(key, encoded)
            self._refresh_asset_page(asset_type)
            self.save_desktop_file()

    def _inline_asset_option_key(self, asset_type):
            return INLINE_CUSTOM_CSS_KEY if asset_type == 'css' else INLINE_CUSTOM_JS_KEY

    def _inline_asset_hash_option_key(self, asset_type):
            return INLINE_CUSTOM_CSS_HASH_KEY if asset_type == 'css' else INLINE_CUSTOM_JS_HASH_KEY

    def _get_inline_asset_text(self, asset_type):
            return (self._get_option_value(self._inline_asset_option_key(asset_type)) or '').replace('\r\n', '\n').replace('\r', '\n')

    def _get_buffer_text(self, buffer):
            start_iter = buffer.get_start_iter()
            end_iter = buffer.get_end_iter()
            return buffer.get_text(start_iter, end_iter, True)

    def _set_buffer_text_if_needed(self, buffer, text):
            current = self._get_buffer_text(buffer)
            if current == text:
                return
            previous_suspend = self._suspend_change_handlers
            self._suspend_change_handlers = True
            try:
                buffer.set_text(text)
            finally:
                self._suspend_change_handlers = previous_suspend
            for editor in getattr(self, '_code_editors', []):
                if editor.get('buffer') is buffer:
                    self._update_code_editor_line_numbers(editor)
                    break

    def _on_style_manager_dark_changed(self, *_args):
            self._apply_code_editor_theme()

    def _source_style_scheme_name(self):
            if GtkSource is None:
                return None
            try:
                manager = GtkSource.StyleSchemeManager.get_default()
            except Exception:
                return None
            if manager is None:
                return None
            preferred = [
                'Adwaita-dark', 'adwaita-dark', 'oblivion', 'solarized-dark',
            ] if self._style_manager is not None and self._style_manager.get_dark() else [
                'Adwaita', 'adwaita', 'classic', 'solarized-light',
            ]
            for name in preferred:
                try:
                    scheme = manager.get_scheme(name)
                except Exception:
                    scheme = None
                if scheme is not None:
                    return name
            try:
                scheme_ids = manager.get_scheme_ids()
            except Exception:
                scheme_ids = []
            return scheme_ids[0] if scheme_ids else None

    def _apply_code_editor_theme(self):
            use_dark = bool(self._style_manager is not None and self._style_manager.get_dark())
            scheme_name = self._source_style_scheme_name()
            for editor in getattr(self, '_code_editors', []):
                view = editor.get('view')
                buffer = editor.get('buffer')
                scrolled = editor.get('scrolled')
                line_number_view = editor.get('line_number_view')
                line_number_scrolled = editor.get('line_number_scrolled')
                if view is None or buffer is None or scrolled is None:
                    continue
                for widget in (view, scrolled, line_number_view, line_number_scrolled):
                    if widget is None:
                        continue
                    try:
                        widget.remove_css_class('inline-editor-dark')
                        widget.remove_css_class('inline-editor-light')
                    except (AttributeError, TypeError):
                        pass
                    try:
                        widget.add_css_class('inline-editor-dark' if use_dark else 'inline-editor-light')
                    except (AttributeError, TypeError):
                        pass
                if GtkSource is not None and hasattr(buffer, 'set_style_scheme') and scheme_name:
                    try:
                        manager = GtkSource.StyleSchemeManager.get_default()
                        scheme = manager.get_scheme(scheme_name) if manager is not None else None
                        buffer.set_style_scheme(scheme)
                    except (AttributeError, TypeError):
                        pass

    def _buffer_line_count(self, buffer):
            text = self._get_buffer_text(buffer)
            return 1 if not text else text.count('\n') + 1

    def _sync_code_editor_line_number_visibility(self, editor):
            line_number_scrolled = editor.get('line_number_scrolled')
            view = editor.get('view')
            uses_source_view = bool(editor.get('uses_source_view'))
            show_compact_gutter = self._is_compact_layout() or not uses_source_view
            if line_number_scrolled is not None:
                line_number_scrolled.set_visible(show_compact_gutter)
            if uses_source_view and view is not None and hasattr(view, 'set_show_line_numbers'):
                try:
                    view.set_show_line_numbers(not show_compact_gutter)
                except (AttributeError, TypeError):
                    pass

    def _update_code_editor_line_numbers(self, editor):
            buffer = editor.get('buffer')
            line_number_buffer = editor.get('line_number_buffer')
            if buffer is None or line_number_buffer is None:
                return
            line_total = self._buffer_line_count(buffer)
            line_number_buffer.set_text('\n'.join(str(index) for index in range(1, line_total + 1)))
            self._sync_code_editor_line_number_visibility(editor)

    def _register_code_editor(self, asset_type, view, buffer, scrolled, line_number_view, line_number_buffer, line_number_scrolled, uses_source_view):
            editor = {
                'asset_type': asset_type,
                'view': view,
                'buffer': buffer,
                'scrolled': scrolled,
                'line_number_view': line_number_view,
                'line_number_buffer': line_number_buffer,
                'line_number_scrolled': line_number_scrolled,
                'uses_source_view': uses_source_view,
            }
            self._code_editors.append(editor)
            self._update_code_editor_line_numbers(editor)
            self._apply_code_editor_theme()
            return editor

    def _build_code_editor(self, asset_type):
            uses_source_view = GtkSource is not None
            if uses_source_view:
                buffer = GtkSource.Buffer()
                try:
                    language_manager = GtkSource.LanguageManager.get_default()
                    language = language_manager.get_language('css' if asset_type == 'css' else 'js')
                    if language is None and asset_type == 'javascript':
                        language = language_manager.get_language('javascript')
                    if language is not None:
                        buffer.set_language(language)
                except (AttributeError, TypeError):
                    pass
                view = GtkSource.View.new_with_buffer(buffer)
                try:
                    view.set_show_line_numbers(True)
                    view.set_highlight_current_line(False)
                    view.set_auto_indent(True)
                    view.set_tab_width(2)
                    view.set_insert_spaces_instead_of_tabs(True)
                except (AttributeError, TypeError):
                    pass
            else:
                view = Gtk.TextView()
                buffer = view.get_buffer()
            view.set_monospace(True)
            view.set_wrap_mode(Gtk.WrapMode.NONE)
            view.set_top_margin(8)
            view.set_bottom_margin(8)
            view.set_left_margin(8)
            view.set_right_margin(8)
            view.set_hexpand(True)
            view.set_vexpand(True)
            try:
                view.add_css_class('inline-code-editor')
            except (AttributeError, TypeError):
                pass

            line_number_view = Gtk.TextView()
            line_number_view.set_editable(False)
            line_number_view.set_cursor_visible(False)
            line_number_view.set_focusable(False)
            line_number_view.set_can_target(False)
            line_number_view.set_monospace(True)
            line_number_view.set_wrap_mode(Gtk.WrapMode.NONE)
            line_number_view.set_justification(Gtk.Justification.RIGHT)
            line_number_view.set_top_margin(8)
            line_number_view.set_bottom_margin(8)
            line_number_view.set_left_margin(6)
            line_number_view.set_right_margin(6)
            try:
                line_number_view.add_css_class('inline-code-line-numbers')
            except (AttributeError, TypeError):
                pass
            line_number_buffer = line_number_view.get_buffer()

            line_number_scrolled = Gtk.ScrolledWindow()
            line_number_scrolled.set_hexpand(False)
            line_number_scrolled.set_vexpand(False)
            line_number_scrolled.set_min_content_width(52)
            line_number_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
            line_number_scrolled.add_css_class('inline-code-line-number-rail')
            line_number_scrolled.set_child(line_number_view)

            scrolled = Gtk.ScrolledWindow()
            scrolled.set_hexpand(True)
            scrolled.set_vexpand(False)
            scrolled.set_min_content_height(180)
            scrolled.add_css_class('card')
            scrolled.add_css_class('inline-code-editor-frame')
            scrolled.set_child(view)

            shared_adjustment = scrolled.get_vadjustment()
            if shared_adjustment is not None:
                line_number_scrolled.set_vadjustment(shared_adjustment)

            editor_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            editor_box.set_hexpand(True)
            editor_box.append(line_number_scrolled)
            editor_box.append(scrolled)

            editor = self._register_code_editor(asset_type, view, buffer, scrolled, line_number_view, line_number_buffer, line_number_scrolled, uses_source_view)
            buffer.connect('changed', lambda buf, current_type=asset_type, current_editor=editor: self._on_inline_editor_changed(current_type, buf, current_editor))
            return editor_box, scrolled, view, buffer

    def _on_inline_editor_changed(self, asset_type, buffer, editor=None):
            if editor is not None:
                self._update_code_editor_line_numbers(editor)
            if self._suspend_change_handlers:
                return
            source_id = self._inline_editor_save_source_ids.get(asset_type, 0)
            if source_id:
                GLib.source_remove(source_id)

            def flush_changes():
                self._inline_editor_save_source_ids[asset_type] = 0
                self._persist_inline_asset_text(asset_type)
                return False

            self._inline_editor_save_source_ids[asset_type] = GLib.timeout_add(450, flush_changes)

    def _persist_inline_asset_text(self, asset_type):
            state = getattr(self, '_asset_page_state', {}).get(asset_type)
            if not state:
                return
            buffer = state.get('inline_buffer')
            if buffer is None:
                return
            text_value = self._get_buffer_text(buffer).replace('\r\n', '\n').replace('\r', '\n')
            if not text_value.strip():
                text_value = ''
            key = self._inline_asset_option_key(asset_type)
            hash_key = self._inline_asset_hash_option_key(asset_type)
            text_hash = asset_content_sha256_from_text(text_value)
            if (self._get_option_value(key) or '') == text_value and (self._get_option_value(hash_key) or '') == text_hash:
                return
            self._set_option_value(key, text_value, commit=False)
            self._set_option_value(hash_key, text_hash, commit=False)
            self.save_desktop_file()

    def _build_asset_page(self, asset_type):
            page_name = 'css_assets' if asset_type == 'css' else 'javascript_assets'
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            page.set_margin_top(12)
            page.set_margin_bottom(12)
            page.set_margin_start(12)
            page.set_margin_end(12)
            page.set_valign(Gtk.Align.START)
            page.set_vexpand(False)
            page.set_hexpand(True)

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            content.set_halign(Gtk.Align.FILL)
            content.set_margin_top(0)
            content.set_margin_bottom(12)
            page.append(content)

            title = Gtk.Label(label=t('detail_asset_page_title_css' if asset_type == 'css' else 'detail_asset_page_title_javascript'))
            title.add_css_class('heading')
            title.set_xalign(0)
            content.append(title)

            hint = Gtk.Label(label=t('detail_asset_page_hint_css' if asset_type == 'css' else 'detail_asset_page_hint_javascript'))
            hint.set_xalign(0)
            hint.set_wrap(True)
            hint.add_css_class('dim-label')
            content.append(hint)

            selector_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            selector_row.set_hexpand(True)
            content.append(selector_row)

            dropdown = Gtk.DropDown.new_from_strings([t('detail_asset_dropdown_none')])
            dropdown.set_hexpand(True)
            selector_row.append(dropdown)

            add_button = Gtk.Button(label=t('detail_asset_add_selected_css' if asset_type == 'css' else 'detail_asset_add_selected_javascript'))
            add_button.connect('clicked', lambda _button, current_type=asset_type: self._add_selected_asset(current_type))
            selector_row.append(add_button)

            current_header = Gtk.Label(label=t('detail_asset_linked_css' if asset_type == 'css' else 'detail_asset_linked_javascript'))
            current_header.add_css_class('heading')
            current_header.set_xalign(0)
            current_header.set_margin_top(4)
            content.append(current_header)

            empty_label = Gtk.Label(label=t('detail_asset_empty_css' if asset_type == 'css' else 'detail_asset_empty_javascript'))
            empty_label.set_xalign(0)
            empty_label.add_css_class('dim-label')
            empty_label.set_wrap(True)
            content.append(empty_label)

            selected_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content.append(selected_list)

            inline_header = Gtk.Label(label=t('detail_asset_inline_css' if asset_type == 'css' else 'detail_asset_inline_javascript'))
            inline_header.add_css_class('heading')
            inline_header.set_xalign(0)
            inline_header.set_margin_top(8)
            content.append(inline_header)

            inline_hint = Gtk.Label(label=t('detail_asset_inline_hint_css' if asset_type == 'css' else 'detail_asset_inline_hint_javascript'))
            inline_hint.set_xalign(0)
            inline_hint.set_wrap(True)
            inline_hint.add_css_class('dim-label')
            content.append(inline_hint)

            inline_editor_widget, inline_scrolled, inline_view, inline_buffer = self._build_code_editor(asset_type)
            content.append(inline_editor_widget)

            note_label = Gtk.Label(label='')
            note_label.set_xalign(0)
            note_label.set_wrap(True)
            note_label.add_css_class('dim-label')
            note_label.set_visible(False)
            content.append(note_label)

            self._asset_page_state[asset_type] = {
                'page': page,
                'content': content,
                'selector_row': selector_row,
                'add_button': add_button,
                'dropdown': dropdown,
                'dropdown_ids': [],
                'selected_list': selected_list,
                'empty_label': empty_label,
                'inline_header': inline_header,
                'inline_hint': inline_hint,
                'inline_scrolled': inline_scrolled,
                'inline_view': inline_view,
                'inline_buffer': inline_buffer,
                'note_label': note_label,
            }
            self.page_stack.add_named(self._adaptive_wrap_page(page), page_name)

    def _refresh_asset_pages(self):
            for asset_type in list(getattr(self, '_asset_page_state', {}).keys()):
                self._refresh_asset_page(asset_type)

    def _refresh_asset_page(self, asset_type):
            state = getattr(self, '_asset_page_state', {}).get(asset_type)
            if not state:
                return
            available_assets = [asset for asset in list_custom_assets() if asset.get('type') == asset_type]
            labels = [t('detail_asset_dropdown_none')] + [f"{asset['name']} ({asset.get('type', '').upper()})" for asset in available_assets]
            dropdown_ids = [''] + [asset['id'] for asset in available_assets]
            new_dropdown = Gtk.DropDown.new_from_strings(labels)
            new_dropdown.set_hexpand(True)
            try:
                old_dropdown = state['dropdown']
                parent = old_dropdown.get_parent()
                if parent is not None:
                    parent.remove(old_dropdown)
                    parent.prepend(new_dropdown)
            except (AttributeError, TypeError):
                pass
            state['dropdown'] = new_dropdown
            state['dropdown_ids'] = dropdown_ids
            self._apply_subpage_adaptive_layout(force=True)

            selected_list = state['selected_list']
            child = selected_list.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                selected_list.remove(child)
                child = next_child

            linked_assets = self._linked_assets(asset_type)
            state['empty_label'].set_visible(not linked_assets)
            for asset in linked_assets:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.set_hexpand(True)

                text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                text_box.set_hexpand(True)
                name_label = Gtk.Label(label=str(asset.get('name') or ''), xalign=0)
                name_label.set_wrap(True)
                meta_label = Gtk.Label(label=f"{asset.get('type', '').upper()} - {format_asset_date(asset.get('imported_at'))}", xalign=0)
                meta_label.add_css_class('dim-label')
                text_box.append(name_label)
                text_box.append(meta_label)
                row.append(text_box)

                delete_button = Gtk.Button(icon_name='user-trash-symbolic')
                delete_button.add_css_class('flat')
                delete_button.connect('clicked', lambda button, current_type=asset_type, current_asset_id=asset['id'], current_name=str(asset.get('name') or ''): self._confirm_remove_linked_asset(button, current_type, current_asset_id, current_name))
                row.append(delete_button)
                selected_list.append(row)

            inline_buffer = state.get('inline_buffer')
            if inline_buffer is not None:
                self._set_buffer_text_if_needed(inline_buffer, self._get_inline_asset_text(asset_type))

            note_label = state['note_label']
            if asset_type == 'javascript' and self._current_browser_family() == 'firefox':
                note_label.set_text(t('detail_asset_firefox_js_note'))
                note_label.set_visible(True)
            else:
                note_label.set_visible(False)

    def _add_selected_asset(self, asset_type):
            state = self._asset_page_state.get(asset_type)
            if not state:
                return
            dropdown = state['dropdown']
            index = int(dropdown.get_selected())
            if index <= 0 or index >= len(state['dropdown_ids']):
                return
            asset_id = state['dropdown_ids'][index]
            current = self._linked_asset_ids(asset_type)
            if asset_id not in current:
                current.append(asset_id)
                self._set_linked_assets(asset_type, current)

    def _confirm_remove_linked_asset(self, anchor, asset_type, asset_id, asset_name):
            label_key = 'detail_asset_remove_css_confirm' if asset_type == 'css' else 'detail_asset_remove_javascript_confirm'
            self._present_choice_dialog(anchor, t(label_key, name=asset_name), lambda confirmed: self._remove_linked_asset(asset_type, asset_id) if confirmed else None, destructive=True)

    def _remove_linked_asset(self, asset_type, asset_id):
            current = [item for item in self._linked_asset_ids(asset_type) if item != asset_id]
            self._set_linked_assets(asset_type, current)
