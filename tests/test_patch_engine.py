import base64
import hashlib
import pathlib
import random
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
from patch_engine import PatchError, apply_bytes, create_delta, apply_tree


class PatchTests(unittest.TestCase):
    def round_trip(self, before, after):
        patch = create_delta(before, after)
        self.assertEqual(apply_bytes(before, patch), after)
        self.assertEqual(apply_bytes(after, patch), after)
        return patch

    def test_empty_insert_delete_replace(self):
        for a, b in [(b'', b'abc'), (b'abc', b''), (b'', b''), (b'hello', b'world')]:
            with self.subTest(a=a, b=b):
                self.round_trip(a, b)

    def test_utf8_offsets_are_bytes_not_python_character_indices(self):
        self.round_trip('图标🌙；label:"Open";路径'.encode(),
                        '图标🌙；label:"打开";路径'.encode())

    def test_repetitive_source_and_dense_ui_labels(self):
        before = b'title:"Open",label:"Close";' * 1200
        after = before.replace(b'"Open"', '"打开"'.encode()).replace(b'"Close"', '"关闭"'.encode())
        self.round_trip(before, after)

    def test_sparse_edits_remain_small(self):
        rng = random.Random(7)
        before = rng.randbytes(200000)
        after = before[:500] + '中文'.encode() + before[505:70000] + b'new' + before[70000:]
        patch = self.round_trip(before, after)
        self.assertLess(sum(len(base64.b64decode(e['data'])) for e in patch['edits']), 2000)

    def test_wrong_source_is_rejected(self):
        with self.assertRaises(PatchError):
            apply_bytes(b'not the supported version', create_delta(b'correct', b'patched'))

    def test_corrupted_payload_is_rejected(self):
        patch = create_delta(b'correct', b'patched')
        patch['edits'][0]['data'] = base64.b64encode(b'bad').decode()
        with self.assertRaises(PatchError):
            apply_bytes(b'correct', patch)

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(PatchError):
                apply_tree(pathlib.Path(d), {'../outside': create_delta(b'', b'bad')})

    def test_symlink_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / 'real').write_bytes(b'correct')
            (root / 'link').symlink_to(root / 'real')
            with self.assertRaises(PatchError):
                apply_tree(root, {'link': create_delta(b'correct', b'patched')})
            self.assertEqual((root / 'real').read_bytes(), b'correct')

    def test_all_files_are_validated_before_first_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / 'a').write_bytes(b'correct')
            (root / 'b').write_bytes(b'wrong')
            patch = create_delta(b'correct', b'patched')
            with self.assertRaises(PatchError):
                apply_tree(root, {'a': patch, 'b': patch})
            self.assertEqual((root / 'a').read_bytes(), b'correct')


if __name__ == '__main__':
    unittest.main()
