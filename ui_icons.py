from __future__ import annotations

from pathlib import Path
import gi_versions  # noqa: F401 -- pins the typelib versions before gi.repository loads
from gi.repository import Gdk, Gtk, GLib

from app_identity import APP_ICON_NAME
from option_config import overview_status_definitions

APP_DIR = Path(__file__).resolve().parent


# Gtk.ListView recycles rows, so binding one re-creates its icon widget. The
# overview used to decode the PNG from disk on every one of those binds, which
# is what made scrolling a longer list stutter. A Gdk.Texture is immutable and
# can be shared by any number of widgets, so the decoded result is kept here,
# keyed by the file's identity *and* its mtime so an edited icon still shows up.
_TEXTURE_CACHE: dict[tuple[str, int, int], object] = {}
_TEXTURE_CACHE_LIMIT = 128


def _texture_cache_key(icon_path: Path):
    try:
        stat_result = icon_path.stat()
    except OSError:
        return None
    return (str(icon_path), stat_result.st_mtime_ns, stat_result.st_size)


def load_icon_paintable(icon_path: Path):
    cache_key = _texture_cache_key(Path(icon_path))
    if cache_key is not None:
        cached = _TEXTURE_CACHE.get(cache_key)
        if cached is not None:
            return cached
    try:
        texture = Gdk.Texture.new_from_filename(str(icon_path))
    except GLib.Error:
        return None
    if cache_key is not None:
        if len(_TEXTURE_CACHE) >= _TEXTURE_CACHE_LIMIT:
            _TEXTURE_CACHE.clear()
        _TEXTURE_CACHE[cache_key] = texture
    return texture


def create_image_from_ref(icon_ref: str, pixel_size: int = 16, fallback_icon: str = APP_ICON_NAME):
    icon_ref = (icon_ref or '').strip()
    if icon_ref:
        icon_path = Path(icon_ref).expanduser()
        if icon_path.exists():
            texture = load_icon_paintable(icon_path)
            if texture is not None:
                picture = Gtk.Picture.new_for_paintable(texture)
                picture.set_size_request(pixel_size, pixel_size)
                picture.set_can_shrink(True)
                picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                picture.set_halign(Gtk.Align.CENTER)
                picture.set_valign(Gtk.Align.CENTER)
                return picture
            image = Gtk.Image.new_from_file(str(icon_path))
            image.set_pixel_size(pixel_size)
            image.set_halign(Gtk.Align.CENTER)
            image.set_valign(Gtk.Align.CENTER)
            return image
        image = Gtk.Image.new_from_icon_name(icon_ref)
        image.set_pixel_size(pixel_size)
        image.set_halign(Gtk.Align.CENTER)
        image.set_valign(Gtk.Align.CENTER)
        return image
    image = Gtk.Image.new_from_icon_name(fallback_icon)
    image.set_pixel_size(pixel_size)
    image.set_halign(Gtk.Align.CENTER)
    image.set_valign(Gtk.Align.CENTER)
    return image


def active_status_icons(options: dict[str, str]) -> list[tuple[str, str]]:
    icons: list[tuple[str, str]] = []
    for option_key, relative_icon_path, tooltip in overview_status_definitions():
        if options.get(option_key, '0') == '1':
            icons.append((str(APP_DIR / relative_icon_path), tooltip))
    return icons
