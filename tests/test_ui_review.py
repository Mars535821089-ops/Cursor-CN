import base64
import gzip
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from patch_engine import PatchError, apply_bytes, create_delta, sha256
from runtime_build import ROOT, build_nls, load_app_patch, load_profile, load_ui_review
from install import classify_app, maintenance_report, preview_report, recovery_launcher
from ui_review import literal_patch, load_translations, review_nls


class UIReviewTests(unittest.TestCase):
    def test_dictionary_preserves_placeholders(self):
        values = load_translations(ROOT/'patches/reviewed-ui.json')
        self.assertIn('Drive the PR end-to-end', values)
        self.assertIn('Turn On and Restart', values)
        self.assertIn('Loading file', values)

    def test_bad_placeholder_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp)/'dictionary.json'
            path.write_text(json.dumps({'schema': 1, 'translations': {'Hello {0}': '你好 {1}'}}))
            with self.assertRaises(PatchError):
                load_translations(path)

    def test_nls_only_translations_do_not_expand_source_replacement_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp)/'dictionary.json'
            path.write_text(json.dumps({'schema': 1, 'translations': {'Open': '打开'},
                                        'nls_translations': {'Close': '关闭'}}))
            self.assertEqual(load_translations(path), {'Open': '打开'})
            self.assertEqual(load_translations(path, include_nls=True),
                             {'Open': '打开', 'Close': '关闭'})

    def test_conflicting_or_invalid_nls_dictionary_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp)/'dictionary.json'
            for extra in [{'Open': '开启'}, {'Hello {0}': '你好 {1}'}, []]:
                path.write_text(json.dumps({'schema': 1, 'translations': {'Open': '打开'},
                                            'nls_translations': extra}))
                with self.assertRaises(PatchError):
                    load_translations(path, include_nls=True)

    def test_json_manifest_keys_identifiers_and_other_values_are_preserved(self):
        source = b'{"name":"Open","contributes":{"commands":[{"command":"Open","title":"Open"}]}}'
        start = source.index(b'"Open"', source.index(b'"title"'))
        delta = literal_patch(source, [{'start': start, 'end': start+6, 'value': 'Open'}],
                              {'Open': '打开'})
        actual = json.loads(apply_bytes(source, delta))
        expected = json.loads(source)
        expected['contributes']['commands'][0]['title'] = '打开'
        self.assertEqual(actual, expected)

    def test_utf16_ast_offsets_become_utf8_byte_offsets(self):
        source = 'const icon="🌙"; const a={description:"Open"};'.encode()
        text = source.decode()
        start = text.index('"Open"')
        offset = len(text[:start].encode('utf-16-le'))//2
        delta = literal_patch(source, [{'start': offset, 'end': offset+6, 'value': 'Open'}],
                              {'Open': '打开'})
        self.assertEqual(apply_bytes(source, delta), source.replace(b'"Open"', '"打开"'.encode()))

    def test_translation_replaces_only_the_selected_literal(self):
        source = b'const a={id:"Open",description:"Open"}; const userText="Open";'
        start = source.index(b'"Open"', source.index(b'description'))
        delta = literal_patch(source, [{'start': start, 'end': start+6, 'value': 'Open'}],
                              {'Open': '打开'})
        self.assertEqual(apply_bytes(source, delta),
                         b'const a={id:"Open",description:' + '"打开"'.encode() + b'}; const userText="Open";')
        with self.assertRaises(PatchError):
            apply_bytes(b'unknown', delta)

    def test_bad_and_overlapping_offsets_stop(self):
        with self.assertRaises(PatchError):
            literal_patch(b'const a="Open";',
                          [{'start': 0, 'end': 5, 'value': 'Open'}], {'Open': '打开'})

    def test_nls_preserves_existing_chinese_and_brands(self):
        english = ['Open', 'Already translated', 'Cursor', 'Hello {0}']
        baseline = ['Open', '已有译文', 'Cursor', 'Hello {0}']
        final, changed = review_nls(english, baseline, {'Open': '打开', 'Hello {0}': '你好 {0}'})
        self.assertEqual(final, ['打开', '已有译文', 'Cursor', '你好 {0}'])
        self.assertEqual(changed, [0, 3])

    def test_baseline_and_reviewed_nls_hashes_are_both_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            app = root/'Cursor.app'
            resources = app/'Contents/Resources/app/out'
            resources.mkdir(parents=True)
            (resources/'nls.keys.json').write_text('[["module",["open"]]]')
            (resources/'nls.messages.json').write_text('["Open"]')
            vsix = root/'pack.zip'
            with zipfile.ZipFile(vsix, 'w') as archive:
                archive.writestr('extension/translations/main.i18n.json', '{"contents":{}}')
            dictionary = root/'reviewed.json'
            dictionary.write_text('{"schema":1,"translations":{"Open":"打开"}}')
            overlay = {'entries': [], 'after_sha256': sha256(b'["Open"]')}
            profile = {'ui_review_dictionary': 'reviewed.json',
                       'reviewed_nls_sha256': sha256('["打开"]'.encode())}
            self.assertEqual(build_nls(app, vsix, overlay, profile, root), '["打开"]'.encode())
            profile['reviewed_nls_sha256'] = 'wrong'
            with self.assertRaises(PatchError):
                build_nls(app, vsix, overlay, profile, root)
            overlay['after_sha256'] = 'wrong'
            with self.assertRaises(PatchError):
                build_nls(app, vsix, overlay, profile, root)

    def test_chained_app_classification_preview_and_standalone_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            app = root/'Cursor.app'
            resources = app/'Contents/Resources/app'
            resources.mkdir(parents=True)
            (resources/'package.json').write_text('{"version":"tested"}')
            (resources/'product.json').write_text('{"commit":"tested"}')
            files = {'main.js': create_delta(b'original', b'base')}
            review = {'main.js': create_delta(b'base', b'reviewed')}
            profile = {'cursor_version': 'tested', 'cursor_commit': 'tested',
                       'runtime_path': 'runtime.js', 'runtime_sha256': sha256(b'runtime'),
                       'ui_review_patch': 'unavailable-on-purpose.gz', 'ui_review_files': review}
            (resources/'main.js').write_bytes(b'original')
            self.assertEqual(classify_app(app, profile, files), 'original')
            preview = preview_report(app, profile, files)
            self.assertEqual([x['phase'] for x in preview['diffs']], ['base', 'ui-review'])
            (resources/'main.js').write_bytes(b'base')
            (resources/'runtime.js').write_bytes(b'runtime')
            self.assertFalse(maintenance_report(app, profile, files)['supported'])
            (resources/'main.js').write_bytes(b'reviewed')
            self.assertEqual(classify_app(app, profile, files), 'patched')
            self.assertTrue(maintenance_report(app, profile, files)['supported'])
            backup = root/'backup'
            backup.mkdir()
            recovery_launcher(backup)
            self.assertTrue((backup/'ui_review.py').is_file())
            self.assertEqual(load_ui_review(profile, files), review)
            review['main.js']['before_sha256'] = 'wrong'
            with self.assertRaises(PatchError):
                load_ui_review(profile, files)

    def test_published_review_chain_and_new_resource_fingerprints(self):
        profile = load_profile()
        files = load_app_patch(profile)
        review = load_ui_review(profile, files)
        self.assertGreater(sum(len(item['edits']) for item in review.values()), 0)
        base = set(json.loads(gzip.decompress((ROOT/profile['app_patch']).read_bytes()))['files'])
        for name in set(review)-base:
            self.assertEqual(files[name]['before_sha256'], review[name]['before_sha256'])
            self.assertEqual(files[name]['edits'], [])


if __name__ == '__main__':
    unittest.main()
