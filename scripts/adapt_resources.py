"""Build and preview a signed-sample resource adaptation, with no application writes."""
import argparse
import gzip
import json
import pathlib
import plistlib
import re
import subprocess
import sys
import zipfile
from collections import Counter

from patch_engine import PatchError, apply_bytes, atomic_write, safe_path, sha256
from runtime_build import ROOT, load_profile
from ui_review import PLACEHOLDERS, literal_patch, load_translations


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def check_translation(source, translated):
    if not isinstance(translated, str):
        raise PatchError('Non-string language-pack value')
    if Counter(PLACEHOLDERS.findall(source)) != Counter(PLACEHOLDERS.findall(translated)):
        raise PatchError('Adapted translation changes placeholders')
    return translated


def adapt_nls(english, keys, contents, reviewed, old_entries):
    if sum(len(names) for _, names in keys) != len(english):
        raise PatchError('NLS index length mismatch')
    by_hash = {}
    for entry in old_entries:
        digest, target = entry['source_sha256'], entry['translation']
        if digest in by_hash and by_hash[digest] != target:
            raise PatchError('Conflicting prior NLS translations for an exact source hash')
        by_hash[digest] = target
    result, changes = [], []
    counts = {'official': 0, 'reviewed': 0, 'exact_source_migration': 0,
              'placeholder_rejections': 0}
    for module, names in keys:
        translations = contents.get(module, {})
        for name in names:
            index = len(result)
            source = english[index]
            translated = source
            official = translations.get(name, source)
            # A newer pack may have changed a message under the same key.
            if (not isinstance(official, str) or
                    Counter(PLACEHOLDERS.findall(source)) != Counter(PLACEHOLDERS.findall(official))):
                counts['placeholder_rejections'] += 1
                official = source
            elif official != source:
                translated = official
                counts['official'] += 1
            if translated == source:
                if source in reviewed:
                    translated = check_translation(source, reviewed[source])
                    counts['reviewed'] += 1
                elif sha256(source.encode()) in by_hash:
                    translated = check_translation(source, by_hash[sha256(source.encode())])
                    if translated != source:
                        counts['exact_source_migration'] += 1
            result.append(translated)
            if translated != official:
                changes.append({'index': index, 'source_sha256': sha256(source.encode()),
                                'translation': translated})
    return result, changes, counts


def verify_sample(app):
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    signature = subprocess.run(['codesign', '-dv', '--verbose=4', str(app)],
                               capture_output=True, text=True, check=True).stderr
    if ('TeamIdentifier=VDXQ22DGB9' not in signature or
            'Authority=Developer ID Application: Hilary Stout (VDXQ22DGB9)' not in signature):
        raise PatchError('Sample signer is not the expected Cursor publisher')
    subprocess.run(['xcrun', 'stapler', 'validate', str(app)], check=True)
    assessment = subprocess.run(['spctl', '--assess', '--type', 'execute', '--verbose=2', str(app)],
                                capture_output=True, text=True, check=True)
    status = subprocess.run(['spctl', '--status'], capture_output=True, text=True)
    policy = (status.stdout + status.stderr).strip()
    if (status.returncode, policy) not in {(0, 'assessments enabled'), (1, 'assessments disabled')}:
        raise PatchError('Could not establish Gatekeeper policy status')
    return {'team_id': 'VDXQ22DGB9', 'authority': 'Hilary Stout',
            'codesign_deep_strict': True, 'stapled_notarization_valid': True,
            'gatekeeper_assessment_accepted': True,
            'gatekeeper_assessments_disabled': policy == 'assessments disabled',
            'gatekeeper_override_reported': 'override=' in assessment.stderr}


def verify_mounted_image(app, dmg):
    images = plistlib.loads(subprocess.run(['hdiutil', 'info', '-plist'],
                                          capture_output=True, check=True).stdout)['images']
    if not any(pathlib.Path(image.get('image-path', '')).resolve() == dmg.resolve() and
               image.get('writeable') is False and
               any(app.resolve().is_relative_to(pathlib.Path(entity['mount-point']).resolve())
                   for entity in image.get('system-entities', []) if 'mount-point' in entity)
               for image in images):
        raise PatchError('Signed sample is not mounted read-only from the specified disk image')


def build(args):
    profile_directory = args.profile.absolute().parent
    if profile_directory != ROOT/'patches':
        raise PatchError('Generated profile must be in this repository patches directory')
    verify_mounted_image(args.app, args.dmg)
    signer = verify_sample(args.app)
    root = args.app/'Contents/Resources/app'
    package = json.loads((root/'package.json').read_text())
    product = json.loads((root/'product.json').read_text())
    audit = json.loads(args.audit.read_text())
    if (audit['version'], audit['commit']) != (package['version'], product['commit']):
        raise PatchError('Audit does not match signed sample')
    if not re.fullmatch(r'3\.27\.\d+', package['version']):
        raise PatchError('This adaptation builder is restricted to the reviewed 3.27 series')
    if args.profile.name != 'profile-' + package['version'] + '.json':
        raise PatchError('Use a version-specific profile filename; never replace the installation profile')
    previous = load_profile()
    vsix = args.vsix.read_bytes()
    if sha256(vsix) != previous['language_pack']['sha256']:
        raise PatchError('Language-pack SHA-256 mismatch')
    extra_dictionary = 'patches/reviewed-ui-3.27.json'
    source_dictionary = merge_dictionaries(['patches/reviewed-ui.json', extra_dictionary])
    combined_dictionary = merge_dictionaries(['patches/reviewed-ui.json', extra_dictionary], include_nls=True)
    reviewed = {}
    fingerprints = {}
    for item in audit['files']:
        relative = item['path']
        source = safe_path(root, relative).read_bytes()
        if sha256(source) != item['sha256']:
            raise PatchError('Audited resource fingerprint changed: ' + relative)
        fingerprints[relative] = item['sha256']
        delta = literal_patch(source, item['candidates'], source_dictionary)
        if delta['edits']:
            apply_bytes(source, delta)
            reviewed[relative] = delta
    for relative in ['out/nls.keys.json', 'out/nls.messages.json', 'package.json', 'product.json']:
        fingerprints[relative] = sha256(safe_path(root, relative).read_bytes())
    english = json.loads((root/'out/nls.messages.json').read_text())
    keys = json.loads((root/'out/nls.keys.json').read_text())
    with zipfile.ZipFile(args.vsix) as archive:
        contents = json.loads(archive.read('extension/translations/main.i18n.json'))['contents']
    old_overlay = json.loads((ROOT/previous['nls_overlay']).read_text())
    nls, entries, counts = adapt_nls(english, keys, contents, combined_dictionary, old_overlay['entries'])
    version = package['version']
    patch_name = 'patches/resources-' + version + '-arm64.json.gz'
    overlay_name = 'patches/nls-' + version + '.json'
    remaining_name = 'patches/remaining-' + version + '.json'
    patch_data = gzip.compress(json_bytes({'files': reviewed}), mtime=0)
    overlay_data = (json.dumps({'entries': entries, 'after_sha256': sha256(json_bytes(nls))},
                               ensure_ascii=False, indent=2)+'\n').encode()
    remaining = {
        'classification': 'unreviewed_candidates_not_confirmed_omissions',
        'nls': [{'index': i, 'source_sha256': sha256(english[i].encode())}
                for i, text in enumerate(nls) if re.search('[a-zA-Z]{2}', text)
                and not re.search('[\u3400-\u9fff]', text)],
        'dynamic_templates': [
            {'path': f['path'], 'start': c['start'], 'end': c['end'],
             'segments_sha256': sha256(json_bytes(c['segments'])), 'expressions': c['expressions']}
            for f in audit['files'] for c in f['dynamicCandidates']],
        'remaining_static_candidates_by_file': {
            f['path']: sum(c['value'] not in source_dictionary for c in f['candidates'])
            for f in audit['files'] if any(c['value'] not in source_dictionary for c in f['candidates'])},
    }
    remaining['language_pack_placeholder_rejections'] = []
    index = 0
    for module, names in keys:
        for name in names:
            value = contents.get(module, {}).get(name, english[index])
            if (not isinstance(value, str) or Counter(PLACEHOLDERS.findall(english[index])) !=
                    Counter(PLACEHOLDERS.findall(value))):
                remaining['language_pack_placeholder_rejections'].append(
                    {'index': index, 'source_sha256': sha256(english[index].encode())})
            index += 1
    remaining_data = (json.dumps(remaining, ensure_ascii=False, indent=2)+'\n').encode()
    profile = {
        'schema': 1, 'mode': 'verified_resource_preview_only',
        'cursor_version': version, 'cursor_commit': product['commit'],
        'platform': 'darwin', 'arch': 'arm64', 'installation_supported': False,
        'source': {'download_url': args.source_url, 'dmg_sha256': sha256(args.dmg.read_bytes()),
                   'sample_read_only_mount_verified': True,
                   'signer': signer},
        'ui_review_patch': patch_name, 'ui_review_dictionary': 'patches/reviewed-ui.json',
        'ui_review_extra_dictionary': extra_dictionary,
        'nls_overlay': overlay_name, 'language_pack': previous['language_pack'],
        'remaining_candidates': remaining_name,
        'resource_fingerprints': fingerprints,
        'artifacts': {patch_name: sha256(patch_data), overlay_name: sha256(overlay_data),
                      remaining_name: sha256(remaining_data),
                      extra_dictionary: sha256((ROOT/extra_dictionary).read_bytes()),
                      'patches/reviewed-ui.json': sha256((ROOT/'patches/reviewed-ui.json').read_bytes())},
        'coverage': {'scanned_resources': len(audit['files']), 'changed_resources': len(reviewed),
                     'literal_corrections': sum(len(p['edits']) for p in reviewed.values()),
                     'nls_entries': len(nls), 'nls_changes': len(entries), 'nls_sources': counts,
                     'literal_candidates_remaining': sum(
                         c['value'] not in source_dictionary
                         for f in audit['files'] for c in f['candidates']),
                     'dynamic_ui_templates': sum(len(f['dynamicCandidates']) for f in audit['files']),
                     'nls_english_candidates_remaining': sum(
                         bool(re.search('[a-zA-Z]{2}', text)) and not
                         bool(re.search('[\u3400-\u9fff]', text)) for text in nls)},
        'limitations': ['No application writes, installation, signing, or runtime injection.',
                        'Dynamic UI, remote content and remaining English candidates need further review.',
                        'Language-pack values with changed placeholders are rejected and counted.',
                        'Full installation and GUI validation have not been performed.'],
    }
    atomic_write(ROOT/patch_name, patch_data)
    atomic_write(ROOT/overlay_name, overlay_data)
    atomic_write(ROOT/remaining_name, remaining_data)
    atomic_write(args.profile, json.dumps(profile, ensure_ascii=False, indent=2).encode()+b'\n')
    print(json.dumps(profile['coverage'], indent=2))


def merge_dictionaries(paths, include_nls=False):
    combined = {}
    for path in paths:
        for source, target in load_translations(safe_path(ROOT, path), include_nls=include_nls).items():
            if source in combined and combined[source] != target:
                raise PatchError('Conflicting adaptation dictionary translation: ' + source)
            combined[source] = target
    return combined


def load_resource_profile(profile_path):
    profile = json.loads(profile_path.read_text())
    if (profile.get('schema') != 1 or profile.get('mode') != 'verified_resource_preview_only'
            or profile.get('installation_supported') is not False):
        raise PatchError('Unsupported resource preview profile')
    for relative, expected in profile['artifacts'].items():
        if sha256(safe_path(ROOT, relative).read_bytes()) != expected:
            raise PatchError('Adaptation artifact fingerprint mismatch: ' + relative)
    return profile


def preview(app, profile_path, vsix):
    profile = load_resource_profile(profile_path)
    root = app/'Contents/Resources/app'
    package = json.loads((root/'package.json').read_text())
    product = json.loads((root/'product.json').read_text())
    if (package['version'], product['commit']) != (profile['cursor_version'], profile['cursor_commit']):
        raise PatchError('Sample version/build mismatch; no application writes')
    for relative, expected in profile['resource_fingerprints'].items():
        if sha256(safe_path(root, relative).read_bytes()) != expected:
            raise PatchError('Sample resource fingerprint mismatch: ' + relative)
    patches = json.loads(gzip.decompress((ROOT/profile['ui_review_patch']).read_bytes()))['files']
    for relative, delta in patches.items():
        apply_bytes(safe_path(root, relative).read_bytes(), delta)
    overlay = json.loads((ROOT/profile['nls_overlay']).read_text())
    english = json.loads((root/'out/nls.messages.json').read_text())
    keys = json.loads((root/'out/nls.keys.json').read_text())
    if sha256(vsix.read_bytes()) != profile['language_pack']['sha256']:
        raise PatchError('Language-pack SHA-256 mismatch')
    with zipfile.ZipFile(vsix) as archive:
        contents = json.loads(archive.read('extension/translations/main.i18n.json'))['contents']
    nls, _, _ = adapt_nls(english, keys, contents, {}, [])
    for entry in overlay['entries']:
        index = entry['index']
        if sha256(english[index].encode()) != entry['source_sha256']:
            raise PatchError('Sample NLS source mismatch')
        nls[index] = check_translation(english[index], entry['translation'])
    if sha256(json_bytes(nls)) != overlay['after_sha256']:
        raise PatchError('Adapted NLS fingerprint mismatch')
    return {'mode': profile['mode'], 'version': profile['cursor_version'],
            'commit': profile['cursor_commit'], 'all_input_fingerprints_match': True,
            'no_application_writes': True, 'installation_supported': False,
            'files': [{'path': name, 'edits': len(delta['edits']),
                       'before_sha256': delta['before_sha256'], 'after_sha256': delta['after_sha256']}
                      for name, delta in patches.items()], 'coverage': profile['coverage']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='签名样本资源适配与只读预览；不安装、不签名')
    parser.add_argument('action', choices=['build', 'preview'])
    parser.add_argument('--app', type=pathlib.Path, required=True)
    parser.add_argument('--profile', type=pathlib.Path, required=True)
    parser.add_argument('--audit', type=pathlib.Path)
    parser.add_argument('--vsix', type=pathlib.Path)
    parser.add_argument('--dmg', type=pathlib.Path)
    parser.add_argument('--source-url')
    args = parser.parse_args(argv)
    if args.action == 'build':
        if not all([args.audit, args.vsix, args.dmg, args.source_url]):
            parser.error('build requires --audit --vsix --dmg --source-url')
        if not re.fullmatch(r'https://downloads\.cursor\.com/production/[0-9a-f]{40}/darwin/arm64/Cursor-darwin-arm64\.dmg',
                            args.source_url):
            raise PatchError('Unexpected official sample URL')
        build(args)
    else:
        if not args.vsix:
            parser.error('preview requires --vsix (the pinned official language-pack archive)')
        print(json.dumps(preview(args.app, args.profile, args.vsix), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (PatchError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print('Resource adaptation stopped: ' + str(exc), file=sys.stderr)
        sys.exit(1)
