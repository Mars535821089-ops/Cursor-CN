#!/usr/bin/env python3
"""Transactional macOS installer. Never edits account/chat databases or extension scripts."""
import argparse
import base64
import datetime
import json
import os
import pathlib
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from patch_engine import PatchError, apply_tree, atomic_write, safe_path, sha256
from runtime_build import ROOT, build_nls, build_runtime, extract_zip, fetch_verified, load_profile, read_patch


SETTINGS = ('locale.json', 'User/locale.json', 'argv.json', 'languagepacks.json')
DEFAULT_DATA = pathlib.Path.home() / 'Library/Application Support/Cursor'
DEFAULT_BACKUPS = pathlib.Path.home() / 'Library/Application Support/CursorChinesePatch/Backups'
DEFAULT_CACHE = pathlib.Path.home() / 'Library/Caches/CursorChinesePatch'
DEFAULT_EXTENSIONS = pathlib.Path.home() / '.cursor/extensions'


def parse_jsonc(text):
    """Cursor argv JSONC: remove comments/terminal commas, never alter string contents."""
    chars=list(text);i=0;quoted=False;escaped=False
    while i<len(text):
        c=text[i]
        if quoted:
            if escaped:escaped=False
            elif c=='\\':escaped=True
            elif c=='"':quoted=False
            i+=1;continue
        if c=='"':quoted=True;i+=1;continue
        if text.startswith('//',i):
            end=text.find('\n',i)
            if end<0:end=len(text)
            chars[i:end]=' '*(end-i);i=end;continue
        if text.startswith('/*',i):
            end=text.find('*/',i+2)
            if end<0:raise PatchError('配置包含未闭合的注释，未修改')
            end+=2
            for k in range(i,end):
                if chars[k] not in '\r\n':chars[k]=' '
            i=end;continue
        i+=1
    clean=''.join(chars);quoted=escaped=False
    for i,c in enumerate(clean):
        if quoted:
            if escaped:escaped=False
            elif c=='\\':escaped=True
            elif c=='"':quoted=False
        elif c=='"':quoted=True
        elif c==',':
            j=i+1
            while j<len(clean) and clean[j].isspace():j+=1
            if j<len(clean) and clean[j] in '}]':chars[i]=' '
    return json.loads(''.join(chars))


def read_object(path):
    if not path.exists():
        return {}
    value = parse_jsonc(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise PatchError('配置不是 JSON 对象，未修改：' + path.name)
    return value


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode()


def app_root(app):
    return pathlib.Path(app) / 'Contents/Resources/app'


def classify_app(app, profile, files):
    app = pathlib.Path(app)
    if app.is_symlink():
        raise PatchError('请指定实际应用目录，不接受应用符号链接')
    root = app_root(app)
    package = read_object(root/'package.json')
    product = read_object(root/'product.json')
    if package.get('version') != profile['cursor_version'] or product.get('commit') != profile['cursor_commit']:
        raise PatchError('此补丁仅支持 Cursor '+profile['cursor_version']+' 的已验证构建；未修改应用')
    originals = patched = True
    for rel, patch in files.items():
        path = safe_path(root, rel)
        digest = sha256(path.read_bytes()) if path.is_file() else None
        originals &= digest == patch['before_sha256']
        patched &= digest == patch['after_sha256']
    runtime = safe_path(root, profile['runtime_path'])
    runtime_ok = runtime.is_file() and sha256(runtime.read_bytes()) == profile['runtime_sha256']
    if patched and runtime_ok:
        return 'patched'
    if originals and not runtime.exists():
        return 'original'
    raise PatchError('应用文件不匹配原版或已审核补丁，拒绝覆盖；请先恢复对应官方版本')


def update_checksums(root):
    path = pathlib.Path(root)/'product.json'
    product = read_object(path)
    for relative in product.get('checksums', {}):
        target = safe_path(root, relative if relative.startswith('out/') else 'out/'+relative)
        product['checksums'][relative] = base64.b64encode(bytes.fromhex(sha256(target.read_bytes()))).decode().rstrip('=')
    atomic_write(path, json_bytes(product))


def allowed_setting(relative):
    return relative in SETTINGS or bool(re.fullmatch(
        r'clp/[0-9a-f]{32}\.zh-cn/(?:tcf\.json|[0-9a-f]{40}/nls\.messages\.json)', relative))


def snapshot_settings(backup, data, extra=(), include_base=True):
    records = {}
    for rel in (SETTINGS if include_base else ()) + tuple(extra):
        if not allowed_setting(rel):
            raise PatchError('不允许备份或写入此配置路径')
        path = safe_path(data, rel)
        existed = path.is_file()
        content = path.read_bytes() if existed else b''
        records[rel] = {'existed': existed, 'before_sha256':sha256(content), 'after_sha256':None}
        if existed:
            atomic_write(safe_path(pathlib.Path(backup)/'settings',rel), content, mode=0o600)
    return records


def restore_settings(backup, data, records, force=False):
    skipped = []
    for rel, record in records.items():
        if not allowed_setting(rel):
            raise PatchError('恢复清单包含不允许的配置路径')
        path = safe_path(data, rel)
        current = path.read_bytes() if path.is_file() else b''
        if not force and record.get('after_sha256') is None:
            continue
        if not force and sha256(current) != record['after_sha256']:
            skipped.append(rel)
            continue
        if record['existed']:
            original = safe_path(pathlib.Path(backup)/'settings',rel).read_bytes()
            if sha256(original) != record['before_sha256']:
                raise PatchError('语言设置备份校验失败')
            atomic_write(path, original)
        elif path.exists():
            path.unlink()
    return skipped


def running(app):
    prefix = str(pathlib.Path(app).absolute()/'Contents/MacOS/Cursor')
    listing = subprocess.check_output(['ps','-axo','command='],text=True)
    return any(line.strip()==prefix or line.strip().startswith(prefix+' ') for line in listing.splitlines())


def quit_app(app, permitted):
    if not running(app):
        return
    if not permitted:
        raise PatchError('请先保存并退出 Cursor，或使用 --quit-cursor 请求正常退出；不会强制终止')
    escaped = str(app).replace('\\','\\\\').replace('"','\\"')
    subprocess.run(['osascript','-e','tell application "'+escaped+'" to quit'],check=True,timeout=30)
    for _ in range(30):
        if not running(app):
            return
        time.sleep(1)
    raise PatchError('Cursor 尚未退出，可能有未保存内容；未强制终止，未替换应用')


def clone_app(source, target):
    if target.exists() or target.is_symlink():
        raise PatchError('副本目标已存在，拒绝覆盖')
    target.parent.mkdir(parents=True,exist_ok=True)
    result = subprocess.run(['cp','-cR',str(source),str(target)],capture_output=True)
    if result.returncode:
        if target.exists():
            shutil.rmtree(target)
        subprocess.run(['cp','-pR',str(source),str(target)],check=True)


def verify_signature(app):
    subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True,capture_output=True)


def sign_app(app, profile):
    # Only this copied application and its Electron helpers are affected.
    # Never changes SIP, Gatekeeper, system keychains, or global security settings.
    with tempfile.TemporaryDirectory(prefix='cursor-zh-sign-') as temp:
        for i, rel in enumerate(sorted(profile['entitlements'],key=len,reverse=True)):
            path = pathlib.Path(temp)/('entitlements-'+str(i)+'.plist')
            path.write_bytes(plistlib.dumps(profile['entitlements'][rel]))
            command = ['codesign','--force','--sign','-',
                       '--preserve-metadata=identifier,flags,runtime','--entitlements',str(path)]
            if rel != '.':
                command.append('--deep')
            subprocess.run(command+[str(app/rel)],check=True,capture_output=True)
    verify_signature(app)


def prepare_app(app, output, profile, cache, files, backup_id=None):
    if classify_app(app,profile,files) != 'original':
        raise PatchError('准备副本需要原厂应用；现有汉化应用不会被重新签名')
    verify_signature(app)
    runtime = build_runtime(profile,app,cache)
    clone_app(app,output)
    root = app_root(output)
    apply_tree(root,files)
    atomic_write(safe_path(root,profile['runtime_path']),runtime,mode=0o644)
    if backup_id:
        atomic_write(root/'cursor-chinese-patch.json',json_bytes({
            'patch_version':profile['patch_version'],'backup_id':backup_id}),mode=0o644)
    update_checksums(root)
    sign_app(output,profile)
    if classify_app(output,profile,files) != 'patched':
        raise PatchError('隔离副本校验失败，未替换应用')


def language_plan(backup, data, profile, cache):
    vsix = fetch_verified(profile['language_pack'],cache)
    language = backup/'language-pack'
    extract_zip(vsix,language)
    extension = language/'extension'
    package = read_object(extension/'package.json')
    if package['version'] != profile['language_pack']['version']:
        raise PatchError('官方语言包版本不匹配')
    translations = {}
    for item in package['contributes']['localizations']:
        if item['languageId'].lower() != 'zh-cn':
            continue
        for entry in item['translations']:
            relative = pathlib.PurePosixPath(entry['path']).as_posix()
            translations[entry['id']] = str(safe_path(extension,relative))
    bridges = {
        'anysphere.cursor-always-local': {'displayName':'Cursor 始终本地','description':'为 Cursor 提供实验性本地功能。'},
        'anysphere.cursor-retrieval': {'displayName':'Cursor 检索','description':'处理 Cursor 的索引与检索能力。'},
        'anysphere.cursor-shadow-workspace': {'displayName':'Cursor 影子工作区','description':'管理供智能体整理代码的隐藏工作区。'},
        'inspecta.inspecta-ide-integration': {'displayName':'Inspecta IDE 集成','description':'将 Inspecta CSS 更改与编辑器集成。'}}
    for name, text in bridges.items():
        path = extension/'translations'/(name+'.i18n.json')
        atomic_write(path,json_bytes({'version':'1.0.0','contents':{'package':text}}))
        translations[name] = str(path)
    overlay = read_object(ROOT/profile['nls_overlay'])
    messages = build_nls(pathlib.Path(profile['_source_app']),vsix,overlay)
    digest = sha256((profile['patch_version']+profile['language_pack']['sha256']+overlay['after_sha256']).encode())[:32]
    cache_root = 'clp/'+digest+'.zh-cn/'
    messages_rel = cache_root+profile['cursor_commit']+'/nls.messages.json'
    tcf_rel = cache_root+'tcf.json'
    packs = read_object(data/'languagepacks.json')
    previous = packs.get('zh-cn',packs.get('zh-CN',{}))
    entry = dict(previous)
    entry.update({'hash':digest,'extensions':[{'extensionIdentifier':{'id':'ms-ceintl.vscode-language-pack-zh-hans'},
                                            'version':profile['language_pack']['version']}],
                  'translations':dict(previous.get('translations',{}),**translations)})
    packs['zh-cn'] = entry
    plan = {'languagepacks.json':json_bytes(packs),messages_rel:messages,tcf_rel:json_bytes(entry['translations'])}
    for rel in ('locale.json','User/locale.json','argv.json'):
        value = read_object(data/rel)
        value['locale'] = 'zh-cn'
        plan[rel] = json_bytes(value)
    return plan


def write_manifest(backup,manifest):
    atomic_write(backup/'manifest.json',json_bytes(manifest),mode=0o600)


def recovery_launcher(backup):
    shutil.copy2(__file__,backup/'install.py')
    shutil.copy2(ROOT/'scripts/patch_engine.py',backup/'patch_engine.py')
    shutil.copy2(ROOT/'scripts/runtime_build.py',backup/'runtime_build.py')
    command = '''#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then echo "请先安装 Python 3.9 或以上版本。"; exit 1; fi
"$PYTHON" install.py restore --backup "$PWD" --quit-cursor --yes
echo "恢复结束。按回车关闭窗口。"
read -r _
'''
    atomic_write(backup/'恢复官方原版.command',command.encode(),mode=0o755)


def install(args,profile,files):
    app,data = args.app.absolute(),args.user_data.absolute()
    state = classify_app(app,profile,files)
    if state == 'patched':
        print('已是此补丁的审核版本；不覆盖、不重新签名，也不会再次触发签名变更授权。')
        return
    if not args.yes and input('将修改此 Cursor 副本并使用本地签名，首次启动可能需系统确认。输入 INSTALL 继续：')!='INSTALL':
        raise PatchError('已取消，未修改')
    if not os.access(app.parent,os.W_OK):
        raise PatchError('应用所在目录不可写；请将 Cursor 放入当前用户可写的 Applications 目录，不会自动使用 sudo')
    quit_app(app,args.quit_cursor)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    backup = args.backup_root/stamp
    backup.mkdir(parents=True,mode=0o700)
    stage = app.parent/('.Cursor-Chinese-Patch-Stage-'+stamp+'.app')
    original = app.parent/('.Cursor-Chinese-Patch-Original-'+stamp+'.app')
    manifest = {'schema':1,'state':'preparing','app':str(app),'original_app':str(original),
                'user_data':str(data),'profile':profile,'files':files,'settings':{}}
    manifest['profile'] = {k:v for k,v in profile.items() if not k.startswith('_')}
    write_manifest(backup,manifest)
    recovery_launcher(backup)
    swapped = False
    try:
        print('正在准备隔离副本；现有应用和账号数据库不作原地修改。',flush=True)
        prepare_app(app,stage,profile,args.cache,files,backup_id=stamp)
        # Register the official pack with Cursor's own extension manager, so the
        # language-pack registry survives ordinary extension rescans. Capture
        # language settings before this CLI call; leave the official extension
        # installed on restore, as documented, never remove unrelated extensions.
        manifest['settings'] = snapshot_settings(backup,data)
        write_manifest(backup,manifest)
        archive = fetch_verified(profile['language_pack'],args.cache)
        vsix = backup/('official-zh-hans-'+profile['language_pack']['version']+'.vsix')
        shutil.copy2(archive,vsix)
        cli = app_root(app)/'bin/cursor'
        subprocess.run([str(cli),'--user-data-dir',str(data),'--extensions-dir',str(args.extensions_dir),
                        '--install-extension',str(vsix),'--force'],check=True,timeout=180)
        profile = dict(profile,_source_app=str(app))
        plan = language_plan(backup,data,profile,args.cache)
        extras = [x for x in plan if x not in SETTINGS]
        extra_records = snapshot_settings(backup,data,extra=extras,include_base=False)
        manifest['settings'].update(extra_records)
        manifest['state'] = 'ready'
        write_manifest(backup,manifest)
        os.replace(app,original)
        try:
            os.replace(stage,app)
        except BaseException:
            os.replace(original,app)
            raise
        swapped = True
        for rel,content in plan.items():
            atomic_write(safe_path(data,rel),content,mode=0o600)
            manifest['settings'][rel]['after_sha256'] = sha256(content)
        manifest['state'] = 'installed'
        write_manifest(backup,manifest)
    except BaseException:
        if swapped:
            retained = app.parent/('.Cursor-Chinese-Patch-Failed-'+stamp+'.app')
            os.replace(app,retained)
            os.replace(original,app)
            manifest['retained_failed_app'] = str(retained)
        if manifest['settings']:
            restore_settings(backup,data,manifest['settings'],force=True)
        manifest['state'] = 'rolled-back'
        write_manifest(backup,manifest)
        raise
    print('安装完成。原厂完整应用保留于：'+str(original))
    print('独立恢复入口：'+str(backup/'恢复官方原版.command'))
    print('不会代点系统权限；升级前先恢复官方原版，不绕过原生更新签名检查。')
    if not args.no_launch:
        subprocess.run(['open','-a',str(app)],check=True)


def restore(args):
    backup = args.backup.absolute()
    manifest = read_object(backup/'manifest.json')
    if manifest.get('schema')!=1 or manifest.get('state')!='installed':
        raise PatchError('此恢复入口不处于已安装状态，未覆盖任何应用')
    app,original = pathlib.Path(manifest['app']),pathlib.Path(manifest['original_app'])
    if original.parent!=app.parent or not original.name.startswith('.Cursor-Chinese-Patch-Original-'):
        raise PatchError('原版恢复路径校验失败')
    profile,files = manifest['profile'],manifest['files']
    if classify_app(original,profile,files)!='original' or classify_app(app,profile,files)!='patched':
        raise PatchError('原版或当前应用已变化，请人工核对；未自动覆盖')
    verify_signature(original)
    if not args.yes and input('保留当前补丁应用并恢复原版。输入 RESTORE 继续：')!='RESTORE':
        raise PatchError('已取消')
    quit_app(app,args.quit_cursor)
    retained = app.parent/('.Cursor-Chinese-Patch-Retained-'+uuid.uuid4().hex[:10]+'.app')
    os.replace(app,retained)
    try:
        os.replace(original,app)
    except BaseException:
        os.replace(retained,app)
        raise
    skipped = restore_settings(backup,pathlib.Path(manifest['user_data']),manifest['settings'])
    manifest.update(state='restored',retained_patched_app=str(retained),settings_preserved_after_user_changes=skipped)
    write_manifest(backup,manifest)
    print('已恢复原厂应用；账号、聊天、项目、扩展脚本不回滚。')
    if skipped:
        print('以下配置安装后发生用户修改，已保留：'+', '.join(skipped))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Cursor-汉化中文补丁：精确版本安装、检查及独立恢复')
    parser.add_argument('action',choices=['status','install','prepare','restore'])
    parser.add_argument('--app',type=pathlib.Path,default=pathlib.Path('/Applications/Cursor.app'))
    parser.add_argument('--user-data',type=pathlib.Path,default=DEFAULT_DATA)
    parser.add_argument('--backup-root',type=pathlib.Path,default=DEFAULT_BACKUPS)
    parser.add_argument('--cache',type=pathlib.Path,default=DEFAULT_CACHE)
    parser.add_argument('--extensions-dir',type=pathlib.Path,default=DEFAULT_EXTENSIONS)
    parser.add_argument('--output',type=pathlib.Path)
    parser.add_argument('--backup',type=pathlib.Path)
    parser.add_argument('--yes',action='store_true')
    parser.add_argument('--quit-cursor',action='store_true')
    parser.add_argument('--no-launch',action='store_true')
    args = parser.parse_args(argv)
    if sys.version_info<(3,9):
        raise PatchError('需要 Python 3.9 或以上版本')
    if platform.system()!='Darwin' or platform.machine()!='arm64':
        raise PatchError('首版只支持 macOS Apple Silicon；不会在未验证平台强行安装')
    if args.action=='restore':
        if not args.backup:
            parser.error('restore 需要 --backup')
        restore(args)
        return
    profile = load_profile()
    files = read_patch(profile['app_patch'])['files']
    args.app = args.app.expanduser().absolute()
    state = classify_app(args.app,profile,files)
    if args.action=='status':
        print(json.dumps({'cursor_version':profile['cursor_version'],'state':state,
                          'resources_match':True,'no_changes_made':True},ensure_ascii=False,indent=2))
    elif args.action=='prepare':
        if not args.output:
            parser.error('prepare 需要 --output，目标必须不存在')
        args.cache.mkdir(parents=True,exist_ok=True)
        prepare_app(args.app,args.output.absolute(),profile,args.cache,files)
        print('隔离副本校验和签名通过；未替换原应用、未写用户配置：'+str(args.output))
    else:
        args.cache.mkdir(parents=True,exist_ok=True)
        install(args,profile,files)


if __name__=='__main__':
    try:
        main()
    except (PatchError,subprocess.CalledProcessError,OSError,ValueError) as exc:
        print('已停止：'+str(exc),file=sys.stderr)
        sys.exit(1)
