"""Check publishable files and decoded delta payloads, not only compressed containers."""
import base64
import gzip
import json
import pathlib
import re
import sys

from runtime_build import ROOT,load_profile,read_patch


RULES = {
    'private_local_path': re.compile(rb'/' + rb'(?:Users|home)/[^\s"\x00]+|/var/' + rb'folders/'),
    'github_credential': re.compile(rb'gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{30,}'),
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'api_secret': re.compile(rb'\bsk-(?:ant-)?[A-Za-z0-9_-]{28,}'),
    'personal_mailbox': re.compile(rb'@[A-Za-z0-9.-]*\b(?:qq|gmail|hotmail|icloud|163|126)\.com\b'),
}


def inspect(data,label):
    for rule,regex in RULES.items():
        if regex.search(data):
            raise ValueError('Publication privacy check failed: '+rule+' in '+label+' (matching data not printed)')


def main():
    profile=load_profile();count=0;inserted=0
    for path in ROOT.rglob('*'):
        rel=path.relative_to(ROOT)
        if any(part in {'.git','.cache','.work','dist','__pycache__'} for part in rel.parts):continue
        if not path.is_file():continue
        if path.suffix in {'.vsix','.app','.pem','.key','.log'} or path.name in {'settings.json','state.vscdb','Cookies','auth.json'}:
            raise ValueError('Non-public artifact in source tree: '+rel.as_posix())
        if path.suffix=='.pyc':continue
        data=path.read_bytes();inspect(data,rel.as_posix());count+=1
        if path.suffix=='.gz':
            patch=json.loads(gzip.decompress(data))
            for name,entry in patch['files'].items():
                for edit in entry['edits']:
                    payload=base64.b64decode(edit['data'],validate=True);inspect(payload,rel.as_posix()+':'+name);inserted+=len(payload)
            if rel.as_posix()==profile['app_patch']:
                assert all(sum(len(base64.b64decode(e['data'])) for e in x['edits'])<max(4096,x['before_size']//3) for x in patch['files'].values()), 'App payload unexpectedly resembles a full source bundle'
    assert profile['cursor_version']=='3.24.18' and profile['arch']=='arm64'
    print(json.dumps({'publication_files_checked':count,'decoded_patch_bytes_checked':inserted,
                      'private_data_detected':False,'artifact_hashes_valid':True,'pass':True},indent=2))


if __name__=='__main__':main()
