import io
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from check_versions import (MAX_PAGE_BYTES, SOURCE, fetch_latest, parse_latest,
                            series_relation, version_report)
from patch_engine import PatchError


def page(series='3.27'):
    return ('<span class="type-md">' + series + '</span><span class="label">Latest</span>'
            '<a href="https://api2.cursor.sh/updates/download/golden/darwin-arm64/cursor/' +
            series + '">Mac (ARM64)</a>')


class VersionTests(unittest.TestCase):
    def test_latest_marker_requires_matching_official_platform_link(self):
        result = parse_latest(page() + page())
        self.assertEqual(result['series'], '3.27')
        self.assertIsNone(result['full_version'])
        self.assertFalse(result['resource_fingerprints_verified'])
        for html in [page().replace('Latest', 'Archived'), page() + page('3.26'),
                     page().replace('api2.cursor.sh', 'untrusted.example')]:
            with self.assertRaises(PatchError):
                parse_latest(html)

    def test_numeric_comparison_does_not_claim_patch_level_current(self):
        self.assertEqual(series_relation('3.9.9', '3.27'), 'behind')
        self.assertEqual(series_relation('3.27.1', '3.27'), 'same_series_patch_level_unknown')
        self.assertEqual(series_relation('4.0.0', '3.27'), 'ahead')
        with self.assertRaises(PatchError):
            series_relation('unknown', '3.27')

    def test_fetch_is_bounded_and_rejects_unexpected_redirects(self):
        for contents, url, expected in [(page().encode(), SOURCE, True),
                                         (b'x' * (MAX_PAGE_BYTES + 1), SOURCE, False),
                                         (page().encode(), 'https://untrusted.example', False)]:
            response = io.BytesIO(contents)
            response.geturl = lambda: url
            with patch('check_versions.urllib.request.urlopen', return_value=response):
                if expected:
                    self.assertEqual(fetch_latest()['series'], '3.27')
                else:
                    with self.assertRaises(PatchError):
                        fetch_latest()

    def test_network_failure_is_not_reported_as_up_to_date(self):
        with patch('check_versions.urllib.request.urlopen', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                fetch_latest()

    def test_known_local_version_does_not_bless_unknown_resources(self):
        local = {'observed': {'version': '3.24.18', 'commit': 'installed'},
                 'expected': {'version': '3.24.18', 'commit': 'supported'},
                 'state': 'unknown_source', 'supported': False}
        with patch('check_versions.maintenance_report', return_value=local):
            result = version_report(pathlib.Path('unused'), {'cursor_version': '3.24.18'},
                                    {}, parse_latest(page()))
        self.assertEqual(result['installed_vs_official_series'], 'behind')
        self.assertFalse(result['local_patch_supported'])
        self.assertFalse(result['automatic_upgrade_or_patch'])
        self.assertTrue(result['no_application_writes'])

    def test_preview_profile_is_not_reported_as_installation_support(self):
        local = {'observed': {'version': '3.24.18'}, 'expected': {'version': '3.24.18'},
                 'state': 'unknown_source', 'supported': False}
        candidate = {'cursor_version': '3.27.22', 'cursor_commit': 'preview-build',
                     'installation_supported': False}
        with patch('check_versions.maintenance_report', return_value=local):
            result = version_report(pathlib.Path('unused'), {'cursor_version': '3.24.18'},
                                    {}, parse_latest(page()), [candidate])
        self.assertEqual(result['repository_vs_official_series'], 'behind')
        self.assertEqual(result['resource_previews'][0]['vs_official_series'],
                         'same_series_patch_level_unknown')
        self.assertFalse(result['resource_previews'][0]['installation_supported'])


if __name__ == '__main__':
    unittest.main()
