"""Build a wheel with fixed timestamps; package only an explicit source allowlist."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
EPOCH='1791849600'


def build(output):
    output.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ,SOURCE_DATE_EPOCH=EPOCH,PYTHONHASHSEED='0')
    result=subprocess.run([sys.executable,'-m','pip','wheel','--no-deps','--no-build-isolation',
        '--wheel-dir',str(output.resolve()),'.'],cwd=ROOT,env=env,capture_output=True,text=True)
    (output/'build.txt').write_text(result.stdout+result.stderr,encoding='utf-8')
    if result.returncode:raise RuntimeError('Wheel build failed; see build.txt')
    paths=list(output.glob('*.whl'))
    if len(paths)!=1:raise RuntimeError('Expected exactly one wheel')
    wheel=paths[0]
    with zipfile.ZipFile(wheel) as archive:
        names=archive.namelist()
        for name in ('cortex/web_assets/index.html','cortex/web_assets/app.js','cortex/default_config/cortex.public.offline.json'):
            if name not in names:raise RuntimeError('Missing package data: '+name)
        if any(part in {'.cortex','.env','runs','snapshots'} for name in names for part in name.split('/')):
            raise RuntimeError('Private artifact in wheel')
    (output/'SHA256SUMS').write_text(hashlib.sha256(wheel.read_bytes()).hexdigest()+'  '+wheel.name+'\n')
    return wheel


def source_package(output):
    release=output/'cortex-0.2.0-source.zip'
    roots=['cortex','config','deployment','examples','scripts','tests','docs']
    root_files=['pyproject.toml','requirements-build.txt','README.md','START_HERE.md','LICENSE_NOTICE.md']
    paths=[ROOT/name for name in root_files if (ROOT/name).is_file()]
    for name in roots:
        paths.extend(p for p in (ROOT/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
    with zipfile.ZipFile(release,'x',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            entry=zipfile.ZipInfo(str(path.relative_to(ROOT)).replace('\\','/'),date_time=(2026,10,13,0,0,0))
            entry.compress_type=zipfile.ZIP_DEFLATED
            entry.external_attr=0o644<<16
            archive.writestr(entry,path.read_bytes())
    return release


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source',action='store_true')
    args=parser.parse_args()
    wheel=build(args.output)
    source=source_package(args.output) if args.source else None
    print(json.dumps({'wheel':str(wheel),'source':str(source) if source else None}))


if __name__=='__main__':main()
