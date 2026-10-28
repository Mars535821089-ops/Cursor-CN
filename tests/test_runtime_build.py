import gzip
import io
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'scripts'))
from patch_engine import PatchError,sha256
from runtime_build import fetch_verified,extract_zip


class RuntimeTests(unittest.TestCase):
    def test_marketplace_transport_gzip_is_decoded_before_hash_verification(self):
        data=b'PK expected verified VSIX contents'
        spec={'url':'https://example.invalid/package','sha256':sha256(data)}
        with tempfile.TemporaryDirectory() as temp:
            with patch('urllib.request.urlopen',return_value=io.BytesIO(gzip.compress(data))):
                result=fetch_verified(spec,pathlib.Path(temp))
            self.assertEqual(result.read_bytes(),data)

    def test_unverified_download_is_not_cached(self):
        spec={'url':'https://example.invalid/package','sha256':sha256(b'expected')}
        with tempfile.TemporaryDirectory() as temp:
            with patch('urllib.request.urlopen',return_value=io.BytesIO(b'wrong')):
                with self.assertRaises(PatchError):fetch_verified(spec,pathlib.Path(temp))
            self.assertEqual(list(pathlib.Path(temp).iterdir()),[])


if __name__=='__main__':unittest.main()
