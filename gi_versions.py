"""Single place that pins the GObject-Introspection versions.

PyGObject resolves a typelib the first time `from gi.repository import X`
runs, and without a prior `gi.require_version` it takes whatever version it
finds -- printing a PyGIWarning and, where two versions are installed, quietly
picking the wrong one (GtkSource 4 next to 5 is the realistic case).

The declarations used to live in the two entry points, which was one import
order away from being useless: `detail_page/__init__.py` imports `.assets`
before `.page`, so the app really did load GtkSource unpinned. Every module
that touches `gi.repository` therefore imports this one first:

    import gi_versions  # noqa: F401

A missing typelib is not an error here -- the `from gi.repository import X`
that follows in the importing module raises the meaningful ImportError, and a
module that only needs GLib must not fail because libadwaita is absent.
"""

import gi

_REQUIRED_VERSIONS = (
    ('GLib', '2.0'),
    ('GObject', '2.0'),
    ('Gio', '2.0'),
    ('Gdk', '4.0'),
    ('Gtk', '4.0'),
    ('Adw', '1'),
    ('Pango', '1.0'),
    ('GdkPixbuf', '2.0'),
    # Optional: gates the code editor in the custom-asset dialog.
    ('GtkSource', '5'),
)

for _namespace, _version in _REQUIRED_VERSIONS:
    try:
        gi.require_version(_namespace, _version)
    except (ValueError, AttributeError):
        # Typelib absent, or a different version was already pinned by an
        # embedder. Either way the import in the calling module decides.
        pass
