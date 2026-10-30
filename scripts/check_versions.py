"""Opt-in official release-series check; never download or modify an application."""
import argparse
import datetime
import json
import pathlib
import re
import sys
import urllib.request

from install import maintenance_report
from patch_engine import PatchError
from runtime_build import ROOT, load_app_patch, load_profile
from adapt_resources import load_resource_profile


SOURCE = 'https://cursor.com/download'
MAX_PAGE_BYTES = 2 * 1024 * 1024


def parse_latest(html):
    # Refuse changed markup rather than mistaking a historical download for latest.
    versions = set(re.findall(
        r'<span\b[^>]*>\s*(\d+\.\d+)\s*</span>\s*'
        r'<span\b[^>]*>\s*Latest\s*</span>', html))
    if len(versions) != 1:
        raise PatchError('Official latest-series marker is missing or ambiguous')
    version = versions.pop()
    url = 'https://api2.cursor.sh/updates/download/golden/darwin-arm64/cursor/' + version
    if not re.search(r'href=["\']' + re.escape(url) + r'["\']', html):
        raise PatchError('Official latest-series ARM64 download link is missing')
    return {'series': version, 'full_version': None, 'build_commit': None,
            'source': SOURCE, 'download_url': url,
            'precision': 'release_series_only', 'resource_fingerprints_verified': False}


def fetch_latest():
    request = urllib.request.Request(SOURCE, headers={'User-Agent': 'Cursor-CN-Version-Check/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.geturl() not in {SOURCE, SOURCE + '/'}:
            raise PatchError('Unexpected official download-page redirect')
        data = response.read(MAX_PAGE_BYTES + 1)
    if len(data) > MAX_PAGE_BYTES:
        raise PatchError('Official download page exceeds expected size')
    return parse_latest(data.decode('utf-8'))


def series_relation(version, latest):
    match = re.fullmatch(r'(\d+)\.(\d+)(?:\.\d+)?', version)
    if not match:
        raise PatchError('Invalid local version: ' + version)
    local = tuple(map(int, match.groups()))
    remote = tuple(map(int, latest.split('.')))
    return ('behind' if local < remote else 'ahead' if local > remote
            else 'same_series_patch_level_unknown')


def version_report(app, profile, files, latest, previews=None):
    local = maintenance_report(app, profile, files)
    installed = local['observed']['version']
    if not isinstance(installed, str):
        raise PatchError('Installed application version is missing')
    return {
        'checked_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'official': latest,
        'installed': local['observed'],
        'repository_supported': local['expected'],
        'installed_vs_official_series': series_relation(installed, latest['series']),
        'repository_vs_official_series': series_relation(profile['cursor_version'], latest['series']),
        'local_patch_state': local['state'],
        'local_patch_supported': local['supported'],
        'resource_previews': [
            {'version': item['cursor_version'], 'commit': item['cursor_commit'],
             'vs_official_series': series_relation(item['cursor_version'], latest['series']),
             'installation_supported': item['installation_supported']}
            for item in (previews or [])],
        'no_application_writes': True,
        'automatic_upgrade_or_patch': False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读联网核对 Cursor 官方最新系列；不升级或应用补丁')
    parser.add_argument('--app', type=pathlib.Path, default=pathlib.Path('/Applications/Cursor.app'))
    args = parser.parse_args(argv)
    profile = load_profile()
    previews = [load_resource_profile(path) for path in sorted((ROOT/'patches').glob('profile-*.json'))]
    report = version_report(args.app, profile, load_app_patch(profile), fetch_latest(), previews)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if ('behind' in {report['installed_vs_official_series'],
                     report['repository_vs_official_series']} or not report['local_patch_supported']):
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (PatchError, OSError, ValueError) as exc:
        print('Version check stopped: ' + str(exc), file=sys.stderr)
        sys.exit(1)
