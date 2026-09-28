"""The version the app shows and the one AppStream advertises must agree.

The app reads `APP_VERSION`; Flatpak front-ends (GNOME Software, Discover)
read the newest `<release>` in the metainfo. Bumping only one of them is how a
release ends up looking like "no update" on a phone.
"""

import ast
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METAINFO = ROOT / 'flatpak' / 'de.cais.webappmanager.metainfo.xml'


def _app_version():
    # Parsed rather than imported: importing app_identity creates the user's
    # config and data directories as a side effect.
    tree = ast.parse((ROOT / 'app_identity.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(target, 'id', None) == 'APP_VERSION' for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError('APP_VERSION not found in app_identity.py')


def _metainfo_versions():
    releases = ET.parse(METAINFO).getroot().find('releases')
    return [release.get('version') for release in releases.findall('release')]


class AppVersionTests(unittest.TestCase):
    def test_app_version_matches_newest_metainfo_release(self):
        self.assertEqual(_app_version(), _metainfo_versions()[0])

    def test_metainfo_releases_are_listed_newest_first(self):
        versions = [int(version) for version in _metainfo_versions()]
        self.assertEqual(versions, sorted(versions, reverse=True))
        self.assertEqual(len(versions), len(set(versions)))


if __name__ == '__main__':
    unittest.main()
