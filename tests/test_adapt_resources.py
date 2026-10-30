import gzip
import json
import pathlib
import plistlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from adapt_resources import (adapt_nls, check_translation, json_bytes, load_resource_profile,
                             merge_dictionaries, preview, verify_mounted_image, verify_sample)
from patch_engine import PatchError, create_delta, sha256
from runtime_build import ROOT


class AdaptationTests(unittest.TestCase):
    def test_exact_hash_migration_does_not_reuse_prior_indices(self):
        source = ['New message', 'Old message']
        entries = [{'index': 0, 'source_sha256': sha256(b'Old message'), 'translation': '旧消息'}]
        result, changes, counts = adapt_nls(source, [['module', ['new', 'old']]], {}, {}, entries)
        self.assertEqual(result, ['New message', '旧消息'])
        self.assertEqual(changes[0]['index'], 1)
        self.assertEqual(counts['exact_source_migration'], 1)

    def test_official_pack_has_priority_and_is_not_redistributed_in_overlay(self):
        result, changes, counts = adapt_nls(
            ['Open', 'New'], [['module', ['open', 'new']]],
            {'module': {'open': '打开'}}, {'Open': '开启', 'New': '新建'}, [])
        self.assertEqual(result, ['打开', '新建'])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['index'], 1)
        self.assertEqual(counts['official'], 1)

    def test_changed_placeholders_are_explicitly_rejected_and_counted(self):
        result, changes, counts = adapt_nls(
            ['Hello {0} {0}'], [['module', ['hello']]],
            {'module': {'hello': '你好 {0}'}}, {}, [])
        self.assertEqual(result, ['Hello {0} {0}'])
        self.assertEqual(changes, [])
        self.assertEqual(counts['placeholder_rejections'], 1)
        with self.assertRaises(PatchError):
            check_translation('Hello {0}', '你好 {1}')

    def test_bad_index_lengths_and_conflicting_prior_translations_stop(self):
        with self.assertRaises(PatchError):
            adapt_nls(['One'], [], {}, {}, [])
        entries = [{'source_sha256': sha256(b'One'), 'translation': value}
                   for value in ['一', '壹']]
        with self.assertRaises(PatchError):
            adapt_nls(['One'], [['module', ['one']]], {}, {}, entries)

    def test_only_read_only_mount_from_the_matching_image_is_accepted(self):
        app, dmg = pathlib.Path('/sample/mount/Cursor.app'), pathlib.Path('/sample/image.dmg')
        image = {'image-path': str(dmg), 'writeable': False,
                 'system-entities': [{'mount-point': '/sample/mount'}]}
        for writable, expected in [(False, True), (True, False)]:
            image['writeable'] = writable
            result = subprocess.CompletedProcess([], 0, stdout=plistlib.dumps({'images': [image]}))
            with patch('adapt_resources.subprocess.run', return_value=result):
                if expected:
                    verify_mounted_image(app, dmg)
                else:
                    with self.assertRaises(PatchError):
                        verify_mounted_image(app, dmg)
        image['writeable'] = False
        image['image-path'] = '/different/image.dmg'
        result = subprocess.CompletedProcess([], 0, stdout=plistlib.dumps({'images': [image]}))
        with patch('adapt_resources.subprocess.run', return_value=result):
            with self.assertRaises(PatchError):
                verify_mounted_image(app, dmg)

    def test_signing_and_stapling_failures_are_not_ignored(self):
        with patch('adapt_resources.subprocess.run',
                   side_effect=subprocess.CalledProcessError(1, ['codesign'])):
            with self.assertRaises(subprocess.CalledProcessError):
                verify_sample(pathlib.Path('sample.app'))
        wrong = subprocess.CompletedProcess([], 0, stdout='', stderr='TeamIdentifier=OTHER')
        with patch('adapt_resources.subprocess.run', return_value=wrong):
            with self.assertRaises(PatchError):
                verify_sample(pathlib.Path('sample.app'))
        valid = subprocess.CompletedProcess([], 0, stdout='', stderr=(
            'TeamIdentifier=VDXQ22DGB9\n'
            'Authority=Developer ID Application: Hilary Stout (VDXQ22DGB9)'))
        with patch('adapt_resources.subprocess.run', side_effect=[
                valid, valid, subprocess.CalledProcessError(1, ['stapler'])]):
            with self.assertRaises(subprocess.CalledProcessError):
                verify_sample(pathlib.Path('sample.app'))

    def test_disabled_gatekeeper_is_explicit_not_a_clean_security_assessment(self):
        success = subprocess.CompletedProcess([], 0, stdout='', stderr='')
        signature = subprocess.CompletedProcess([], 0, stdout='', stderr=(
            'TeamIdentifier=VDXQ22DGB9\n'
            'Authority=Developer ID Application: Hilary Stout (VDXQ22DGB9)'))
        assessment = subprocess.CompletedProcess([], 0, stdout='',
                                                 stderr='accepted\noverride=security disabled')
        disabled = subprocess.CompletedProcess([], 1, stdout='assessments disabled\n', stderr='')
        with patch('adapt_resources.subprocess.run',
                   side_effect=[success, signature, success, assessment, disabled]):
            result = verify_sample(pathlib.Path('sample.app'))
        self.assertTrue(result['stapled_notarization_valid'])
        self.assertTrue(result['gatekeeper_assessments_disabled'])
        self.assertTrue(result['gatekeeper_override_reported'])

    def test_source_dictionary_conflicts_stop_instead_of_overriding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            for name, value in [('a.json', '打开'), ('b.json', '开启')]:
                (root/name).write_bytes(json_bytes({'schema': 1, 'translations': {'Open': value}}))
            with patch('adapt_resources.ROOT', root):
                with self.assertRaises(PatchError):
                    merge_dictionaries(['a.json', 'b.json'])

    def test_dictionaries_merge_without_broadening_old_source_scope(self):
        values = merge_dictionaries(['patches/reviewed-ui.json', 'patches/reviewed-ui-3.27.json'])
        self.assertIn('Your session expired. Sign in again.', values)
        self.assertNotIn('Agent host heap snapshots', values)
        nls = merge_dictionaries(['patches/reviewed-ui.json', 'patches/reviewed-ui-3.27.json'],
                                 include_nls=True)
        self.assertIn('Agent host heap snapshots', nls)

    def test_published_preview_is_locked_and_not_an_installation_profile(self):
        profile = load_resource_profile(ROOT/'patches/profile-3.27.22.json')
        self.assertEqual(profile['cursor_version'], '3.27.22')
        self.assertFalse(profile['installation_supported'])
        self.assertTrue(profile['source']['signer']['stapled_notarization_valid'])
        self.assertEqual(profile['coverage']['literal_corrections'], 1119)
        self.assertEqual(profile['coverage']['nls_changes'], 1624)
        remaining = json.loads((ROOT/profile['remaining_candidates']).read_text())
        self.assertEqual(len(remaining['nls']), profile['coverage']['nls_english_candidates_remaining'])
        self.assertEqual(len(remaining['dynamic_templates']), profile['coverage']['dynamic_ui_templates'])
        self.assertEqual(len(remaining['language_pack_placeholder_rejections']), 22)

    def test_preview_reconstructs_without_writing_and_refuses_mutated_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            app = root/'Cursor.app'
            resources = app/'Contents/Resources/app'
            resources.mkdir(parents=True)
            fixture = {'package.json': {'version': 'tested'}, 'product.json': {'commit': 'tested'},
                       'out/nls.keys.json': [['module', ['open']]],
                       'out/nls.messages.json': ['Open']}
            for name, value in fixture.items():
                file = resources/name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(json_bytes(value))
            (resources/'main.js').write_bytes(b'const label="Open";')
            delta = create_delta(b'const label="Open";', 'const label="打开";'.encode())
            patch_file = root/'resource.gz'
            patch_file.write_bytes(gzip.compress(json_bytes({'files': {'main.js': delta}})))
            overlay = root/'nls.json'
            overlay.write_bytes(json_bytes({'entries': [
                {'index': 0, 'source_sha256': sha256(b'Open'), 'translation': '打开'}],
                'after_sha256': sha256(json_bytes(['打开']))}))
            vsix = root/'pack.zip'
            with zipfile.ZipFile(vsix, 'w') as archive:
                archive.writestr('extension/translations/main.i18n.json', '{"contents":{}}')
            fingerprints = {p.relative_to(resources).as_posix(): sha256(p.read_bytes())
                            for p in resources.rglob('*') if p.is_file()}
            profile = {'schema': 1, 'mode': 'verified_resource_preview_only',
                       'cursor_version': 'tested', 'cursor_commit': 'tested',
                       'installation_supported': False,
                       'artifacts': {'resource.gz': sha256(patch_file.read_bytes()),
                                     'nls.json': sha256(overlay.read_bytes())},
                       'resource_fingerprints': fingerprints,
                       'ui_review_patch': 'resource.gz', 'nls_overlay': 'nls.json',
                       'language_pack': {'sha256': sha256(vsix.read_bytes())}, 'coverage': {}}
            profile_path = root/'profile.json'
            profile_path.write_bytes(json_bytes(profile))
            with patch('adapt_resources.ROOT', root):
                result = preview(app, profile_path, vsix)
                self.assertTrue(result['no_application_writes'])
                self.assertEqual((resources/'main.js').read_bytes(), b'const label="Open";')
                self.assertEqual({name: sha256((resources/name).read_bytes())
                                  for name in fingerprints}, fingerprints)
                (resources/'main.js').write_bytes(b'unknown')
                with self.assertRaises(PatchError):
                    preview(app, profile_path, vsix)


if __name__ == '__main__':
    unittest.main()
