import base64
import binascii
import io
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from PIL import Image, UnidentifiedImageError
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from icon_pipeline import get_managed_icon_path, is_svg_support_missing_error, normalize_icon_bytes_to_png, normalize_icon_to_png
from webapp_constants import ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY, USER_AGENT_VALUE_KEY
from input_validation import DESKTOP_CHROME_USER_AGENT, MAX_ICON_FILE_SIZE, build_safe_slug, candidate_urls_for_input, is_structurally_valid_url, validate_icon_source_path
from browser_profiles import get_profile_size_bytes
from app_identity import APP_ICON_NAME
from i18n import t
from logger_setup import get_logger

LOG = get_logger(__name__)


class DetailPageIconMixin:
    def _icon_request_user_agent(self):
        configured = str(self._get_option_value(USER_AGENT_VALUE_KEY) or '').strip()
        return configured or DESKTOP_CHROME_USER_AGENT

    def _registrable_domain_host(self, host):
        host = (host or '').lower().strip().strip('.')
        if not host or host == 'localhost':
            return ''
        if re.fullmatch(r'\d{1,3}(?:\.\d{1,3}){3}', host):
            return ''
        labels = [label for label in host.split('.') if label]
        if len(labels) < 2:
            return ''

        multi_part_suffixes = {
            ('ac', 'uk'),
            ('co', 'in'),
            ('co', 'jp'),
            ('co', 'kr'),
            ('co', 'nz'),
            ('co', 'uk'),
            ('co', 'za'),
            ('com', 'au'),
            ('com', 'br'),
            ('com', 'cn'),
            ('com', 'hk'),
            ('com', 'mx'),
            ('com', 'sa'),
            ('com', 'sg'),
            ('com', 'tr'),
            ('com', 'tw'),
            ('gov', 'uk'),
            ('net', 'au'),
            ('org', 'au'),
            ('org', 'uk'),
        }
        if len(labels) >= 3 and tuple(labels[-2:]) in multi_part_suffixes:
            return '.'.join(labels[-3:])
        return '.'.join(labels[-2:])

    def _has_custom_icon(self):
        icon_ref = (self._icon_path() or '').strip()
        if not icon_ref:
            return False
        if '/' not in icon_ref and '\\' not in icon_ref:
            return icon_ref not in {'applications-internet', APP_ICON_NAME}
        return Path(icon_ref).exists()

    def _maybe_autofetch_icon(self, value):
        if not value or self._has_custom_icon() or self._auto_icon_fetch_url == value:
            return
        self._auto_icon_fetch_url = value

        def worker():
            try:
                path = self._download_favicon(value)
                if path and Path(path).exists():
                    GLib.idle_add(self._apply_downloaded_icon_silent, str(path), value)
                    return
            except (OSError, ValueError, urllib.error.URLError) as error:
                LOG.debug('Automatic icon fetch failed for %s: %s', value, error)
            GLib.idle_add(self._reset_auto_icon_fetch, value)

        threading.Thread(target=worker, daemon=True).start()

    def _reset_auto_icon_fetch(self, value=''):
        if not value or self._auto_icon_fetch_url == value:
            self._auto_icon_fetch_url = ''
        return False

    def _apply_downloaded_icon_silent(self, path, value):
        temp_path = Path(path)
        try:
            if self._has_custom_icon():
                return False
            target_path = self._managed_icon_target()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_path), str(target_path))
            self._apply_icon_path(target_path)
        finally:
            temp_path.unlink(missing_ok=True)
            self._reset_auto_icon_fetch(value)
        return False

    def _build_icon_page(self):
        self.icon_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.icon_page.set_margin_top(12)
        self.icon_page.set_margin_bottom(12)
        self.icon_page.set_margin_start(12)
        self.icon_page.set_margin_end(12)
        self.icon_page.set_valign(Gtk.Align.START)
        self.icon_page.set_vexpand(False)
        self.icon_page.set_hexpand(True)

        self.icon_page_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.icon_page_content.set_halign(Gtk.Align.FILL)
        self.icon_page_content.set_margin_top(12)
        self.icon_page_content.set_margin_bottom(12)
        self.icon_page_content.set_valign(Gtk.Align.START)
        self.icon_page_content.set_vexpand(False)
        self.icon_page_content.set_hexpand(True)
        self.icon_page.append(self.icon_page_content)

        self.icon_page_preview_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.icon_page_preview_frame.add_css_class('icon-preview-frame')
        self.icon_page_preview_frame.set_halign(Gtk.Align.CENTER)
        self.icon_page_preview_frame.set_valign(Gtk.Align.START)
        self.icon_page_preview_frame.set_hexpand(False)
        self.icon_page_preview_frame.set_vexpand(False)
        self.icon_page_preview_frame.set_size_request(92, 92)
        self.icon_page_preview_frame.set_overflow(Gtk.Overflow.HIDDEN)
        self.icon_page_preview_frame.set_margin_bottom(2)
        self.icon_page_preview_canvas = Gtk.Fixed()
        self.icon_page_preview_canvas.set_size_request(92, 92)
        self.icon_page_preview_canvas.set_hexpand(False)
        self.icon_page_preview_canvas.set_vexpand(False)
        self.icon_page_preview_frame.append(self.icon_page_preview_canvas)
        self.icon_page_content.append(self.icon_page_preview_frame)

        self.icon_page_progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.icon_page_progress_box.set_halign(Gtk.Align.CENTER)
        self.icon_page_progress_box.set_valign(Gtk.Align.START)
        self.icon_page_progress_box.set_hexpand(True)
        self.icon_page_progress_box.set_vexpand(False)
        self.icon_page_progress_box.set_margin_top(10)
        self.icon_page_progress_box.set_margin_bottom(12)
        self.icon_page_content.append(self.icon_page_progress_box)

        self.icon_page_search_spinner = Gtk.Spinner()
        self.icon_page_search_spinner.set_size_request(28, 28)
        self.icon_page_search_spinner.set_halign(Gtk.Align.CENTER)
        self.icon_page_search_spinner.set_valign(Gtk.Align.START)
        self.icon_page_search_spinner.set_visible(False)
        self.icon_page_progress_box.append(self.icon_page_search_spinner)

        self.icon_page_status = Gtk.Label(label='', halign=Gtk.Align.CENTER)
        self.icon_page_status.set_vexpand(False)
        self.icon_page_status.set_xalign(0.5)
        self.icon_page_status.set_justify(Gtk.Justification.CENTER)
        self.icon_page_status.set_wrap(True)
        self.icon_page_status.add_css_class('dim-label')
        self.icon_page_status.set_margin_top(0)
        self.icon_page_status.set_margin_bottom(0)
        self.icon_page_progress_box.append(self.icon_page_status)

        self.icon_download_button = Gtk.Button(label=t('icon_action_download'))
        self.icon_download_button.set_margin_top(0)
        self.icon_download_button.set_vexpand(False)
        self.icon_download_button.connect('clicked', self.on_icon_download_clicked)
        self.icon_page_content.append(self.icon_download_button)

        self.icon_upload_button = Gtk.Button(label=t('icon_action_upload'))
        self.icon_upload_button.set_vexpand(False)
        self.icon_upload_button.connect('clicked', self.on_icon_upload_clicked)
        self.icon_page_content.append(self.icon_upload_button)

        self.icon_delete_button = Gtk.Button(label=t('icon_action_delete'))
        self.icon_delete_button.set_vexpand(False)
        self.icon_delete_button.add_css_class('destructive-action')
        self.icon_delete_button.connect('clicked', self.on_icon_delete_clicked)
        self.icon_page_content.append(self.icon_delete_button)
        self._icon_page_buttons = [self.icon_download_button, self.icon_upload_button, self.icon_delete_button]

        self.page_stack.add_named(self._adaptive_wrap_page(self.icon_page), 'icon')

    def _icon_path(self):
        return self._get_option_value(ICON_PATH_KEY) or ''

    def _refresh_header_meta(self):
        if hasattr(self, 'header_name_label'):
            self.header_name_label.set_text(self.entry.title or '')
        if hasattr(self, 'header_profile_label'):
            self.header_profile_label.set_text(self._profile_display_name())
        self.header_name_label.set_valign(Gtk.Align.START)
        self.header_profile_label.set_valign(Gtk.Align.START)

    def _emit_visual_changed(self):
        self._refresh_header_meta()
        if self.on_visual_changed:
            self.on_visual_changed(self.entry)
        if self.on_title_changed:
            self.on_title_changed(self.entry)

    def _format_size(self, size_bytes):
        if size_bytes <= 0:
            return '0 MB'
        gb = float(size_bytes) / (1024 ** 3)
        if gb >= 1:
            return f'{gb:.2f} GB'
        mb = float(size_bytes) / (1024 ** 2)
        if mb >= 1:
            return f'{mb:.0f} MB'
        kb = float(size_bytes) / 1024.0
        if kb >= 1:
            return f'{kb:.0f} KB'
        return f'{int(size_bytes)} B'

    def _apply_profile_button_label(self, profile_path, size_bytes=None):
        size_text = ''
        if profile_path:
            display_size = 0 if size_bytes is None else max(0, int(size_bytes))
            size_text = f" ({self._format_size(display_size)})"
        self.delete_profile_button.set_label(t('profile_delete_button', size=size_text))
        self.delete_profile_button.set_sensitive(bool(profile_path))

    def _finish_profile_size_refresh(self, serial, profile_path, size_bytes):
        if serial != self._profile_size_request_serial:
            return False
        self._profile_size_pending_path = ''
        self._profile_size_cache[profile_path] = max(0, int(size_bytes or 0))
        current_profile_path = self._get_option_value(PROFILE_PATH_KEY) or ''
        if current_profile_path == profile_path:
            self._apply_profile_button_label(profile_path, self._profile_size_cache[profile_path])
        return False

    def _refresh_profile_button_label(self):
        profile_path = (self._get_option_value(PROFILE_PATH_KEY) or '').strip()
        self._profile_size_request_serial += 1
        serial = self._profile_size_request_serial
        if not profile_path:
            self._profile_size_pending_path = ''
            self._apply_profile_button_label('', None)
            return
        cached_size = self._profile_size_cache.get(profile_path)
        if cached_size is not None:
            self._profile_size_pending_path = ''
            self._apply_profile_button_label(profile_path, cached_size)
            return
        self._profile_size_pending_path = profile_path
        self._apply_profile_button_label(profile_path, None)

        def worker(path_value, token):
            size_bytes = get_profile_size_bytes(path_value)
            GLib.idle_add(self._finish_profile_size_refresh, token, path_value, size_bytes)

        threading.Thread(target=worker, args=(profile_path, serial), daemon=True).start()

    def _has_exportable_webapp(self):
        title = (self.title_entry.get_text() or '').strip()
        address = self._normalize_address_for_ui(self.address_entry.get_text())
        description = (self.description_entry.get_text() or '').strip() if hasattr(self, 'description_entry') else ''
        icon_path = str(self._get_option_value(ICON_PATH_KEY) or '').strip()
        options = dict(self._options_dict())
        for transient_key in (ICON_PATH_KEY, PROFILE_NAME_KEY, PROFILE_PATH_KEY):
            options.pop(transient_key, None)
        has_meaningful_options = any(str(value or '').strip() for value in options.values())
        return any((title, address, description, icon_path, has_meaningful_options))

    def _set_inline_busy(self, active, message=None):
        is_active = bool(active)
        self.inline_busy_label.set_text((message or t('loading')) if is_active else '')
        self.inline_busy_overlay.set_visible(is_active)
        if is_active:
            self.inline_busy_spinner.start()
        else:
            self.inline_busy_spinner.stop()

    def _set_icon_download_busy(self, active, message=None):
        is_active = bool(active)
        self.icon_page_progress_box.set_visible(True)
        if is_active:
            self.icon_page_search_spinner.set_visible(True)
            self.icon_page_search_spinner.start()
            self.icon_page_status.set_text(message or t('icon_page_status_searching'))
            self.icon_download_button.set_sensitive(False)
            return
        self.icon_page_search_spinner.stop()
        self.icon_page_search_spinner.set_visible(False)
        self.icon_download_button.set_sensitive(True)

    def _set_detail_action_status(self, text=''):
        text = text or ''
        self.detail_action_status.set_text(text)
        self.detail_action_status.set_visible(bool(text.strip()))

    def _cancel_detail_toast(self):
        if self._detail_toast_timeout_id:
            GLib.source_remove(self._detail_toast_timeout_id)
            self._detail_toast_timeout_id = 0

    def _hide_detail_toast(self):
        self._cancel_detail_toast()
        return False

    def _show_plugin_banner(self, text, timeout_ms=3000):
        message = (text or '').strip()
        if not message:
            return
        self._cancel_detail_toast()
        if callable(self.on_overlay_notification):
            self.on_overlay_notification(message, timeout_ms=timeout_ms)

    def _load_texture(self, icon_path):
        try:
            path = Path(icon_path)
            cache_key = None
            if path.exists():
                cache_key = (str(path), int(path.stat().st_mtime_ns))
                cached = self._icon_texture_cache.get(cache_key)
                if cached is not None:
                    return cached
            texture = Gdk.Texture.new_from_filename(str(icon_path))
            if cache_key is not None:
                self._icon_texture_cache = {cache_key: texture}
            return texture
        except (GLib.Error, OSError, ValueError) as error:
            LOG.warning('Failed to load icon texture %s: %s', icon_path, error)
            return None

    def _prepare_display_icon_path(self, icon_path, size):
        try:
            preview_dir = Path(tempfile.gettempdir()) / 'webapp_icon_previews'
            preview_dir.mkdir(parents=True, exist_ok=True)
            source = Path(icon_path)
            target = preview_dir / f'entry-{self.entry.id}-{size}-{int(source.stat().st_mtime_ns)}.png'
            if not target.exists():
                source_suffix = source.suffix.lower()
                if source_suffix == '.svg':
                    normalize_icon_to_png(source, target)
                else:
                    with Image.open(source) as image:
                        image = image.convert('RGBA')
                        image.thumbnail((size, size), Image.Resampling.LANCZOS)
                        canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
                        x = (size - image.width) // 2
                        y = (size - image.height) // 2
                        canvas.alpha_composite(image, (x, y))
                        canvas.save(target, 'PNG')
            return target
        except (OSError, ValueError, UnidentifiedImageError):
            return Path(icon_path)

    def _create_icon_widget(self, size):
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.set_size_request(size, size)
        wrapper.set_halign(Gtk.Align.CENTER)
        wrapper.set_valign(Gtk.Align.CENTER)
        wrapper.set_hexpand(False)
        wrapper.set_vexpand(False)
        wrapper.set_overflow(Gtk.Overflow.HIDDEN)

        def _append(child):
            child.set_halign(Gtk.Align.CENTER)
            child.set_valign(Gtk.Align.CENTER)
            child.set_hexpand(False)
            child.set_vexpand(False)
            wrapper.append(child)
            return wrapper

        icon_ref = self._icon_path()
        if icon_ref:
            icon_path = Path(icon_ref)
            if icon_path.exists():
                display_path = self._prepare_display_icon_path(icon_path, size)
                texture = self._load_texture(display_path)
                if texture is not None:
                    picture = Gtk.Picture.new_for_paintable(texture)
                    picture.set_size_request(size, size)
                    picture.set_can_shrink(True)
                    picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                    picture.set_keep_aspect_ratio(True)
                    picture.set_overflow(Gtk.Overflow.HIDDEN)
                    return _append(picture)
            image = Gtk.Image.new_from_icon_name(icon_ref)
            image.set_pixel_size(max(32, size - 16))
            image.set_size_request(size, size)
            return _append(image)

        image = Gtk.Image.new_from_icon_name(APP_ICON_NAME)
        image.set_pixel_size(max(32, size - 16))
        image.set_size_request(size, size)
        return _append(image)

    def refresh_icon_preview(self):
        widget = self._create_icon_widget(56)
        self.icon_button.set_size_request(72, 72)
        widget.set_valign(Gtk.Align.CENTER)
        widget.set_halign(Gtk.Align.CENTER)
        self.icon_button.set_child(widget)

    def _clear_icon_page_preview_canvas(self):
        child = self.icon_page_preview_canvas.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.icon_page_preview_canvas.remove(child)
            child = next_child

    def _icon_preview_signature(self, size):
        icon_ref = self._icon_path() or ''
        if icon_ref:
            icon_path = Path(icon_ref)
            if icon_path.exists():
                return (str(icon_path), int(icon_path.stat().st_mtime_ns), size)
        return (icon_ref, None, size)

    def _set_icon_page_preview_placeholder(self):
        self._clear_icon_page_preview_canvas()
        placeholder = Gtk.Image.new_from_icon_name(APP_ICON_NAME)
        placeholder.set_pixel_size(48)
        placeholder.set_size_request(64, 64)
        placeholder.set_halign(Gtk.Align.CENTER)
        placeholder.set_valign(Gtk.Align.CENTER)
        self.icon_page_preview_canvas.put(placeholder, 14, 14)

    def _schedule_icon_page_preview_refresh(self):
        if self._icon_page_preview_refresh_source_id:
            GLib.source_remove(self._icon_page_preview_refresh_source_id)
            self._icon_page_preview_refresh_source_id = 0
        signature = self._icon_preview_signature(64)
        self._icon_page_preview_signature = signature
        self._icon_page_preview_refresh_source_id = GLib.idle_add(self._refresh_icon_page_preview_idle, signature)

    def _refresh_icon_page_preview_idle(self, signature):
        self._icon_page_preview_refresh_source_id = 0
        if signature != self._icon_page_preview_signature:
            return False
        preview = self._create_icon_widget(64)
        preview.set_halign(Gtk.Align.CENTER)
        preview.set_valign(Gtk.Align.CENTER)
        self._clear_icon_page_preview_canvas()
        self.icon_page_preview_canvas.put(preview, 14, 14)
        return False

    def refresh_icon_page(self):
        self._set_icon_page_preview_placeholder()

        if self._icon_download_in_progress:
            self._set_icon_download_busy(True, t('icon_page_status_searching'))
        else:
            self._set_icon_download_busy(False)
            url = self.address_entry.get_text().strip()
            if is_structurally_valid_url(url):
                self.icon_download_button.set_sensitive(True)
                self.icon_page_status.set_text(t('icon_page_status_url'))
            else:
                self.icon_download_button.set_sensitive(False)
                self.icon_page_status.set_text(t('icon_page_status_no_url'))

        has_icon = self._has_custom_icon()
        self.icon_delete_button.set_sensitive(has_icon)
        self._schedule_icon_page_preview_refresh()

    def _managed_icon_target(self):
        return get_managed_icon_path(self.entry.title, '.png', self.entry.id)

    def _sync_icon_filename(self):
        icon_path = self._icon_path()
        if not icon_path:
            return
        if '/' not in icon_path and '\\' not in icon_path:
            return
        try:
            current = Path(icon_path).expanduser()
            if not current.exists():
                return
            target = self._managed_icon_target()
            if current.resolve() == target.resolve():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(current.read_bytes())
            if current.parent == target.parent and current.name != target.name:
                try:
                    current.unlink(missing_ok=True)
                except OSError:
                    pass
            self._set_option_value(ICON_PATH_KEY, str(target))
        except OSError as error:
            LOG.warning('Failed to sync icon filename for entry %s: %s', self.entry.id, error)

    def _store_pil_image(self, pil_image, target_path=None):
        target_path = target_path or self._managed_icon_target()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        image = pil_image.convert('RGBA')
        max_size = 256
        fit_size = 220
        width, height = image.size
        if width <= 0 or height <= 0:
            raise OSError('Invalid image size')
        scale = min(fit_size / width, fit_size / height)
        if scale <= 0:
            scale = 1.0
        new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', (max_size, max_size), (0, 0, 0, 0))
        x = (max_size - image.width) // 2
        y = (max_size - image.height) // 2
        canvas.alpha_composite(image, (x, y))
        canvas.save(target_path, 'PNG')
        return target_path

    def _apply_icon_path(self, path):
        self._set_option_value(ICON_PATH_KEY, str(path))
        self.refresh_icon_preview()
        self.refresh_icon_page()
        self._update_export_button_state()
        self._emit_visual_changed()
        self.save_desktop_file()
        GLib.idle_add(self._emit_visual_changed)

    def _store_icon_file(self, source_path):
        try:
            validated_source = validate_icon_source_path(source_path)
            if validated_source is None:
                raise OSError('Invalid or oversized icon file')
            target_path = self._managed_icon_target()
            normalize_icon_to_png(validated_source, target_path)
            self._apply_icon_path(target_path)
            return True
        except (UnidentifiedImageError, OSError) as error:
            LOG.warning('Failed to normalize icon file %s: %s', source_path, error)
            if is_svg_support_missing_error(error):
                self._show_plugin_banner(t('svg_import_requires_cairo'), timeout_ms=4200)
            else:
                self._show_plugin_banner(t('icon_page_status_upload_failed'), timeout_ms=3200)
        return False

    def _parse_html_tag_attributes(self, tag):
        attrs = {}
        attr_pattern = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:\-.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))")
        for match in attr_pattern.finditer(tag):
            value = match.group(2)
            if value is None:
                value = match.group(3)
            if value is None:
                value = match.group(4) or ''
            attrs[match.group(1).lower()] = value
        return attrs

    def _size_score_from_string(self, sizes_value):
        size_score = 0
        saw_any = False
        for token in str(sizes_value or '').lower().split():
            token = token.strip()
            if not token:
                continue
            if token == 'any':
                saw_any = True
                continue
            if 'x' in token:
                try:
                    width, height = token.split('x', 1)
                    size_score = max(size_score, min(int(width), int(height)))
                except ValueError:
                    continue
        if saw_any:
            size_score = max(size_score, 4096)
        return size_score

    def _infer_size_score_from_url(self, href):
        href_path = urlparse(str(href or '')).path.lower()
        for pattern in (r'(\d{2,4})x(\d{2,4})', r'[-_](\d{2,4})[.-](?:png|svg|ico|webp|jpg|jpeg|avif)$'):
            match = re.search(pattern, href_path)
            if not match:
                continue
            try:
                if len(match.groups()) >= 2:
                    return min(int(match.group(1)), int(match.group(2)))
                return int(match.group(1))
            except ValueError:
                continue
        return 0

    def _icon_type_priority(self, href, rel_value='', type_value='', sizes_value='', purpose_value=''):
        href_path = urlparse(str(href or '')).path.lower()
        rel_value = str(rel_value or '').lower()
        type_value = str(type_value or '').lower()
        sizes_value = str(sizes_value or '').lower()
        purpose_value = str(purpose_value or '').lower()
        if (
            href_path.endswith('.svg')
            or 'image/svg+xml' in type_value
            or 'mask-icon' in rel_value
            or 'monochrome' in purpose_value
            or sizes_value.strip() == 'any'
        ):
            return 5
        if any(href_path.endswith(ext) for ext in ('.png', '.webp', '.avif', '.jpg', '.jpeg')) or any(image_type in type_value for image_type in ('image/png', 'image/webp', 'image/avif', 'image/jpeg')):
            return 4
        if 'apple-touch-icon' in rel_value or 'fluid-icon' in rel_value:
            return 4
        if href_path.endswith('.ico') or 'image/x-icon' in type_value or 'image/vnd.microsoft.icon' in type_value:
            return 2
        return 3

    def _source_priority_for_candidate(self, source_kind, rel_value='', media_value=''):
        rel_value = str(rel_value or '').lower()
        media_value = str(media_value or '').lower().strip()
        priority_map = {
            'manifest_maskable': 65,
            'manifest': 60,
            'apple_touch': 58,
            'icon_link': 56,
            'mask_icon': 54,
            'fluid_icon': 52,
            'browserconfig': 46,
            'root_fallback': 40,
            'special_fallback': 38,
            'meta_image': 10,
        }
        priority = priority_map.get(source_kind, 20)
        if 'apple-touch-icon' in rel_value:
            priority = max(priority, priority_map['apple_touch'])
        if 'mask-icon' in rel_value:
            priority = max(priority, priority_map['mask_icon'])
        if media_value:
            if 'print' in media_value:
                priority -= 20
            elif 'screen' in media_value or 'all' in media_value:
                priority += 1
        return priority

    def _make_icon_candidate(self, href, *, source_kind='icon_link', rel_value='', type_value='', sizes_value='', purpose_value='', media_value='', order=0):
        return {
            'href': href,
            'type_priority': self._icon_type_priority(href, rel_value, type_value, sizes_value, purpose_value),
            'source_priority': self._source_priority_for_candidate(source_kind, rel_value, media_value),
            'size_score': self._size_score_from_string(sizes_value) or self._infer_size_score_from_url(href),
            'order': int(order),
        }

    def _order_icon_candidates(self, candidates):
        normalized = []
        seen = set()
        for item in candidates:
            href = str((item or {}).get('href') or '').strip()
            if not href or href in seen:
                continue
            seen.add(href)
            normalized.append(item)
        normalized.sort(
            key=lambda item: (
                int(item.get('type_priority', 0)),
                int(item.get('source_priority', 0)),
                int(item.get('size_score', 0)),
                int(item.get('order', 0)),
            ),
            reverse=True,
        )
        return [item['href'] for item in normalized]

    def _extract_base_href(self, html, base_url):
        pattern = re.compile(r'<base\b[^>]*>', re.IGNORECASE)
        for match in pattern.finditer(html):
            attrs = self._parse_html_tag_attributes(match.group(0))
            href_value = str(attrs.get('href') or '').strip()
            if href_value:
                return urljoin(base_url, href_value)
        return None

    def _extract_icon_candidates(self, html, base_url):
        candidates = []
        pattern = re.compile(r'<link\b[^>]*>', re.IGNORECASE)
        order = 0
        for match in pattern.finditer(html):
            tag = match.group(0)
            attrs = self._parse_html_tag_attributes(tag)
            href_value = str(attrs.get('href') or '').strip()
            if not href_value:
                continue
            rel_value = str(attrs.get('rel') or '').lower().strip()
            href = urljoin(base_url, href_value)
            source_kind = None
            if 'manifest' in rel_value:
                continue
            if 'apple-touch-icon' in rel_value:
                source_kind = 'apple_touch'
            elif 'mask-icon' in rel_value:
                source_kind = 'mask_icon'
            elif 'fluid-icon' in rel_value:
                source_kind = 'fluid_icon'
            elif 'icon' in rel_value:
                source_kind = 'icon_link'
            if source_kind is None:
                continue
            order += 1
            candidates.append(
                self._make_icon_candidate(
                    href,
                    source_kind=source_kind,
                    rel_value=rel_value,
                    type_value=attrs.get('type', ''),
                    sizes_value=attrs.get('sizes', ''),
                    media_value=attrs.get('media', ''),
                    order=order,
                )
            )
        return candidates

    def _extract_manifest_url(self, html, base_url):
        pattern = re.compile(r'<link\b[^>]*>', re.IGNORECASE)
        for match in pattern.finditer(html):
            attrs = self._parse_html_tag_attributes(match.group(0))
            rel_value = str(attrs.get('rel') or '').lower().strip()
            href_value = str(attrs.get('href') or '').strip()
            if href_value and 'manifest' in rel_value:
                return urljoin(base_url, href_value)
        return None

    def _extract_favicon_asset_candidates(self, html, base_url):
        candidates = []
        pattern = re.compile(r'["\']([^"\']*favicon[^"\']*)["\']', re.IGNORECASE)
        order = 0
        for match in pattern.finditer(str(html or '')):
            href_value = str(match.group(1) or '').strip()
            if not href_value:
                continue
            lowered = href_value.lower()
            if not (
                lowered.endswith(('.svg', '.png', '.ico', '.webp', '.jpg', '.jpeg', '.avif'))
                or 'favicon?' in lowered
                or 'favicon.' in lowered
                or '/favicon' in lowered
            ):
                continue
            order += 1
            candidates.append(
                self._make_icon_candidate(
                    urljoin(base_url, href_value),
                    source_kind='root_fallback',
                    order=order,
                )
            )
        return candidates

    def _extract_manifest_icon_candidates(self, manifest_text, manifest_url):
        try:
            manifest = json.loads(manifest_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        candidates = []
        order = 0
        for icon in manifest.get('icons', []) or []:
            if not isinstance(icon, dict):
                continue
            src = str(icon.get('src') or '').strip()
            if not src:
                continue
            href = urljoin(manifest_url, src)
            purpose_value = str(icon.get('purpose') or '')
            order += 1
            candidates.append(
                self._make_icon_candidate(
                    href,
                    source_kind='manifest_maskable' if 'maskable' in purpose_value.lower() else 'manifest',
                    type_value=icon.get('type', ''),
                    sizes_value=icon.get('sizes', ''),
                    purpose_value=purpose_value,
                    order=order,
                )
            )
        return candidates

    def _extract_browserconfig_url(self, html, base_url):
        pattern = re.compile(r'<meta\b[^>]*>', re.IGNORECASE)
        for match in pattern.finditer(html):
            attrs = self._parse_html_tag_attributes(match.group(0))
            key = str(attrs.get('name') or '').lower().strip()
            content = str(attrs.get('content') or '').strip()
            if key == 'msapplication-config' and content:
                return urljoin(base_url, content)
        return None

    def _extract_browserconfig_icon_candidates(self, browserconfig_text, browserconfig_url):
        pattern = re.compile(r'<(square\d+x\d+logo|wide\d+x\d+logo|tileimage)\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
        candidates = []
        order = 0
        for match in pattern.finditer(browserconfig_text):
            tag_name = str(match.group(1) or '').lower()
            href = urljoin(browserconfig_url, str(match.group(2) or '').strip())
            sizes_value = ''
            size_match = re.search(r'(\d+x\d+)', tag_name)
            if size_match:
                sizes_value = size_match.group(1)
            order += 1
            candidates.append(
                self._make_icon_candidate(
                    href,
                    source_kind='browserconfig',
                    sizes_value=sizes_value,
                    order=order,
                )
            )
        return candidates

    def _extract_meta_image_candidates(self, html, base_url):
        candidates = []
        pattern = re.compile(r'<meta\b[^>]*>', re.IGNORECASE)
        supported_keys = {
            'og:image',
            'og:image:url',
            'og:image:secure_url',
            'twitter:image',
            'twitter:image:src',
            'msapplication-tileimage',
        }
        order = 0
        for match in pattern.finditer(html):
            attrs = self._parse_html_tag_attributes(match.group(0))
            content = str(attrs.get('content') or '').strip()
            if not content:
                continue
            key = str(attrs.get('property') or attrs.get('name') or '').lower().strip()
            if key not in supported_keys:
                continue
            href = urljoin(base_url, content)
            order += 1
            candidates.append(
                self._make_icon_candidate(
                    href,
                    source_kind='meta_image',
                    order=order,
                )
            )
        return candidates

    def _download_image_bytes(self, url):
        request = urllib.request.Request(url, headers={'User-Agent': self._icon_request_user_agent(), 'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9', 'Connection': 'close'})
        with urllib.request.urlopen(request, timeout=10) as response:
            content_length = response.headers.get('Content-Length')
            if content_length:
                try:
                    if int(content_length) > MAX_ICON_FILE_SIZE:
                        raise OSError('Icon download too large')
                except ValueError:
                    pass
            payload = response.read(MAX_ICON_FILE_SIZE + 1)
            if len(payload) > MAX_ICON_FILE_SIZE:
                raise OSError('Icon download too large')
            content_type = response.headers.get_content_type()
            return payload, content_type

    def _public_root_hosts_for_icon_fallback(self, host):
        host = (host or '').lower().strip()
        hosts = []
        if host:
            hosts.append(host)
        if host.startswith('www.'):
            hosts.append(host[4:])
        registrable = self._registrable_domain_host(host)
        if registrable:
            hosts.append(registrable)
        return [item for index, item in enumerate(hosts) if item and item not in hosts[:index]]

    def _special_icon_fallback_candidates(self, url):
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        path = parsed.path or ''
        candidates = []
        if host in {'www.google.com', 'google.com', 'maps.google.com'} and path.startswith('/maps'):
            candidates.extend([
                'https://www.google.com/images/branding/product/ico/maps15_bnuw3a_32dp.ico',
                'https://maps.google.com/favicon.ico',
            ])
        if host.endswith('booking.com'):
            candidates.extend([
                'https://cf.bstatic.com/static/img/favicon/9ca83ba2a5a3293ff07452cb24949a5843af4592.svg',
                'https://cf.bstatic.com/static/img/apple-touch-icon/5db9fd30d96b1796883ee94be7dddce50b73bb38.png',
                'https://cf.bstatic.com/static/img/favicon/40749a316c45e239a7149b6711ea4c48d10f8d89.ico',
            ])
        return candidates

    def _icon_source_page_candidates(self, url):
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return []
        candidates = []

        def add_candidate(value):
            value = str(value or '').strip()
            if value and value not in candidates:
                candidates.append(value)

        clean = parsed._replace(fragment='')
        add_candidate(urlunparse(clean))

        normalized = clean._replace(params='', query='')
        add_candidate(urlunparse(normalized))

        path = normalized.path or '/'
        if path not in ('', '/'):
            stripped_path = path.rstrip('/') or '/'
            if stripped_path != path:
                add_candidate(urlunparse(normalized._replace(path=stripped_path)))
            leaf = stripped_path.rsplit('/', 1)[-1]
            if '.' in leaf:
                parent_path = stripped_path.rsplit('/', 1)[0] or '/'
                add_candidate(urlunparse(normalized._replace(path=parent_path)))
            add_candidate(urlunparse(normalized._replace(path='/')))

        registrable = self._registrable_domain_host(parsed.hostname or '')
        if registrable and registrable != (parsed.hostname or '').lower().strip():
            add_candidate(urlunparse(normalized._replace(netloc=registrable, path='/', params='', query='', fragment='')))

        return candidates

    def _download_text_response(self, url, accept_header, timeout=8):
        request = urllib.request.Request(url, headers={'User-Agent': self._icon_request_user_agent(), 'Accept': accept_header, 'Accept-Language': 'en-US,en;q=0.9', 'Connection': 'close'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            content_length = response.headers.get('Content-Length')
            if content_length:
                try:
                    if int(content_length) > 512 * 1024:
                        raise OSError('Response too large for icon discovery')
                except ValueError:
                    pass
            body = response.read(512 * 1024 + 1)
            final_url = response.geturl()
        if len(body) > 512 * 1024:
            raise OSError('Response too large for icon discovery')
        return body.decode('utf-8', errors='ignore'), content_type, final_url

    def _download_favicon(self, url):
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None

        primary_icon_candidates = []
        fallback_meta_candidates = []
        final_url = url
        seen_document_urls = set()

        for document_url in self._icon_source_page_candidates(url):
            if document_url in seen_document_urls:
                continue
            seen_document_urls.add(document_url)
            try:
                html, content_type, resolved_final_url = self._download_text_response(document_url, 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8')
            except (OSError, urllib.error.URLError, ValueError):
                continue
            if 'html' not in content_type:
                continue
            final_url = resolved_final_url or document_url
            document_base_url = final_url
            base_href = self._extract_base_href(html, document_base_url)
            if base_href:
                document_base_url = base_href
            primary_icon_candidates.extend(self._extract_icon_candidates(html, document_base_url))
            primary_icon_candidates.extend(self._extract_favicon_asset_candidates(html, document_base_url))
            manifest_url = self._extract_manifest_url(html, document_base_url)
            if manifest_url:
                try:
                    manifest_text, manifest_content_type, resolved_manifest_url = self._download_text_response(manifest_url, 'application/manifest+json,application/json,text/plain;q=0.9,*/*;q=0.8')
                    if 'json' in manifest_content_type or manifest_text.lstrip().startswith('{'):
                        primary_icon_candidates.extend(self._extract_manifest_icon_candidates(manifest_text, resolved_manifest_url))
                except (OSError, urllib.error.URLError, ValueError):
                    pass
            browserconfig_url = self._extract_browserconfig_url(html, document_base_url)
            if browserconfig_url:
                try:
                    browserconfig_text, _browserconfig_content_type, resolved_browserconfig_url = self._download_text_response(browserconfig_url, 'application/xml,text/xml,text/plain;q=0.9,*/*;q=0.8')
                    primary_icon_candidates.extend(self._extract_browserconfig_icon_candidates(browserconfig_text, resolved_browserconfig_url))
                except (OSError, urllib.error.URLError, ValueError):
                    pass
            fallback_meta_candidates.extend(self._extract_meta_image_candidates(html, document_base_url))

        root_hosts = self._public_root_hosts_for_icon_fallback(urlparse(final_url).hostname or parsed.hostname or '')
        order = 0
        for host in root_hosts:
            for fallback in (
                '/favicon.svg',
                '/favicon.png',
                '/favicon-512x512.png',
                '/favicon-192x192.png',
                '/favicon-180x180.png',
                '/favicon-64x64.png',
                '/favicon-32x32.png',
                '/favicon-16x16.png',
                '/apple-touch-icon.png',
                '/apple-touch-icon-precomposed.png',
                '/apple-touch-icon-180x180.png',
                '/favicon.ico',
            ):
                href = f'https://{host}{fallback}'
                order += 1
                primary_icon_candidates.append(self._make_icon_candidate(href, source_kind='root_fallback', order=order))
        for href in self._special_icon_fallback_candidates(final_url):
            order += 1
            primary_icon_candidates.append(self._make_icon_candidate(href, source_kind='special_fallback', order=order))

        def _try_candidates(candidate_list):
            for icon_url in self._order_icon_candidates(candidate_list):
                try:
                    payload, content_type = self._download_image_bytes(icon_url)
                    temp_fd, temp_name = tempfile.mkstemp(suffix='.png')
                    os.close(temp_fd)
                    temp_target = Path(temp_name)
                    return normalize_icon_bytes_to_png(payload, temp_target, source_name=icon_url, content_type=content_type)
                except (OSError, ValueError, urllib.error.URLError, UnidentifiedImageError) as error:
                    if is_svg_support_missing_error(error):
                        raise
                    continue
            return None

        primary_result = _try_candidates(primary_icon_candidates)
        if primary_result is not None:
            return primary_result
        return _try_candidates(fallback_meta_candidates)

    def on_icon_clicked(self, button):
        self.page_stack.set_visible_child_name('icon')
        self._update_tabbed_navigation_state()
        self._update_export_button_state()
        GLib.idle_add(self._refresh_icon_page_after_open)

    def _refresh_icon_page_after_open(self):
        if self.is_icon_page_visible():
            self.refresh_icon_page()
        return False

    def is_icon_page_visible(self):
        return self.page_stack.get_visible_child_name() == 'icon'

    def on_icon_download_clicked(self, button):
        if self._icon_download_in_progress:
            return
        raw_url = self.address_entry.get_text().strip()
        if not self._looks_ready_for_url_check(raw_url):
            self.refresh_icon_page()
            self._update_export_button_state()
            return
        self._icon_download_in_progress = True
        self._set_icon_download_busy(True, t('icon_page_status_searching'))
        thread = threading.Thread(target=self._load_icon_from_url, args=(raw_url,), daemon=True)
        thread.start()

    def _present_choice_dialog(self, anchor, message, on_result, destructive=False):
        def handle_response(response_id):
            on_result(response_id == 'yes')

        root = self.get_root()
        if root is None:
            on_result(False)
            return

        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog.new(t('app_title'), message)
            dialog.add_response('no', t('dialog_no'))
            dialog.add_response('yes', t('dialog_yes'))
            dialog.set_default_response('no')
            dialog.set_close_response('no')
            if destructive:
                dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect('response', lambda _d, response: handle_response(response))
            dialog.present(root)
            return

        dialog = Adw.MessageDialog.new(root, t('app_title'), message)
        dialog.add_response('no', t('dialog_no'))
        dialog.add_response('yes', t('dialog_yes'))
        dialog.set_default_response('no')
        dialog.set_close_response('no')
        if destructive:
            dialog.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', lambda _d, response: handle_response(response))
        dialog.present()

    def on_icon_upload_clicked(self, button):
        self._icon_upload_dialog_active = True
        self._cancel_address_timers()
        self._cancel_initial_address_validation()
        self.open_icon_file_dialog()

    def on_icon_delete_clicked(self, button):
        self._present_choice_dialog(button, t('icon_delete_confirm'), lambda confirmed: self.delete_icon() if confirmed else None, destructive=True)

    def delete_icon(self):
        icon_path = self._icon_path()
        if icon_path:
            path = Path(icon_path)
            safe_name = f'webapp-entry-{self.entry.id}'
            managed_dir = get_managed_icon_path(self.entry.title, '.png', self.entry.id).parent
            if path.exists() and path.parent == managed_dir and (path.stem == build_safe_slug(self.entry.title) or path.name.startswith(safe_name)):
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    LOG.warning('Failed to delete icon %s: %s', path, error)
        self._set_option_value(ICON_PATH_KEY, '')
        self.icon_page_status.set_text(t('icon_page_status_deleted'))
        self.refresh_icon_preview()
        self.refresh_icon_page()
        self._update_export_button_state()
        self._emit_visual_changed()
        self.save_desktop_file()

    def _load_icon_from_url(self, url):
        try:
            for candidate_url in candidate_urls_for_input(url, prefer_https=True, include_http_fallback=True):
                try:
                    path = self._download_favicon(candidate_url)
                    if path and Path(path).exists():
                        GLib.idle_add(self._apply_downloaded_icon, str(path))
                        return
                except (OSError, ValueError, urllib.error.URLError) as error:
                    LOG.debug('Failed to download favicon for %s: %s', candidate_url, error)
                except Exception as error:
                    LOG.warning('Unexpected error while downloading favicon for %s: %s', candidate_url, error, exc_info=True)
        except Exception as error:
            LOG.warning('Unexpected icon download failure for %s: %s', url, error, exc_info=True)
        GLib.idle_add(self._finish_icon_download, t('icon_page_status_download_failed'))

    def _finish_icon_download(self, status_text=None):
        self._icon_download_in_progress = False
        self._compact_mode_override = None
        self._inline_editor_save_source_ids = {'css': 0, 'javascript': 0}
        self._set_icon_download_busy(False)
        self.refresh_icon_page()
        self.icon_download_button.set_sensitive(True)
        if status_text is not None:
            self.icon_page_status.set_text(status_text)
        self._update_export_button_state()
        return False

    def _set_icon_page_status(self, text):
        self.icon_page_status.set_text(text)
        self._update_export_button_state()
        return False

    def _apply_downloaded_icon(self, path):
        temp_path = Path(path)
        try:
            target_path = self._managed_icon_target()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_path), str(target_path))
            self._apply_icon_path(target_path)
        finally:
            temp_path.unlink(missing_ok=True)
        return self._finish_icon_download(t('icon_page_status_downloaded'))

    def _copy_gfile_to_temp_path(self, file_obj, suffix=''):
        if file_obj is None:
            return None
        local_path = file_obj.get_path()
        if local_path:
            return Path(local_path)
        tmp_name = None
        try:
            stream = file_obj.read(None)
            fd, tmp_name = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    while True:
                        chunk = stream.read_bytes(65536, None)
                        if not chunk:
                            break
                        handle.write(chunk)
            finally:
                try:
                    stream.close(None)
                except GLib.Error:
                    pass
            return Path(tmp_name)
        except (GLib.Error, OSError, AttributeError) as error:
            if tmp_name:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                uri = file_obj.get_uri()
            except GLib.Error:
                uri = ''
            LOG.warning('Failed to copy selected file %s: %s', uri, error)
            return None

    def _write_text_to_gfile(self, file_obj, text):
        if file_obj is None:
            return False
        payload = text.encode('utf-8')
        local_path = file_obj.get_path()
        if local_path:
            target = Path(local_path)
            if target.suffix.lower() != '.wapp':
                target = target.with_suffix('.wapp')
            target.write_bytes(payload)
            return True
        try:
            basename = file_obj.get_basename() or 'webapp.wapp'
            if not basename.lower().endswith('.wapp'):
                parent = file_obj.get_parent()
                if parent is not None:
                    file_obj = parent.get_child(f'{basename}.wapp')
            stream = file_obj.replace(None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
            stream.write_all(payload, None)
            stream.close(None)
            return True
        except (GLib.Error, OSError, AttributeError) as error:
            try:
                uri = file_obj.get_uri()
            except GLib.Error:
                uri = ''
            LOG.warning('Failed to write selected file %s: %s', uri, error)
            return False

    def _build_file_filter_store(self, patterns):
        store = Gio.ListStore.new(Gtk.FileFilter)
        first_filter = None
        for name, pattern in (patterns or []):
            filt = Gtk.FileFilter()
            filt.set_name(name)
            filt.add_pattern(pattern)
            store.append(filt)
            if first_filter is None:
                first_filter = filt
        return store, first_filter

    def _open_file_dialog(self, title, callback, patterns=None):
        patterns = patterns or []
        parent = self.get_root()
        if hasattr(Gtk, 'FileDialog'):
            dialog = Gtk.FileDialog(title=title, modal=True)
            filters, first_filter = self._build_file_filter_store(patterns)
            if filters.get_n_items() > 0:
                dialog.set_filters(filters)
            if first_filter is not None:
                dialog.set_default_filter(first_filter)

            def handle_open(_dialog, result):
                try:
                    file_obj = _dialog.open_finish(result)
                except GLib.Error:
                    callback(None, Gtk.ResponseType.CANCEL)
                    return
                callback(file_obj)

            dialog.open(parent, None, handle_open)
            return
        dialog = Gtk.FileChooserNative.new(
            title,
            parent,
            Gtk.FileChooserAction.OPEN,
            t('icon_dialog_open'),
            t('icon_dialog_cancel'),
        )
        first_filter = None
        for name, pattern in patterns:
            filt = Gtk.FileFilter()
            filt.set_name(name)
            filt.add_pattern(pattern)
            dialog.add_filter(filt)
            if first_filter is None:
                first_filter = filt
        if first_filter is not None:
            dialog.set_filter(first_filter)
        dialog.connect('response', callback)
        dialog.show()

    def _save_file_dialog(self, title, suggested_name, callback):
        parent = self.get_root()
        if hasattr(Gtk, 'FileDialog'):
            dialog = Gtk.FileDialog(title=title, modal=True, initial_name=suggested_name)

            def handle_save(_dialog, result):
                try:
                    file_obj = _dialog.save_finish(result)
                except GLib.Error:
                    callback(None, Gtk.ResponseType.CANCEL)
                    return
                callback(file_obj)

            dialog.save(parent, None, handle_save)
            return
        dialog = Gtk.FileChooserNative.new(
            title,
            parent,
            Gtk.FileChooserAction.SAVE,
            t('icon_dialog_open'),
            t('icon_dialog_cancel'),
        )
        dialog.set_current_name(suggested_name)
        dialog.connect('response', callback)
        dialog.show()

    def open_icon_file_dialog(self):
        self._open_file_dialog(t('icon_dialog_title'), self.on_icon_file_selected)
        return False

    def on_icon_file_selected(self, result, response=None):
        self._icon_upload_dialog_active = False
        if isinstance(result, Gio.File):
            file_obj = result
        else:
            dialog = result
            if response != Gtk.ResponseType.ACCEPT:
                if dialog is not None:
                    dialog.destroy()
                return
            file_obj = dialog.get_file()
            dialog.destroy()
        local_path = file_obj.get_path() if file_obj is not None else None
        temp_path = self._copy_gfile_to_temp_path(file_obj, '.icon')
        try:
            if temp_path is not None and self._store_icon_file(temp_path):
                self.icon_page_status.set_text(t('icon_page_status_uploaded'))
            else:
                self.icon_page_status.set_text(t('icon_page_status_upload_failed'))
            self.refresh_icon_page()
            self._update_export_button_state()
        finally:
            if temp_path is not None and (not local_path or str(temp_path) != local_path):
                temp_path.unlink(missing_ok=True)
