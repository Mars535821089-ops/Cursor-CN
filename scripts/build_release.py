"""Maintainer-only builder. Inputs are explicit; no machine-specific paths are published."""
import argparse
import base64
import datetime
import gzip
import json
import pathlib
import plistlib
import re
import subprocess
import sys
import zipfile

from patch_engine import create_delta, sha256
from runtime_build import raw_runtime, build_nls


def write_patch(path, data):
    path.write_bytes(gzip.compress(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))


def main():
    parser = argparse.ArgumentParser()
    for arg in ['original-app', 'patched-app', 'pristine-upstream', 'modified-upstream', 'upstream-zip', 'nls-cache']:
        parser.add_argument('--'+arg, required=True, type=pathlib.Path)
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parents[1]
    patches = root / 'patches'
    original = args.original_app / 'Contents/Resources/app'
    current = args.patched_app / 'Contents/Resources/app'
    relative_files = ['out/main.js', 'out/vs/workbench/workbench.desktop.main.js',
        'out/vs/workbench/workbench.glass.main.js', 'out/vs/workbench/workbench.anysphere-ui-automations.js',
        'out/vs/workbench/workbench.xai-commerce-prepaid.js',
        'out/vs/code/electron-sandbox/workbench/workbench.html']
    files = {}
    for rel in relative_files:
        before, after = (original / rel).read_bytes(), (current / rel).read_bytes()
        files[rel] = create_delta(before, after)
        print('APP_DELTA', rel, len(files[rel]['edits']), flush=True)
    write_patch(patches/'app-3.24.18-arm64.json.gz', {'files': files})
    upstream_files = {}
    for rel in ['localization/Core_Dictionary.json', 'localization/runtime/bootstrap.js',
                'localization/runtime/market.js', 'localization/runtime/init.js', 'localization/runtime/engine.js']:
        upstream_files[rel] = create_delta((args.pristine_upstream/rel).read_bytes(), (args.modified_upstream/rel).read_bytes())
    write_patch(patches/'upstream-customizations.json.gz', {'files': upstream_files})
    runtime_rel = 'out/vs/code/electron-sandbox/workbench/Cursor_Localization.js'
    runtime = (current/runtime_rel).read_bytes()
    timestamp = re.search(rb'Generated: (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)', runtime).group(1).decode()
    raw = raw_runtime(args.modified_upstream, args.patched_app, timestamp)
    write_patch(patches/'runtime-safety.json.gz', {'files': {'runtime.js': create_delta(raw, runtime)}})
    vsix = args.modified_upstream/'VSCode-language-pack-zh-hans.vsix'
    with zipfile.ZipFile(vsix) as z:
        version = json.loads(z.read('extension/package.json'))['version']
        contents = json.loads(z.read('extension/translations/main.i18n.json'))['contents']
    english = json.loads((original/'out/nls.messages.json').read_text())
    keys = json.loads((original/'out/nls.keys.json').read_text())
    baseline = []
    for module, names in keys:
        translations = contents.get(module, {})
        for name in names:
            value = translations.get(name)
            baseline.append(value if isinstance(value, str) else english[len(baseline)])
    final = json.loads(args.nls_cache.read_text())
    assert len(baseline) == len(final) == len(english)
    nls = {'entries': [{'index': i, 'source_sha256': sha256(english[i].encode()), 'translation': value}
                       for i, value in enumerate(final) if baseline[i] != value],
           'after_sha256': sha256(json.dumps(final, ensure_ascii=False, separators=(',', ':')).encode())}
    (patches/'nls-overrides.json').write_text(json.dumps(nls, ensure_ascii=False, indent=2)+'\n')
    product = json.loads((original/'product.json').read_text())
    profile = {'schema': 1, 'patch_version': '1.0.0', 'cursor_version': '3.24.18',
        'cursor_commit': product['commit'], 'platform': 'darwin', 'arch': 'arm64',
        'build_time': timestamp, 'runtime_path': runtime_rel, 'runtime_sha256': sha256(runtime),
        'app_patch': 'patches/app-3.24.18-arm64.json.gz',
        'upstream_patch': 'patches/upstream-customizations.json.gz',
        'runtime_patch': 'patches/runtime-safety.json.gz',
        'nls_overlay': 'patches/nls-overrides.json',
        'upstream': {'commit': '88f93c685ac5d1bc645fbb5c3e3cc1ff1c16e2a7',
            'url': 'https://codeload.github.com/vibepm666/cursor-localization-zh/zip/88f93c685ac5d1bc645fbb5c3e3cc1ff1c16e2a7',
            'sha256': sha256(args.upstream_zip.read_bytes())},
        'language_pack': {'version': version, 'url': 'https://marketplace.visualstudio.com/_apis/public/gallery/publishers/MS-CEINTL/vsextensions/vscode-language-pack-zh-hans/'+version+'/vspackage',
                         'sha256': sha256(vsix.read_bytes())},
        'entitlements': {}, 'artifacts': {}}
    app_paths = ['.', 'Contents/Frameworks/Cursor Helper.app',
        'Contents/Frameworks/Cursor Helper (GPU).app', 'Contents/Frameworks/Cursor Helper (Plugin).app',
        'Contents/Frameworks/Cursor Helper (Renderer).app']
    for rel in app_paths:
        p = subprocess.run(['codesign', '-d', '--entitlements', ':-', str(args.patched_app/rel)], capture_output=True, check=True)
        profile['entitlements'][rel] = plistlib.loads(p.stdout)
    for p in sorted(patches.iterdir()):
        if p.name != 'profile.json' and p.is_file():
            profile['artifacts'][p.relative_to(root).as_posix()] = sha256(p.read_bytes())
    (patches/'profile.json').write_text(json.dumps(profile, ensure_ascii=False, indent=2)+'\n')
    assert build_nls(args.original_app, vsix, nls)
    print('RELEASE_PAYLOADS', {p.name:p.stat().st_size for p in patches.iterdir()}, flush=True)


if __name__ == '__main__':
    main()
