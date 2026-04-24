import logging
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from input_validation import DESKTOP_CHROME_USER_AGENT

def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.sorting.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from detail_page import DetailPage
from mainwindow import MainWindowEntriesMixin


class _DummyStore:
    def __init__(self, items=None):
        self._items = list(items or [])

    def get_n_items(self):
        return len(self._items)

    def get_item(self, index):
        return self._items[index]

    def insert(self, index, item):
        self._items.insert(index, item)

    def append(self, item):
        self._items.append(item)

    def remove(self, index):
        self._items.pop(index)


class _EntryStoreHarness(MainWindowEntriesMixin):
    def __init__(self, items=None):
        self.entries_store = _DummyStore(items)


class EntrySortingTests(unittest.TestCase):
    def test_insert_entry_sorted_places_new_entry_alphabetically(self):
        harness = _EntryStoreHarness(
            [
                SimpleNamespace(id=1, title='Alpha'),
                SimpleNamespace(id=3, title='Delta'),
            ]
        )

        harness._insert_entry_sorted(SimpleNamespace(id=2, title='Beta'))

        self.assertEqual(['Alpha', 'Beta', 'Delta'], [item.title for item in harness.entries_store._items])

    def test_reposition_entry_in_store_moves_renamed_entry(self):
        renamed = SimpleNamespace(id=3, title='Aardvark')
        harness = _EntryStoreHarness(
            [
                SimpleNamespace(id=1, title='Bravo'),
                SimpleNamespace(id=2, title='Charlie'),
                renamed,
            ]
        )

        harness._reposition_entry_in_store(renamed)

        self.assertEqual(['Aardvark', 'Bravo', 'Charlie'], [item.title for item in harness.entries_store._items])


class UrlReadinessTests(unittest.TestCase):
    def test_url_check_accepts_trailing_slash(self):
        detail_page = DetailPage.__new__(DetailPage)

        self.assertTrue(
            DetailPage._looks_ready_for_url_check(
                detail_page,
                'https://www.adac.de/verkehr/tanken-kraftstoff-antrieb/kraftstoffpreise/',
            )
        )

    def test_root_host_fallback_includes_registrable_domain_last(self):
        detail_page = DetailPage.__new__(DetailPage)

        hosts = DetailPage._public_root_hosts_for_icon_fallback(detail_page, 'www.foo.bar.example.com')

        self.assertEqual(hosts, ['www.foo.bar.example.com', 'foo.bar.example.com', 'example.com'])

    def test_icon_source_candidates_include_registrable_domain_root_last(self):
        detail_page = DetailPage.__new__(DetailPage)

        candidates = DetailPage._icon_source_page_candidates(
            detail_page,
            'https://www.foo.bar.example.com/path/to/app/',
        )

        self.assertEqual(candidates[-1], 'https://example.com/')

    def test_extract_favicon_asset_candidates_finds_direct_asset_paths(self):
        detail_page = DetailPage.__new__(DetailPage)
        html = (
            '<link data-rh="true" rel="icon" type="image/svg+xml" href="/assets/ui/favicon.svg"/>'
            '<link data-rh="true" rel="icon" type="image/png" sizes="32x32" href="/assets/ui/favicon-32x32.png"/>'
            '<link data-rh="true" rel="shortcut icon" href="/assets/ui/favicon.ico"/>'
        )

        candidates = DetailPage._extract_favicon_asset_candidates(detail_page, html, 'https://www.adac.de/')

        self.assertEqual(
            [candidate['href'] for candidate in candidates],
            [
                'https://www.adac.de/assets/ui/favicon.svg',
                'https://www.adac.de/assets/ui/favicon-32x32.png',
                'https://www.adac.de/assets/ui/favicon.ico',
            ],
        )

    def test_icon_request_user_agent_prefers_configured_value(self):
        detail_page = DetailPage.__new__(DetailPage)
        detail_page._get_option_value = lambda key: 'Custom Agent/1.0' if key == 'UserAgent' or key == 'UserAgentValue' else 'Custom Agent/1.0'

        with mock.patch('detail_page.icon.USER_AGENT_VALUE_KEY', 'UserAgentValue'):
            self.assertEqual(DetailPage._icon_request_user_agent(detail_page), 'Custom Agent/1.0')

    def test_icon_request_user_agent_falls_back_to_default(self):
        detail_page = DetailPage.__new__(DetailPage)
        detail_page._get_option_value = lambda key: ''

        self.assertEqual(DetailPage._icon_request_user_agent(detail_page), DESKTOP_CHROME_USER_AGENT)


if __name__ == '__main__':
    unittest.main()
