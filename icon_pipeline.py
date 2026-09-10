from pathlib import Path
from functools import lru_cache
import io

SVG_CAIRO_MISSING_ERROR = 'SVG support is unavailable: neither cairosvg nor a GdkPixbuf SVG loader is installed'


# Pillow and the cairosvg chain are only needed when an icon is actually
# imported, but this module is pulled in by desktop_entries and therefore
# by every start of the app. Importing them on first use keeps roughly the
# cost of loading PIL.Image (plus all of cairo/tinycss2 where cairosvg is
# installed) out of the startup path.
@lru_cache(maxsize=1)
def _pillow_image():
    from PIL import Image

    return Image


@lru_cache(maxsize=1)
def _cairosvg():
    try:
        import cairosvg
    except ImportError:
        return None
    return cairosvg


# Fallback for source installs without cairosvg: GTK already pulls in
# gdk-pixbuf, and on any desktop that ships librsvg its SVG loader is
# registered there. Sites that only publish an SVG favicon (increasingly
# common) would otherwise silently end up without an icon.
@lru_cache(maxsize=1)
def _gdk_pixbuf_svg():
    try:
        import gi

        gi.require_version('GdkPixbuf', '2.0')
        from gi.repository import Gio, GLib, GdkPixbuf
    except (ImportError, ValueError, AttributeError):
        return None
    try:
        has_svg_loader = any('svg' in (fmt.get_name() or '') for fmt in GdkPixbuf.Pixbuf.get_formats())
    except Exception:  # pragma: no cover - defensive, get_formats is not expected to fail
        return None
    if not has_svg_loader:
        return None
    return GdkPixbuf, Gio, GLib

from input_validation import build_safe_slug, validate_icon_source_path
from webapp_constants import APPLICATIONS_DIR, ICON_THEME_APPS_DIR

def _get_managed_icon_stem(title, entry_id=None):
    safe_slug = build_safe_slug(title)
    if safe_slug:
        return safe_slug
    if entry_id not in (None, ''):
        return f'webapp-entry-{entry_id}'
    return 'webapp'

def get_managed_icon_name(title, entry_id=None):
    return _get_managed_icon_stem(title, entry_id)

def get_managed_icon_path(title, extension='.png', entry_id=None):
    extension = extension if extension.startswith('.') else f'.{extension}'
    return APPLICATIONS_DIR / f'{_get_managed_icon_stem(title, entry_id)}{extension}'

def get_managed_theme_icon_path(title, extension='.png', entry_id=None):
    extension = extension if extension.startswith('.') else f'.{extension}'
    return ICON_THEME_APPS_DIR / f'{_get_managed_icon_stem(title, entry_id)}{extension}'

def ensure_applications_dir():
    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    ICON_THEME_APPS_DIR.mkdir(parents=True, exist_ok=True)

def _looks_like_svg(payload: bytes) -> bool:
    if not payload:
        return False
    head = payload[:512].lstrip()
    if head.startswith(b'<?xml') or head.startswith(b'<svg'):
        return True
    try:
        decoded = head.decode('utf-8', errors='ignore').lower()
    except (UnicodeDecodeError, AttributeError):
        return False
    return '<svg' in decoded


def svg_support_available():
    return _cairosvg() is not None or _gdk_pixbuf_svg() is not None


def is_svg_support_missing_error(error):
    return SVG_CAIRO_MISSING_ERROR in str(error or '')


def _render_svg_with_gdk_pixbuf(svg_bytes, target_path):
    GdkPixbuf, Gio, GLib = _gdk_pixbuf_svg()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # No base URI is handed to the loader, so librsvg has nothing to resolve a
    # relative reference against, and it never speaks http(s) itself -- the
    # same containment cairosvg's unsafe=False gives us.
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(svg_bytes))
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, 256, 256, True, None)
    except GLib.Error as error:
        raise OSError(f'SVG could not be rendered: {error.message}') from error
    finally:
        stream.close(None)
    if pixbuf is None:
        raise OSError('SVG could not be rendered')
    ok, buffer = pixbuf.save_to_bufferv('png', [], [])
    if not ok:
        raise OSError('SVG could not be encoded as PNG')
    target_path.write_bytes(bytes(buffer))
    return target_path


def _render_svg_bytes_to_png(svg_bytes, target_path):
    cairosvg = _cairosvg()
    if cairosvg is None:
        if _gdk_pixbuf_svg() is not None:
            return _render_svg_with_gdk_pixbuf(svg_bytes, target_path)
        raise OSError(SVG_CAIRO_MISSING_ERROR)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # unsafe=False is what actually contains a hostile SVG: it makes cairosvg
    # refuse XML entities (no XXE) and skip every external resource, so an
    # <image xlink:href="http://..."> is never fetched -- no tracking pixel, no
    # request to a link-local metadata endpoint. It is cairosvg's default, but
    # it is stated explicitly because the whole SVG import path depends on it.
    #
    # This used to pass url_fetcher=<blocking callback>, which cairosvg has no
    # such parameter for (it is a WeasyPrint concept). Every SVG import raised
    # TypeError, and the "protection" it looked like was never in effect.
    cairosvg.svg2png(
        bytestring=svg_bytes,
        write_to=str(target_path),
        output_width=256,
        output_height=256,
        unsafe=False,
    )
    return target_path


def normalize_icon_bytes_to_png(payload, target_path, source_name='', content_type=''):
    if not payload:
        raise OSError('Empty icon payload')
    target_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = Path(source_name or '').suffix.lower()
    is_svg = suffix == '.svg' or 'image/svg+xml' in str(content_type or '').lower() or _looks_like_svg(payload)
    if is_svg:
        return _render_svg_bytes_to_png(payload, target_path)
    with _pillow_image().open(io.BytesIO(payload)) as image:
        image.load()
        rgba = image.convert('RGBA')
        rgba.save(target_path, 'PNG')
    return target_path


def normalize_icon_to_png(source_path, target_path):
    validated_source = validate_icon_source_path(source_path)
    if validated_source is None:
        raise OSError('Invalid icon source path')
    return normalize_icon_bytes_to_png(
        validated_source.read_bytes(),
        target_path,
        source_name=validated_source.name,
        content_type='image/svg+xml' if validated_source.suffix.lower() == '.svg' else '',
    )

def _allowed_managed_icon_stems(entry_id, title=''):
    slug = build_safe_slug(title)
    allowed = {f'webapp-entry-{entry_id}'}
    if slug:
        allowed.add(slug)
    return {item.lower() for item in allowed}

def _is_safe_managed_icon_path(path, entry_id, title=''):
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    allowed_parents = {APPLICATIONS_DIR.resolve(), ICON_THEME_APPS_DIR.resolve()}
    if resolved.parent not in allowed_parents:
        return False
    return resolved.stem.lower() in _allowed_managed_icon_stems(entry_id, title)
