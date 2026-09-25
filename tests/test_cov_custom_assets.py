"""Coverage tests for custom_assets: the asset library, option encoding and
the per-browser customizer writers.

Every test points ASSET_LIBRARY_DIR at a temporary directory and replaces the
app-config accessors with an in-memory dict, so nothing under the real HOME is
read or written.
"""
import json
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.cov_custom_assets.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

import custom_assets as ca  # noqa: E402
from webapp_constants import DEFAULT_ZOOM_KEY  # noqa: E402


class _LibraryFixture(unittest.TestCase):
    """Temporary asset directory plus an in-memory app config."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.library_dir = self.root / 'assets'
        self.library_dir.mkdir()
        self.config = {}
        self.saved = []

        def fake_get():
            return json.loads(json.dumps(self.config))

        def fake_save(data):
            self.config = json.loads(json.dumps(data))
            self.saved.append(self.config)
            return self.config

        patches = [
            mock.patch.object(ca, 'ASSET_LIBRARY_DIR', self.library_dir),
            mock.patch.object(ca, 'get_app_config', side_effect=fake_get),
            mock.patch.object(ca, 'save_app_config', side_effect=fake_save),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def make_source(self, name, text):
        path = self.root / 'src' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def set_library(self, items):
        self.config = {'settings': {'custom_assets': items}}

    def write_asset_file(self, asset_type, filename, text):
        path = self.library_dir / asset_type / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path


class ZoomTests(unittest.TestCase):
    def test_known_zoom_values_pass_through(self):
        self.assertEqual(ca.managed_default_zoom_value({DEFAULT_ZOOM_KEY: ' 125 '}), '125')

    def test_unknown_or_missing_zoom_falls_back_to_100(self):
        self.assertEqual(ca.managed_default_zoom_value({DEFAULT_ZOOM_KEY: '333'}), '100')
        self.assertEqual(ca.managed_default_zoom_value(None), '100')
        self.assertEqual(ca.managed_default_zoom_value({DEFAULT_ZOOM_KEY: ''}), '100')

    def test_zoom_css_only_emitted_for_non_default(self):
        self.assertEqual(ca._managed_zoom_css({DEFAULT_ZOOM_KEY: '100'}), '')
        css = ca._managed_zoom_css({DEFAULT_ZOOM_KEY: '150'})
        self.assertIn('zoom: 150%', css)
        self.assertIn('@supports', css)


class LibraryMetadataTests(_LibraryFixture):
    def test_settings_dict_replaces_non_dict_settings(self):
        config, settings = ca._settings_dict({'settings': 'broken', 'x': 1})
        self.assertEqual(settings, {})
        self.assertIs(config['settings'], settings)
        self.assertEqual(config['x'], 1)

    def test_settings_dict_reads_app_config_when_none_given(self):
        self.config = {'settings': {'a': 1}}
        _config, settings = ca._settings_dict()
        self.assertEqual(settings, {'a': 1})

    def test_invalid_library_items_are_dropped(self):
        settings = {'custom_assets': [
            'not a dict',
            {'id': '', 'type': 'css', 'filename': 'a.css'},
            {'id': 'a', 'type': 'python', 'filename': 'a.py'},
            {'id': 'b', 'type': 'css', 'filename': ''},
            {'id': 'c', 'type': ' CSS ', 'filename': 'c.css', 'sha256': ' ABC '},
            {'id': 'c', 'type': 'css', 'filename': 'dup.css'},
        ]}
        result = ca._library_metadata(settings)
        self.assertEqual(result, [{
            'id': 'c', 'name': 'c.css', 'type': 'css', 'filename': 'c.css',
            'imported_at': '', 'sha256': 'abc',
        }])

    def test_non_list_library_yields_empty(self):
        self.assertEqual(ca._library_metadata({'custom_assets': {'x': 1}}), [])

    def test_library_metadata_without_settings_reads_config(self):
        self.set_library([{'id': 'x', 'type': 'javascript', 'filename': 'x.js', 'name': 'X'}])
        self.assertEqual([item['id'] for item in ca._library_metadata()], ['x'])

    def test_save_library_metadata_persists_normalised_fields(self):
        result = ca._save_library_metadata([{'id': 'a', 'name': 'A', 'type': 'css', 'filename': 'a.css', 'sha256': 'FF', 'path': '/ignored'}])
        self.assertEqual(result[0]['id'], 'a')
        stored = self.config['settings']['custom_assets'][0]
        self.assertEqual(stored, {'id': 'a', 'name': 'A', 'type': 'css', 'filename': 'a.css', 'imported_at': '', 'sha256': 'ff'})
        self.assertNotIn('path', stored)

    def test_list_custom_assets_sorted_with_paths(self):
        self.set_library([
            {'id': '2', 'type': 'css', 'filename': 'b.css', 'name': 'beta'},
            {'id': '1', 'type': 'javascript', 'filename': 'a.js', 'name': 'Alpha'},
        ])
        items = ca.list_custom_assets()
        self.assertEqual([item['name'] for item in items], ['Alpha', 'beta'])
        self.assertEqual(items[0]['path'], str(self.library_dir / 'javascript' / 'a.js'))

    def test_asset_file_path_handles_none(self):
        self.assertEqual(ca.asset_file_path(None), self.library_dir / '' / '')

    def test_get_custom_asset(self):
        self.set_library([{'id': 'k', 'type': 'css', 'filename': 'k.css'}])
        self.assertIsNone(ca.get_custom_asset('  '))
        self.assertIsNone(ca.get_custom_asset('missing'))
        found = ca.get_custom_asset(' k ')
        self.assertEqual(found['path'], str(self.library_dir / 'css' / 'k.css'))


class HashAndIntegrityTests(_LibraryFixture):
    def test_text_hash_normalises_line_endings(self):
        self.assertEqual(ca.asset_content_sha256_from_text('a\r\nb\rc'), ca.asset_content_sha256_from_text('a\nb\nc'))
        self.assertEqual(ca.asset_content_sha256_from_text(None), ca._sha256_bytes(b''))

    def test_file_hash_matches_bytes_hash(self):
        path = self.make_source('x.css', 'body{}')
        self.assertEqual(ca.asset_file_sha256(path), ca._sha256_bytes(b'body{}'))

    def test_inline_hash_for_options(self):
        self.assertEqual(ca.inline_asset_hash_for_options({ca.INLINE_CUSTOM_CSS_HASH_KEY: ' AB '}, 'css'), 'ab')
        self.assertEqual(ca.inline_asset_hash_for_options(None, 'javascript'), '')
        self.assertEqual(ca.inline_asset_hash_for_options({}, 'python'), '')

    def test_verify_without_expected_hash_passes(self):
        self.assertTrue(ca.verify_asset_integrity({'path': '/nonexistent', 'sha256': ''}))

    def test_verify_reports_unreadable_file(self):
        logger = mock.Mock()
        self.assertFalse(ca.verify_asset_integrity({'path': str(self.root / 'missing'), 'sha256': 'aa'}, logger=logger))
        logger.warning.assert_called_once()
        # Without a logger the failure is still reported through the result.
        self.assertFalse(ca.verify_asset_integrity({'path': str(self.root / 'missing'), 'sha256': 'aa'}))

    def test_verify_detects_mismatch(self):
        path = self.make_source('y.css', 'a')
        logger = mock.Mock()
        self.assertFalse(ca.verify_asset_integrity({'path': str(path), 'sha256': 'deadbeef'}, logger=logger))
        logger.warning.assert_called_once()
        self.assertFalse(ca.verify_asset_integrity({'path': str(path), 'sha256': 'deadbeef'}))
        good = ca.asset_file_sha256(path).upper()
        self.assertTrue(ca.verify_asset_integrity({'path': str(path), 'sha256': good}))


class ImportRemoveTests(_LibraryFixture):
    def test_import_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            ca.import_custom_asset(self.root / 'nope.css')
        with self.assertRaises(FileNotFoundError):
            ca.import_custom_asset(self.root)

    def test_import_unsupported_type_raises(self):
        with self.assertRaisesRegex(ValueError, 'unsupported-asset-type'):
            ca.import_custom_asset(self.make_source('x.txt', 'x'))

    def test_import_copies_file_and_records_metadata(self):
        source = self.make_source('Theme.CSS', 'body{color:red}')
        asset = ca.import_custom_asset(source)
        self.assertEqual(asset['type'], 'css')
        self.assertEqual(asset['name'], 'Theme.CSS')
        self.assertTrue(asset['filename'].endswith('.css'))
        target = Path(asset['path'])
        self.assertEqual(target.parent, self.library_dir / 'css')
        self.assertEqual(target.read_text(), 'body{color:red}')
        self.assertEqual(asset['sha256'], ca.asset_file_sha256(target))
        stored = self.config['settings']['custom_assets']
        self.assertEqual([item['id'] for item in stored], [asset['id']])

    def test_remove_unknown_or_empty_id(self):
        self.assertIsNone(ca.remove_custom_asset(''))
        self.set_library([{'id': 'a', 'type': 'css', 'filename': 'a.css'}])
        self.assertIsNone(ca.remove_custom_asset('b'))
        self.assertEqual(self.saved, [])

    def test_remove_deletes_file_and_metadata(self):
        self.set_library([
            {'id': 'a', 'type': 'css', 'filename': 'a.css'},
            {'id': 'b', 'type': 'css', 'filename': 'b.css'},
        ])
        path = self.write_asset_file('css', 'a.css', 'x')
        removed = ca.remove_custom_asset('a')
        self.assertEqual(removed['id'], 'a')
        self.assertFalse(path.exists())
        self.assertEqual([item['id'] for item in self.config['settings']['custom_assets']], ['b'])

    def test_remove_still_updates_metadata_when_unlink_fails(self):
        self.set_library([{'id': 'a', 'type': 'css', 'filename': 'a.css'}])
        with mock.patch.object(Path, 'unlink', side_effect=OSError('busy')):
            removed = ca.remove_custom_asset('a')
        self.assertEqual(removed['id'], 'a')
        self.assertEqual(self.config['settings']['custom_assets'], [])


class LinkedIdEncodingTests(_LibraryFixture):
    def test_decode_variants(self):
        self.assertEqual(ca._decode_raw_asset_ids(None), [])
        self.assertEqual(ca._decode_raw_asset_ids(''), [])
        self.assertEqual(ca._decode_raw_asset_ids('   '), [])
        self.assertEqual(ca._decode_raw_asset_ids(['a', ' a ', None, 'b']), ['a', 'b'])
        self.assertEqual(ca._decode_raw_asset_ids('["x", "y", "x"]'), ['x', 'y'])
        self.assertEqual(ca._decode_raw_asset_ids('"solo"'), ['solo'])
        self.assertEqual(ca._decode_raw_asset_ids('a, b ,,a'), ['a', 'b'])

    def test_normalize_filters_unknown_and_wrong_type(self):
        self.set_library([
            {'id': 'c1', 'type': 'css', 'filename': 'c1.css'},
            {'id': 'j1', 'type': 'javascript', 'filename': 'j1.js'},
        ])
        self.assertEqual(ca.normalize_linked_asset_ids('["c1","j1","zz"]'), ['c1', 'j1'])
        self.assertEqual(ca.normalize_linked_asset_ids('["c1","j1"]', asset_type='css'), ['c1'])
        self.assertEqual(json.loads(ca.encode_linked_asset_ids(['j1', 'c1'], asset_type='javascript')), ['j1'])


class InlineAndRuntimeTests(_LibraryFixture):
    def test_normalize_inline_text(self):
        self.assertEqual(ca._normalize_inline_asset_text(None), '')
        self.assertEqual(ca._normalize_inline_asset_text('  \n '), '')
        self.assertEqual(ca._normalize_inline_asset_text('a\r\nb'), 'a\nb')

    def test_inline_text_for_options(self):
        options = {ca.INLINE_CUSTOM_CSS_KEY: 'body{}', ca.INLINE_CUSTOM_JS_KEY: ' '}
        self.assertEqual(ca.inline_asset_text_for_options(options, 'css'), 'body{}')
        self.assertEqual(ca.inline_asset_text_for_options(options), {'css': 'body{}', 'javascript': ''})
        self.assertEqual(ca.inline_asset_text_for_options(None), {'css': '', 'javascript': ''})

    def test_has_runtime_customizations(self):
        self.assertFalse(ca.has_runtime_customizations({}))
        self.assertTrue(ca.has_runtime_customizations({ca.INLINE_CUSTOM_JS_KEY: 'x()'}))
        self.assertTrue(ca.has_runtime_customizations({DEFAULT_ZOOM_KEY: '80'}))
        with mock.patch.object(ca, 'linked_assets_for_options', return_value=[{'id': 'a'}]):
            self.assertTrue(ca.has_runtime_customizations({}))

    def test_linked_assets_skip_missing_and_corrupt_files(self):
        good = self.write_asset_file('css', 'good.css', 'a{}')
        bad = self.write_asset_file('css', 'bad.css', 'b{}')
        self.set_library([
            {'id': 'good', 'type': 'css', 'filename': 'good.css', 'sha256': ca.asset_file_sha256(good)},
            {'id': 'bad', 'type': 'css', 'filename': 'bad.css', 'sha256': 'mismatch'},
            {'id': 'gone', 'type': 'css', 'filename': 'gone.css'},
            {'id': 'js', 'type': 'javascript', 'filename': 'j.js'},
        ])
        self.assertTrue(bad.exists())
        options = {ca.CUSTOM_CSS_LINKS_KEY: '["good","bad","gone"]', ca.CUSTOM_JS_LINKS_KEY: '["js"]'}
        result = ca.linked_assets_for_options(options, 'css')
        self.assertEqual([item['id'] for item in result], ['good'])
        self.assertEqual(result[0]['path'], str(good))
        # Without a type both families are consulted; the JS file does not exist.
        self.assertEqual([item['id'] for item in ca.linked_assets_for_options(options)], ['good'])

    def test_linked_assets_skip_ids_missing_from_library_snapshot(self):
        # normalize_linked_asset_ids and the library snapshot are read separately;
        # an id that vanishes between the two reads is skipped, not crashed on.
        self.set_library([{'id': 'a', 'type': 'css', 'filename': 'a.css'}])
        with mock.patch.object(ca, 'normalize_linked_asset_ids', return_value=['ghost']):
            self.assertEqual(ca.linked_assets_for_options({ca.CUSTOM_CSS_LINKS_KEY: 'x'}, 'css'), [])


class DatabaseReferenceTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE options (entry_id INTEGER, option_key TEXT, option_value TEXT)')
        rows = [
            (1, ca.CUSTOM_CSS_LINKS_KEY, '["a","b"]'),
            (2, ca.CUSTOM_JS_LINKS_KEY, '["a"]'),
            (3, ca.CUSTOM_CSS_LINKS_KEY, '["c"]'),
            (4, 'Other', '["a"]'),
        ]
        conn.executemany('INSERT INTO options VALUES (?, ?, ?)', rows)
        self.db = types.SimpleNamespace(cursor=conn.cursor(), add_option=mock.Mock())

    def test_count_references(self):
        self.assertEqual(ca.count_asset_references(self.db, 'a'), 2)
        self.assertEqual(ca.count_asset_references(self.db, 'zz'), 0)
        self.assertEqual(ca.count_asset_references(self.db, ' '), 0)

    def test_detach_rewrites_only_affected_rows(self):
        self.assertEqual(ca.detach_asset_from_entries(self.db, ''), [])
        affected = ca.detach_asset_from_entries(self.db, 'a')
        self.assertEqual(affected, [1, 2])
        self.db.add_option.assert_any_call(1, ca.CUSTOM_CSS_LINKS_KEY, '["b"]')
        self.db.add_option.assert_any_call(2, ca.CUSTOM_JS_LINKS_KEY, '[]')
        self.assertEqual(self.db.add_option.call_count, 2)


class SmallHelperTests(unittest.TestCase):
    def test_format_asset_date(self):
        self.assertEqual(ca.format_asset_date(None), '')
        self.assertEqual(ca.format_asset_date('garbage'), 'garbage')
        formatted = ca.format_asset_date('2024-01-02T03:04:05Z')
        self.assertRegex(formatted, r'^2024-01-0[12] \d\d:\d\d$')

    def test_scope_and_matches(self):
        self.assertEqual(ca._css_scope_start('https://Example.org/path'), 'https://Example.org/')
        self.assertEqual(ca._css_scope_start('ftp://x'), '')
        self.assertEqual(ca._css_scope_start('http://'), '')
        self.assertEqual(ca._css_scope_start(None), '')
        self.assertEqual(ca._content_script_matches('http://a.b/c'), ['http://a.b/*'])
        self.assertEqual(ca._content_script_matches('file:///x'), [])
        self.assertEqual(ca._content_script_matches('https://'), [])

    def test_sanitize_extension_filename(self):
        self.assertEqual(ca._sanitize_extension_filename('id', 'my file!.css', '-1.css'), 'my-file-1.css')
        self.assertEqual(ca._sanitize_extension_filename('id9', '', '-2.js'), 'id9-2.js')
        self.assertEqual(ca._sanitize_extension_filename('fallback', '!!!.js', '-3.js'), 'fallback-3.js')

    def test_firefox_requires_signed_runtime_js(self):
        self.assertTrue(ca.firefox_requires_signed_runtime_js())


class FirefoxWriterTests(_LibraryFixture):
    def setUp(self):
        super().setUp()
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.user_content = self.profile / 'chrome' / 'userContent.css'

    def asset(self, name, text, asset_type='css', suffix='.css'):
        path = self.write_asset_file(asset_type, name + suffix, text)
        return {'id': name, 'name': name + suffix, 'path': str(path), 'sha256': ''}

    def test_read_asset_text_refuses_corrupt_asset(self):
        asset = self.asset('x', 'body{}')
        asset['sha256'] = 'bad'
        with self.assertRaisesRegex(ValueError, 'integrity'):
            ca._read_asset_text(asset)

    def test_user_content_block_replaces_previous_block_and_keeps_foreign_css(self):
        self.user_content.parent.mkdir(parents=True)
        self.user_content.write_text(
            '/* mine */\n' + ca.FIREFOX_USER_CONTENT_START + 'old\n' + ca.FIREFOX_USER_CONTENT_END,
            encoding='utf-8',
        )
        ca._write_firefox_user_content(
            self.profile, 'https://ex.org/app', [self.asset('a', 'a{}')],
            inline_css_text='b{}', managed_css_text='zoom{}',
        )
        text = self.user_content.read_text()
        self.assertTrue(text.startswith('/* mine */\n\n' + ca.FIREFOX_USER_CONTENT_START))
        self.assertNotIn('old', text)
        self.assertIn('@-moz-document url-prefix("https://ex.org/")', text)
        self.assertLess(text.index('Managed profile CSS'), text.index('Asset: a.css'))
        self.assertLess(text.index('Asset: a.css'), text.index('Inline CSS'))

    def test_user_content_without_foreign_css(self):
        ca._write_firefox_user_content(self.profile, 'https://ex.org', [], inline_css_text='b{}')
        self.assertTrue(self.user_content.read_text().startswith(ca.FIREFOX_USER_CONTENT_START))

    def test_user_content_removed_when_nothing_left(self):
        self.user_content.parent.mkdir(parents=True)
        self.user_content.write_text(ca.FIREFOX_USER_CONTENT_START + 'x\n' + ca.FIREFOX_USER_CONTENT_END)
        ca._write_firefox_user_content(self.profile, 'https://ex.org', [])
        self.assertFalse(self.user_content.exists())

    def test_user_content_without_scope_writes_no_block(self):
        self.user_content.parent.mkdir(parents=True)
        self.user_content.write_text('/* keep */\n')
        ca._write_firefox_user_content(self.profile, 'not a url', [], inline_css_text='a{}')
        self.assertEqual(self.user_content.read_text(), '/* keep */\n')

    def test_unreadable_existing_user_content_is_treated_as_empty(self):
        self.user_content.parent.mkdir(parents=True)
        self.user_content.write_text('/* lost */\n')
        original = Path.read_text

        def flaky(path, *args, **kwargs):
            if path == self.user_content:
                raise OSError('denied')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', flaky):
            ca._write_firefox_user_content(self.profile, 'https://ex.org', [], inline_css_text='z{}')
        text = self.user_content.read_text()
        self.assertNotIn('lost', text)
        self.assertIn('z{}', text)

    def test_remove_customizer_xpi(self):
        ext = self.profile / 'extensions'
        ext.mkdir()
        xpi = ext / ca.CUSTOMIZER_FIREFOX_XPI_NAME
        xpi.write_bytes(b'x')
        ca._remove_firefox_customizer_xpi(self.profile)
        self.assertFalse(xpi.exists())
        with mock.patch.object(Path, 'unlink', side_effect=OSError('busy')):
            ca._remove_firefox_customizer_xpi(self.profile)  # must not raise

    def test_xpi_not_written_without_scripts_or_scope(self):
        ext = self.profile / 'extensions'
        ext.mkdir()
        stale = ext / ca.CUSTOMIZER_FIREFOX_XPI_NAME
        stale.write_bytes(b'old')
        self.assertFalse(ca._write_firefox_customizer_xpi(self.profile, 'https://ex.org', []))
        self.assertFalse(stale.exists())
        self.assertFalse(ca._write_firefox_customizer_xpi(self.profile, 'mailto:x', [], inline_js_text='x()'))

    def test_xpi_contains_manifest_and_scripts(self):
        js = self.asset('lib one', 'lib()', 'javascript', '.js')
        self.assertTrue(ca._write_firefox_customizer_xpi(self.profile, 'https://ex.org/p', [js], inline_js_text='go()'))
        xpi = self.profile / 'extensions' / ca.CUSTOMIZER_FIREFOX_XPI_NAME
        with zipfile.ZipFile(xpi) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(archive.read('assets/lib-one-1.js').decode(), 'lib()')
            self.assertEqual(archive.read('assets/inline-runtime.js').decode(), 'go()\n')
        self.assertEqual(manifest['browser_specific_settings']['gecko']['id'], ca.CUSTOMIZER_FIREFOX_EXTENSION_ID)
        self.assertEqual(manifest['content_scripts'][0]['js'], ['assets/lib-one-1.js', 'assets/inline-runtime.js'])
        self.assertEqual(manifest['host_permissions'], ['https://ex.org/*'])
        leftovers = [p.name for p in (self.profile / 'extensions').iterdir()]
        self.assertEqual(leftovers, [ca.CUSTOMIZER_FIREFOX_XPI_NAME])


class ChromiumWriterTests(_LibraryFixture):
    def setUp(self):
        super().setUp()
        self.profile = self.root / 'chromium'
        self.profile.mkdir()
        self.ext_dir = self.profile / ca.CHROMIUM_CUSTOMIZER_DIRNAME

    def asset(self, name, text, asset_type, suffix):
        path = self.write_asset_file(asset_type, name + suffix, text)
        return {'id': name, 'name': name + suffix, 'path': str(path), 'sha256': ''}

    def test_nothing_to_write_removes_extension(self):
        self.ext_dir.mkdir()
        self.assertFalse(ca._write_chromium_customizer(self.profile, 'https://ex.org', [], []))
        self.assertFalse(self.ext_dir.exists())
        # Removing a non-existent directory is a no-op.
        ca._remove_chromium_extension(self.profile)

    def test_full_extension_layout(self):
        (self.ext_dir / 'stale').mkdir(parents=True)
        css = self.asset('c', 'c{}', 'css', '.css')
        js = self.asset('j', 'j()', 'javascript', '.js')
        self.assertTrue(ca._write_chromium_customizer(
            self.profile, 'https://ex.org', [css], [js],
            inline_css_text='i{}', inline_js_text='i()', managed_css_text='m{}',
        ))
        self.assertFalse((self.ext_dir / 'stale').exists())
        manifest = json.loads((self.ext_dir / 'manifest.json').read_text())
        block = manifest['content_scripts'][0]
        self.assertEqual(block['css'], ['assets/c-1.css', 'assets/managed-runtime.css', 'assets/inline-runtime.css'])
        self.assertEqual(block['js'], ['assets/j-1.js', 'assets/inline-runtime.js'])
        self.assertEqual((self.ext_dir / 'assets' / 'managed-runtime.css').read_text(), 'm{}\n')
        self.assertEqual((self.ext_dir / 'assets' / 'j-1.js').read_text(), 'j()')

    def test_css_only_manifest_has_no_js_key(self):
        self.assertTrue(ca._write_chromium_customizer(self.profile, 'https://ex.org', [], [], inline_css_text='a{}'))
        block = json.loads((self.ext_dir / 'manifest.json').read_text())['content_scripts'][0]
        self.assertNotIn('js', block)
        self.assertEqual(block['css'], ['assets/inline-runtime.css'])


class EnsureProfileCustomizationsTests(_LibraryFixture):
    def test_missing_profile_info(self):
        logger = mock.Mock()
        self.assertEqual(ca.ensure_profile_customizations(None, {}, logger), {'css_applied': False, 'js_applied': False})
        self.assertEqual(ca.ensure_profile_customizations({'profile_path': ''}, {}, logger), {'css_applied': False, 'js_applied': False})

    def test_unknown_family(self):
        result = ca.ensure_profile_customizations({'profile_path': str(self.root), 'browser_family': 'epiphany'}, {}, mock.Mock())
        self.assertEqual(result, {'css_applied': False, 'js_applied': False})

    def test_firefox_zoom_only_applies_css(self):
        profile = self.root / 'ff'
        profile.mkdir()
        options = {'Address': 'https://ex.org', DEFAULT_ZOOM_KEY: '125'}
        result = ca.ensure_profile_customizations({'profile_path': str(profile), 'browser_family': 'Firefox'}, options, mock.Mock())
        self.assertEqual(result, {'css_applied': True, 'js_applied': False})
        self.assertIn('zoom: 125%', (profile / 'chrome' / 'userContent.css').read_text())

    def test_firefox_write_errors_are_logged(self):
        logger = mock.Mock()
        with mock.patch.object(ca, '_write_firefox_user_content', side_effect=OSError('ro')), \
                mock.patch.object(ca, '_write_firefox_customizer_xpi', side_effect=OSError('ro')):
            result = ca.ensure_profile_customizations(
                {'profile_path': str(self.root), 'browser_family': 'firefox'},
                {'Address': 'https://ex.org', ca.INLINE_CUSTOM_CSS_KEY: 'a{}'}, logger,
            )
        self.assertEqual(result, {'css_applied': False, 'js_applied': False})
        self.assertEqual(logger.warning.call_count, 2)

    def test_chromium_reports_css_and_js_separately(self):
        profile = self.root / 'cr'
        profile.mkdir()
        options = {'Address': 'https://ex.org', ca.INLINE_CUSTOM_JS_KEY: 'x()'}
        result = ca.ensure_profile_customizations({'profile_path': str(profile), 'browser_family': 'chrome'}, options, mock.Mock())
        self.assertEqual(result, {'css_applied': False, 'js_applied': True})

    def test_chromium_write_error_is_logged(self):
        logger = mock.Mock()
        with mock.patch.object(ca, '_write_chromium_customizer', side_effect=OSError('ro')):
            result = ca.ensure_profile_customizations({'profile_path': str(self.root), 'browser_family': 'chromium'}, {}, logger)
        self.assertEqual(result, {'css_applied': False, 'js_applied': False})
        logger.warning.assert_called_once()


class ChromiumRuntimeArgsTests(_LibraryFixture):
    def test_args_only_for_chromium_with_customizations_and_existing_dir(self):
        options = {ca.INLINE_CUSTOM_CSS_KEY: 'a{}'}
        info = {'browser_family': 'chromium', 'profile_path': str(self.root)}
        self.assertEqual(ca.chromium_runtime_extension_args(None, options), [])
        self.assertEqual(ca.chromium_runtime_extension_args({'browser_family': 'firefox'}, options), [])
        self.assertEqual(ca.chromium_runtime_extension_args(info, {}), [])
        self.assertEqual(ca.chromium_runtime_extension_args(info, options), [])
        ext = self.root / ca.CHROMIUM_CUSTOMIZER_DIRNAME
        ext.mkdir()
        self.assertEqual(ca.chromium_runtime_extension_args(info, options), [f'--load-extension={ext}'])


if __name__ == '__main__':
    unittest.main()
