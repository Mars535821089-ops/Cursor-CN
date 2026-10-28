import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from patch_engine import PatchError, create_delta, sha256
from install import classify_app, snapshot_settings, restore_settings, update_checksums, read_object


class InstallerTests(unittest.TestCase):
    def test_jsonc_argv_preserves_url_strings_and_trailing_comma_values(self):
        with tempfile.TemporaryDirectory() as d:
            path=pathlib.Path(d)/'argv.json'
            path.write_text('''{
              // Native Cursor argv files commonly contain comments.
              "locale": "en",
              "url": "https://example.invalid/a//b", /* keep slashes inside strings */
              "items": ["x,}", "y",],
            }''')
            data=read_object(path)
            self.assertEqual(data['url'],'https://example.invalid/a//b')
            self.assertEqual(data['items'],['x,}','y'])

    def test_unterminated_jsonc_block_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            path=pathlib.Path(d)/'argv.json';path.write_text('{/* unclosed')
            with self.assertRaises(ValueError):read_object(path)

    def fixture(self, parent):
        app = pathlib.Path(parent) / 'Cursor.app'
        root = app / 'Contents/Resources/app'
        (root/'out').mkdir(parents=True)
        (root/'package.json').write_text(json.dumps({'version':'3.24.18'}))
        (root/'product.json').write_text(json.dumps({'commit':'tested','checksums':{}}))
        (root/'out/main.js').write_bytes(b'original')
        profile = {'cursor_version':'3.24.18','cursor_commit':'tested',
                   'runtime_path':'out/runtime.js','runtime_sha256':sha256(b'runtime')}
        files = {'out/main.js':create_delta(b'original',b'localized')}
        return app, root, profile, files

    def test_recognizes_original_and_idempotent_installed_state(self):
        with tempfile.TemporaryDirectory() as d:
            app,root,profile,files=self.fixture(d)
            self.assertEqual(classify_app(app,profile,files),'original')
            (root/'out/main.js').write_bytes(b'localized')
            (root/'out/runtime.js').write_bytes(b'runtime')
            self.assertEqual(classify_app(app,profile,files),'patched')

    def test_unknown_or_partial_patch_refused(self):
        with tempfile.TemporaryDirectory() as d:
            app,root,profile,files=self.fixture(d)
            (root/'out/main.js').write_bytes(b'unknown')
            with self.assertRaises(PatchError):classify_app(app,profile,files)

    def test_wrong_version_refused(self):
        with tempfile.TemporaryDirectory() as d:
            app,root,profile,files=self.fixture(d)
            (root/'package.json').write_text(json.dumps({'version':'9.0.0'}))
            with self.assertRaises(PatchError):classify_app(app,profile,files)

    def test_checksummed_metadata_not_replaced_wholesale(self):
        with tempfile.TemporaryDirectory() as d:
            app,root,profile,files=self.fixture(d)
            (root/'product.json').write_text(json.dumps({'commit':'tested','unrelated':'keep','checksums':{'main.js':'old'}}))
            update_checksums(root)
            product=json.loads((root/'product.json').read_text())
            self.assertEqual(product['unrelated'],'keep')
            self.assertNotEqual(product['checksums']['main.js'],'old')

    def test_language_restore_preserves_changes_made_after_install(self):
        with tempfile.TemporaryDirectory() as d:
            base=pathlib.Path(d);data=base/'data';backup=base/'backup';data.mkdir();backup.mkdir()
            (data/'argv.json').write_text('{"locale":"en","unrelated":true}')
            records=snapshot_settings(backup,data)
            (data/'argv.json').write_text('{"locale":"zh-cn","unrelated":true}')
            records['argv.json']['after_sha256']=sha256((data/'argv.json').read_bytes())
            (data/'argv.json').write_text('{"locale":"fr","newUserChoice":true}')
            skipped=restore_settings(backup,data,records)
            self.assertIn('argv.json',skipped)
            self.assertEqual(json.loads((data/'argv.json').read_text())['locale'],'fr')

    def test_failed_install_can_restore_all_snapshot_files(self):
        with tempfile.TemporaryDirectory() as d:
            base=pathlib.Path(d);data=base/'data';backup=base/'backup';data.mkdir();backup.mkdir()
            original=b'{"locale":"en","keep":true}'
            (data/'argv.json').write_bytes(original)
            records=snapshot_settings(backup,data)
            (data/'argv.json').write_bytes(b'{"locale":"zh-cn"}')
            (data/'locale.json').write_bytes(b'{"locale":"zh-cn"}')
            restore_settings(backup,data,records,force=True)
            self.assertEqual((data/'argv.json').read_bytes(),original)
            self.assertFalse((data/'locale.json').exists())

    def test_cache_snapshot_does_not_overwrite_pre_cli_language_backup(self):
        with tempfile.TemporaryDirectory() as d:
            base=pathlib.Path(d);data=base/'data';backup=base/'backup';data.mkdir();backup.mkdir()
            (data/'languagepacks.json').write_bytes(b'{"previous":true}')
            records=snapshot_settings(backup,data)
            (data/'languagepacks.json').write_bytes(b'{"cliInstalledPack":true}')
            cache='clp/'+'a'*32+'.zh-cn/tcf.json'
            snapshot_settings(backup,data,extra=[cache],include_base=False)
            restore_settings(backup,data,records,force=True)
            self.assertEqual((data/'languagepacks.json').read_bytes(),b'{"previous":true}')


if __name__=='__main__':unittest.main()
