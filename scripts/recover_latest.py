"""Select only an installed manifest from the installer-owned backup directory."""
import argparse
import json
import pathlib
import subprocess
import sys

parser=argparse.ArgumentParser()
parser.add_argument('--backup-root',type=pathlib.Path,default=pathlib.Path.home()/'Library/Application Support/CursorChinesePatch/Backups')
args=parser.parse_args()
backups=[]
for manifest in args.backup_root.glob('*/manifest.json'):
    try:
        data=json.loads(manifest.read_text())
        if data.get('schema')==1 and data.get('state')=='installed':backups.append(manifest)
    except (OSError,ValueError):continue
if not backups:
    raise SystemExit('未找到本安装器创建的可恢复记录；不会改动当前应用。请使用原安装时的独立恢复入口。')
target=max(backups,key=lambda x:x.stat().st_mtime)
result=subprocess.run([sys.executable,str(pathlib.Path(__file__).with_name('install.py')),'restore','--backup',str(target.parent),'--quit-cursor','--yes'])
raise SystemExit(result.returncode)
