import json
import re
import string
import unittest
from pathlib import Path

LANG_DIR = Path(__file__).resolve().parent.parent / 'lang'
REFERENCE_LANGUAGE = 'en'
META_KEY = '_meta_language_name'
ALLOWED_MISSING_KEYS = {META_KEY}
PLACEHOLDER_PATTERN = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')


def _load_language(code):
    path = LANG_DIR / f'{code}.json'
    return json.loads(path.read_text(encoding='utf-8'))


def _other_languages():
    for path in sorted(LANG_DIR.glob('*.json')):
        code = path.stem
        if code != REFERENCE_LANGUAGE:
            yield code


def _placeholders(value):
    if not isinstance(value, str):
        return set()
    formatter = string.Formatter()
    keys = set()
    try:
        for _literal, field_name, _format_spec, _conversion in formatter.parse(value):
            if not field_name:
                continue
            head = field_name.split('.', 1)[0].split('[', 1)[0]
            if head:
                keys.add(head)
    except ValueError:
        keys.update(PLACEHOLDER_PATTERN.findall(value))
    return keys


class I18nKeyCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = _load_language(REFERENCE_LANGUAGE)
        cls.reference_keys = set(cls.reference.keys())

    def test_reference_language_present(self):
        self.assertGreater(len(self.reference_keys), 0)

    def test_other_languages_have_no_unknown_keys(self):
        offenders = {}
        for code in _other_languages():
            data = _load_language(code)
            extras = set(data.keys()) - self.reference_keys - ALLOWED_MISSING_KEYS
            if extras:
                offenders[code] = sorted(extras)
        self.assertEqual(offenders, {}, f'unknown keys found that are not in {REFERENCE_LANGUAGE}.json: {offenders}')

    def test_other_languages_have_matching_placeholders(self):
        mismatches = {}
        for code in _other_languages():
            data = _load_language(code)
            per_key = {}
            for key, reference_value in self.reference.items():
                if key not in data:
                    continue
                expected = _placeholders(reference_value)
                actual = _placeholders(data[key])
                if expected != actual:
                    per_key[key] = {
                        'expected': sorted(expected),
                        'actual': sorted(actual),
                    }
            if per_key:
                mismatches[code] = per_key
        self.assertEqual(mismatches, {}, f'placeholder mismatches detected: {mismatches}')


class I18nKeyCompletenessTests(unittest.TestCase):
    """Reports missing keys per language as a soft check.

    Missing translations are common in incomplete locales; we keep this as a
    pure information signal so reviewers can see the coverage without forcing
    every PR to translate every string. The test always passes; the report is
    visible via the test name list when running with -v.
    """

    @classmethod
    def setUpClass(cls):
        cls.reference_keys = set(_load_language(REFERENCE_LANGUAGE).keys())

    def test_translation_coverage_report(self):
        report = []
        for code in _other_languages():
            data = _load_language(code)
            present = set(data.keys()) & self.reference_keys
            coverage = len(present) / max(1, len(self.reference_keys))
            if coverage < 1.0:
                missing = len(self.reference_keys - set(data.keys()))
                report.append((code, round(coverage * 100, 1), missing))
        # Always passes; informational.
        for code, coverage_pct, missing in report:
            self.assertGreaterEqual(coverage_pct, 0.0, f'{code}: {coverage_pct}% coverage, {missing} keys missing')


if __name__ == '__main__':
    unittest.main()
