"""Reproduce the audited runtime without redistributing the upstream program."""
import datetime
import gzip
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import types
import urllib.request
import zipfile

from patch_engine import PatchError, apply_bytes, apply_tree, atomic_write, sha256


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_profile(root=ROOT):
    profile = json.loads((root / 'patches/profile.json').read_text())
    if profile['schema'] != 1:
        raise PatchError('不支持的补丁格式')
    for relative, digest in profile['artifacts'].items():
        if sha256((root / relative).read_bytes()) != digest:
            raise PatchError('补丁包完整性校验失败：' + relative)
    return profile


def read_patch(relative, root=ROOT):
    return json.loads(gzip.decompress((root / relative).read_bytes()))


def fetch_verified(spec, cache):
    cache = pathlib.Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (spec['sha256'] + '.zip')
    if path.exists():
        if sha256(path.read_bytes()) != spec['sha256']:
            raise PatchError('缓存校验失败，请删除此单个缓存文件后重试：' + str(path))
        return path
    if not spec['url'].startswith('https://'):
        raise PatchError('依赖下载必须使用 HTTPS')
    request = urllib.request.Request(spec['url'], headers={'User-Agent': 'Cursor-Chinese-Patch/1.0'})
    chunks = []
    size = 0
    with urllib.request.urlopen(request, timeout=90) as response:
        while True:
            chunk = response.read(1024*1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 30*1024*1024:
                raise PatchError('下载文件超出预期大小')
            chunks.append(chunk)
    data = b''.join(chunks)
    # Marketplace serves a gzip-encoded VSIX on some routes. Verify the actual
    # ZIP bytes, as used by the audited upstream downloader, not transport gzip.
    if data.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as decoded:
            data = decoded.read(30*1024*1024+1)
        if len(data) > 30*1024*1024:
            raise PatchError('依赖解压后的大小异常')
    if sha256(data) != spec['sha256']:
        raise PatchError('依赖 SHA-256 不匹配，已停止；不会运行该下载内容')
    atomic_write(path, data)
    return path


def extract_zip(archive, target, strip_first=False):
    target = pathlib.Path(target)
    expanded = 0
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 10000:
            raise PatchError('压缩包文件数异常')
        for info in z.infolist():
            parts = pathlib.PurePosixPath(info.filename).parts
            if info.filename.startswith('/') or '..' in parts or '\\' in info.filename:
                raise PatchError('压缩包包含危险路径')
            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                raise PatchError('压缩包不能包含符号链接')
            if strip_first:
                parts = parts[1:]
            if not parts or info.is_dir():
                continue
            expanded += info.file_size
            if expanded > 100*1024*1024:
                raise PatchError('压缩包展开大小异常')
            path = target.joinpath(*parts)
            atomic_write(path, z.read(info), mode=0o600)


def raw_runtime(localizer, app, build_time):
    """Only invoke the pinned tool's pure text-generation function, never its installer."""
    names = {'CURSOR_INSTALL_DIR': str(app), 'CURSOR_USER_DATA_DIR': str(localizer / '.unused-user-data')}
    previous = {k: os.environ.get(k) for k in names}
    os.environ.update(names)
    try:
        spec = importlib.util.spec_from_file_location('cursor_zh_pinned_builder', localizer / 'Cursor_Localization_Tool.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixed = datetime.datetime.strptime(build_time, '%Y-%m-%d %H:%M:%S')
        class Frozen(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed
        module.datetime = types.SimpleNamespace(datetime=Frozen)
        return module.ShengCheng_JS_DaiMa().encode('utf-8')
    finally:
        for k, value in previous.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value


def build_runtime(profile, app, cache, root=ROOT):
    archive = fetch_verified(profile['upstream'], cache)
    with tempfile.TemporaryDirectory(prefix='cursor-zh-build-', dir=cache) as temp:
        localizer = pathlib.Path(temp)
        extract_zip(archive, localizer, strip_first=True)
        patch = read_patch(profile['upstream_patch'], root)
        apply_tree(localizer, patch['files'])
        generated = raw_runtime(localizer, app, profile['build_time'])
        result = apply_bytes(generated, read_patch(profile['runtime_patch'], root)['files']['runtime.js'])
    if sha256(result) != profile['runtime_sha256']:
        raise PatchError('运行时重建结果与已审核版本不一致')
    return result


def build_nls(app, vsix, overlay):
    app_root = pathlib.Path(app) / 'Contents/Resources/app'
    keys = json.loads((app_root / 'out/nls.keys.json').read_text())
    english = json.loads((app_root / 'out/nls.messages.json').read_text())
    with zipfile.ZipFile(vsix) as z:
        contents = json.loads(z.read('extension/translations/main.i18n.json'))['contents']
    result = []
    for module, names in keys:
        translations = contents.get(module, {})
        for name in names:
            value = translations.get(name)
            result.append(value if isinstance(value, str) else english[len(result)])
    if len(result) != len(english):
        raise PatchError('语言包索引长度不匹配')
    for entry in overlay['entries']:
        index = entry['index']
        if sha256(english[index].encode()) != entry['source_sha256']:
            raise PatchError('语言包索引原文与当前版本不匹配')
        result[index] = entry['translation']
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
    if sha256(encoded) != overlay['after_sha256']:
        raise PatchError('语言包重建校验失败')
    return encoded
