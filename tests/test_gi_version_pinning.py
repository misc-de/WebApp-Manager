"""Every module that imports from gi.repository must pin the typelib versions
first.

PyGObject resolves a namespace on the first import and warns -- or silently
picks the wrong version -- when no gi.require_version ran before it. Declaring
that in the entry points was not enough: detail_page/__init__.py imports
.assets before .page, so GtkSource was loaded unpinned in the real app. This
guards the arrangement that replaced it.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# tests/ stubs modules of its own, and build-dir/ plus every dot-directory
# (.flatpak-build/, .flatpak-builder/, .ci-artifacts/) hold flatpak-builder's
# copies of an older tree -- scanning those would report the state of a past
# build, not of the code.
SKIPPED_DIRS = {'tests', 'build-dir', 'repo'}
GUARD_MODULE = 'gi_versions'


def _python_sources():
    for path in sorted(REPO_ROOT.rglob('*.py')):
        parts = path.relative_to(REPO_ROOT).parts
        if any(part in SKIPPED_DIRS or part.startswith('.') for part in parts):
            continue
        yield path


def _first_repository_import_line(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or '').startswith('gi.repository'):
            return node.lineno
    return None


def _guard_import_lines(tree):
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == GUARD_MODULE:
                    lines.append(node.lineno)
    return lines


class GiVersionPinningTests(unittest.TestCase):
    def test_repository_imports_are_preceded_by_the_guard(self):
        offenders = []
        for path in _python_sources():
            if path.name == f'{GUARD_MODULE}.py':
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            repository_line = _first_repository_import_line(tree)
            if repository_line is None:
                continue
            guard_lines = [line for line in _guard_import_lines(tree) if line < repository_line]
            if not guard_lines:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [], f'these modules import gi.repository without importing {GUARD_MODULE} first: {offenders}')

    def test_guard_pins_every_namespace_the_app_imports(self):
        guard_source = (REPO_ROOT / f'{GUARD_MODULE}.py').read_text(encoding='utf-8')
        pinned = set()
        for node in ast.walk(ast.parse(guard_source)):
            if isinstance(node, ast.Tuple) and len(node.elts) == 2:
                namespace, version = node.elts
                if isinstance(namespace, ast.Constant) and isinstance(version, ast.Constant):
                    pinned.add(namespace.value)

        used = set()
        for path in _python_sources():
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or '') == 'gi.repository':
                    used.update(alias.name for alias in node.names)

        self.assertTrue(used, 'no gi.repository imports found -- the scan is broken, not the code')
        self.assertEqual(sorted(used - pinned), [], f'{GUARD_MODULE} does not pin: {sorted(used - pinned)}')


if __name__ == '__main__':
    unittest.main()
