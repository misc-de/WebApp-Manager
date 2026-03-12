from pathlib import Path
import io

from PIL import Image

try:
    import cairosvg
except Exception:
    cairosvg = None

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
    except Exception:
        return False
    return '<svg' in decoded


def _render_svg_bytes_to_png(svg_bytes, target_path):
    if cairosvg is None:
        raise OSError('SVG support is unavailable')
    target_path.parent.mkdir(parents=True, exist_ok=True)
    cairosvg.svg2png(bytestring=svg_bytes, write_to=str(target_path), output_width=256, output_height=256)
    return target_path


def normalize_icon_bytes_to_png(payload, target_path, source_name='', content_type=''):
    if not payload:
        raise OSError('Empty icon payload')
    target_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = Path(source_name or '').suffix.lower()
    is_svg = suffix == '.svg' or 'image/svg+xml' in str(content_type or '').lower() or _looks_like_svg(payload)
    if is_svg:
        return _render_svg_bytes_to_png(payload, target_path)
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        image = image.convert('RGBA')
        image.save(target_path, 'PNG')
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
