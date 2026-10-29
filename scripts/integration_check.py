"""Explicit-input macOS integration test; never patches or launches the input app."""
import argparse
import json
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile

from patch_engine import sha256
from runtime_build import ROOT,load_app_patch,load_profile,read_patch


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--original-app',type=pathlib.Path,required=True)
    parser.add_argument('--report',type=pathlib.Path)
    args=parser.parse_args()
    if platform.system()!='Darwin' or platform.machine()!='arm64':
        raise SystemExit('Integration test requires macOS Apple Silicon.')
    profile=load_profile();files=load_app_patch(profile)
    source_root=args.original_app/'Contents/Resources/app'
    before={rel:sha256((source_root/rel).read_bytes()) for rel in files}
    assert all(before[rel]==record['before_sha256'] for rel,record in files.items()),'Input must be the exact supported original'
    temp=pathlib.Path(tempfile.mkdtemp(prefix='cursor-patch-integration-'))
    app=temp/'Test Cursor.app';data=temp/'User Data';backups=temp/'Backups';cache=temp/'Cache';extensions=temp/'Extensions'
    data.mkdir();(data/'User/globalStorage').mkdir(parents=True)
    argv=b'{\n// Original commented Cursor configuration\n"locale":"en","keepExistingOption":true,\n}'
    (data/'argv.json').write_bytes(argv)
    database=data/'User/globalStorage/state.vscdb';database.write_bytes(b'protected-test-fixture-not-account-data')
    subprocess.run(['cp','-cR',str(args.original_app),str(app)],check=True)
    command=[sys.executable,str(ROOT/'scripts/install.py')]
    options=['--app',str(app),'--user-data',str(data),'--backup-root',str(backups),
             '--cache',str(cache),'--extensions-dir',str(extensions),'--yes','--no-launch']
    try:
        subprocess.run(command+['install']+options,check=True)
        manifest=next(backups.glob('*/manifest.json'));record=json.loads(manifest.read_text())
        assert record['state']=='installed'
        assert json.loads((data/'argv.json').read_text())['keepExistingOption'] is True
        assert json.loads((data/'locale.json').read_text())['locale']=='zh-cn'
        assert database.read_bytes()==b'protected-test-fixture-not-account-data'
        resources=app/'Contents/_CodeSignature/CodeResources';signed_at=resources.stat().st_mtime_ns
        subprocess.run(command+['status','--app',str(app)],check=True)
        subprocess.run(command+['install']+options,check=True)
        assert resources.stat().st_mtime_ns==signed_at,'Repeat install must not re-sign'
        assert len(list(backups.glob('*/manifest.json')))==1
        # Exercise the recovery copy, not the original checkout.
        subprocess.run([sys.executable,str(manifest.parent/'install.py'),'restore',
                        '--backup',str(manifest.parent),'--yes','--no-launch'],check=True)
        subprocess.run(command+['status','--app',str(app)],check=True)
        assert (data/'argv.json').read_bytes()==argv
        assert not (data/'locale.json').exists()
        assert database.read_bytes()==b'protected-test-fixture-not-account-data'
        assert all(sha256((source_root/rel).read_bytes())==digest for rel,digest in before.items())
        result={'platform':'macOS arm64','cursor_version':profile['cursor_version'],
            'isolated_install':True,'exact_runtime_reconstruction':True,'resource_hashes_match':True,
            'signature_verified':True,'idempotent_no_resigning':True,'standalone_recovery':True,
            'language_settings_restore':True,'protected_database_untouched':True,
            'source_app_unchanged':True,'no_gui_launched_during_test':True,'pass':True}
        if args.report:
            args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print('ISOLATED_INSTALL_RESTORE_PASS',json.dumps(result),flush=True)
    except BaseException:
        print('Failed-test fixture retained for local inspection: '+str(temp),file=sys.stderr)
        raise
    else:
        shutil.rmtree(temp)


if __name__=='__main__':main()
