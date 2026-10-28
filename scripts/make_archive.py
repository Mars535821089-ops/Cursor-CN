"""Archive only committed files and preserve executable launcher permissions."""
import argparse
import hashlib
import json
import pathlib
import subprocess
import zipfile

from runtime_build import ROOT,load_profile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=pathlib.Path,default=ROOT/'dist')
    args=parser.parse_args()
    profile=load_profile()
    dirty=subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()
    if dirty:raise SystemExit('Commit and verify the source before creating a public archive.')
    names=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    names=[x for x in names if x]
    if not names:raise SystemExit('No committed source files.')
    name='Cursor-CN-v'+profile['patch_version']+'-macOS-arm64'
    args.output.mkdir(parents=True,exist_ok=True)
    destination=args.output/(name+'.zip')
    with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for relative in sorted(names):
            path=ROOT/relative
            if path.is_symlink():raise SystemExit('Release source must not contain symlinks.')
            info=zipfile.ZipInfo(name+'/'+relative,date_time=(2026,10,26,0,0,0))
            info.create_system=3
            mode=0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr=(0o100000|mode)<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,path.read_bytes())
    digest=hashlib.sha256(destination.read_bytes()).hexdigest()
    (args.output/'SHA256SUMS').write_text(digest+'  '+destination.name+'\n')
    print(json.dumps({'file':destination.name,'bytes':destination.stat().st_size,'sha256':digest,'committed_files_only':True},indent=2))


if __name__=='__main__':main()
