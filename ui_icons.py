from __future__ import annotations

from pathlib import Path
from gi.repository import Gdk, Gtk

from option_config import overview_status_definitions

APP_DIR = Path(__file__).resolve().parent


def load_icon_paintable(icon_path: Path):
    try:
        return Gdk.Texture.new_from_filename(str(icon_path))
    except Exception:
        return None


def create_image_from_ref(icon_ref: str, pixel_size: int = 16, fallback_icon: str = 'applications-internet-symbolic'):
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
