"""Maintainer-only supplemental delta builder from an explicitly audited, known patched app."""
import argparse
import gzip
import json
import pathlib

from patch_engine import PatchError, apply_bytes, atomic_write, safe_path, sha256
from runtime_build import ROOT, read_patch
from ui_review import literal_patch, load_translations, review_nls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', type=pathlib.Path, required=True)
    parser.add_argument('--audit', type=pathlib.Path, required=True)
    parser.add_argument('--nls-cache', type=pathlib.Path, required=True)
    args = parser.parse_args()
    profile = json.loads((ROOT/'patches/profile.json').read_text(encoding='utf-8'))
    for relative, expected in profile['artifacts'].items():
        if relative in {'patches/reviewed-ui.json', 'patches/ui-review-3.24.18.json.gz'}:
            continue
        if sha256((ROOT/relative).read_bytes()) != expected:
            raise PatchError('Base artifact fingerprint changed: ' + relative)
    audit = json.loads(args.audit.read_text(encoding='utf-8'))
    if (audit.get('version'), audit.get('commit')) != (profile['cursor_version'], profile['cursor_commit']):
        raise PatchError('Audit does not match the supported build')
    root = args.app/'Contents/Resources/app'
    base = read_patch(profile['app_patch'])['files']
    translations = load_translations(ROOT/'patches/reviewed-ui.json')
    reviewed = {}
    for item in audit['files']:
        relative = item['path']
        source = safe_path(root, relative).read_bytes()
        if sha256(source) != item['sha256'] or (relative in base and sha256(source) != base[relative]['after_sha256']):
            raise PatchError('Audit or app fingerprint changed: ' + relative)
        patch = literal_patch(source, item['candidates'], translations)
        if patch['edits']:
            apply_bytes(source, patch)
            reviewed[relative] = patch
    overlay = json.loads((ROOT/profile['nls_overlay']).read_text(encoding='utf-8'))
    encoded = args.nls_cache.read_bytes()
    if sha256(encoded) != overlay['after_sha256']:
        raise PatchError('Baseline NLS fingerprint changed')
    translated = json.loads(encoded)
    nls_translations = load_translations(ROOT/'patches/reviewed-ui.json', include_nls=True)
    final, changed = review_nls(translated, translated, nls_translations)
    profile['reviewed_nls_sha256'] = sha256(json.dumps(final, ensure_ascii=False, separators=(',', ':')).encode())
    relative = 'patches/ui-review-3.24.18.json.gz'
    profile['ui_review_patch'] = relative
    profile['ui_review_dictionary'] = 'patches/reviewed-ui.json'
    data = gzip.compress(json.dumps({'files': reviewed}, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0)
    atomic_write(ROOT/relative, data)
    for name in [relative, profile['ui_review_dictionary']]:
        profile['artifacts'][name] = sha256((ROOT/name).read_bytes())
    atomic_write(ROOT/'patches/profile.json', (json.dumps(profile, ensure_ascii=False, indent=2)+'\n').encode())
    print(json.dumps({'files': {name: len(patch['edits']) for name, patch in reviewed.items()},
                      'nls_corrections': len(changed), 'dictionary_entries': len(translations),
                      'nls_dictionary_entries': len(nls_translations)}, indent=2))


if __name__ == '__main__':
    main()
