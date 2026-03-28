import json
import logging
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


def _build_test_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(f'test.runtime.{name}')
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


fake_logger_setup = types.ModuleType('logger_setup')
fake_logger_setup.get_logger = _build_test_logger
sys.modules.setdefault('logger_setup', fake_logger_setup)

from custom_assets import (
    CHROMIUM_CUSTOMIZER_DIRNAME,
    CUSTOMIZER_FIREFOX_XPI_NAME,
    chromium_runtime_extension_args,
    ensure_profile_customizations,
)


class RuntimeCustomizationTests(unittest.TestCase):
    def test_firefox_css_linked_file_writes_user_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)
            css_file = profile_dir / 'theme.css'
            css_file.write_text('body { color: red; }', encoding='utf-8')
            css_asset = {'id': 'asset-css', 'name': 'theme.css', 'path': str(css_file)}
            profile_info = {'browser_family': 'firefox', 'profile_path': str(profile_dir)}
            options = {'Address': 'https://example.com/app'}

            with patch('custom_assets.linked_assets_for_options') as mock_linked, patch('custom_assets.inline_asset_text_for_options') as mock_inline:
                mock_linked.side_effect = lambda _opts, asset_type=None: [css_asset] if asset_type == 'css' else []
                mock_inline.side_effect = lambda _opts, asset_type=None: ''
                result = ensure_profile_customizations(profile_info, options, _build_test_logger('firefox-css'))

            user_content = (profile_dir / 'chrome' / 'userContent.css').read_text(encoding='utf-8')

        self.assertTrue(result['css_applied'])
        self.assertFalse(result['js_applied'])
        self.assertIn('@-moz-document url-prefix("https://example.com/")', user_content)
        self.assertIn('body { color: red; }', user_content)

    def test_firefox_inline_javascript_writes_runtime_xpi(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)
            profile_info = {'browser_family': 'firefox', 'profile_path': str(profile_dir)}
            options = {'Address': 'https://example.com/app'}

            with patch('custom_assets.linked_assets_for_options') as mock_linked, patch('custom_assets.inline_asset_text_for_options') as mock_inline:
                mock_linked.return_value = []
                mock_inline.side_effect = lambda _opts, asset_type=None: 'console.log("inline js");' if asset_type == 'javascript' else ''
                result = ensure_profile_customizations(profile_info, options, _build_test_logger('firefox-js'))

            xpi_path = profile_dir / 'extensions' / CUSTOMIZER_FIREFOX_XPI_NAME
            with zipfile.ZipFile(xpi_path) as archive:
                manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
                inline_js = archive.read('assets/inline-runtime.js').decode('utf-8')

        self.assertFalse(result['css_applied'])
        self.assertTrue(result['js_applied'])
        self.assertEqual(manifest['manifest_version'], 3)
        self.assertIn('assets/inline-runtime.js', manifest['content_scripts'][0]['js'])
        self.assertIn('console.log("inline js");', inline_js)

    def test_chromium_inline_css_writes_extension_and_launch_arg(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)
            profile_info = {'browser_family': 'chromium', 'profile_path': str(profile_dir)}
            options = {'Address': 'https://example.com/app', 'Inline Custom CSS': 'body { background: blue; }'}

            with patch('custom_assets.linked_assets_for_options') as mock_linked, patch('custom_assets.inline_asset_text_for_options') as mock_inline:
                mock_linked.return_value = []
                mock_inline.side_effect = lambda _opts, asset_type=None: 'body { background: blue; }' if asset_type == 'css' else ''
                result = ensure_profile_customizations(profile_info, options, _build_test_logger('chromium-css'))

            extension_dir = profile_dir / CHROMIUM_CUSTOMIZER_DIRNAME
            manifest = json.loads((extension_dir / 'manifest.json').read_text(encoding='utf-8'))
            inline_css = (extension_dir / 'assets' / 'inline-runtime.css').read_text(encoding='utf-8')
            args = chromium_runtime_extension_args(profile_info, options)

        self.assertTrue(result['css_applied'])
        self.assertFalse(result['js_applied'])
        self.assertIn('assets/inline-runtime.css', manifest['content_scripts'][0]['css'])
        self.assertIn('body { background: blue; }', inline_css)
        self.assertEqual(args, [f'--load-extension={extension_dir}'])

    def test_chromium_linked_javascript_file_writes_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir)
            js_file = profile_dir / 'runtime.js'
            js_file.write_text('console.log("linked js");', encoding='utf-8')
            js_asset = {'id': 'asset-js', 'name': 'runtime.js', 'path': str(js_file)}
            profile_info = {'browser_family': 'chrome', 'profile_path': str(profile_dir)}
            options = {'Address': 'https://example.com/app'}

            with patch('custom_assets.linked_assets_for_options') as mock_linked, patch('custom_assets.inline_asset_text_for_options') as mock_inline:
                mock_linked.side_effect = lambda _opts, asset_type=None: [js_asset] if asset_type == 'javascript' else []
                mock_inline.side_effect = lambda _opts, asset_type=None: ''
                result = ensure_profile_customizations(profile_info, options, _build_test_logger('chromium-js'))

            extension_dir = profile_dir / CHROMIUM_CUSTOMIZER_DIRNAME
            manifest = json.loads((extension_dir / 'manifest.json').read_text(encoding='utf-8'))
            script_paths = manifest['content_scripts'][0]['js']
            asset_script = (extension_dir / script_paths[0]).read_text(encoding='utf-8')

        self.assertFalse(result['css_applied'])
        self.assertTrue(result['js_applied'])
        self.assertTrue(script_paths[0].startswith('assets/'))
        self.assertIn('console.log("linked js");', asset_script)


if __name__ == '__main__':
    unittest.main()
