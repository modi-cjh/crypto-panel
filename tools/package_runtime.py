#!/usr/bin/env python3
"""Build a source/deployment ZIP from tracked files, excluding credentials and local state."""
import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def build(output):
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    prefixes=('assets/','tools/','runtime/','deploy/','tests/','paper/','.github/workflows/')
    names={'README.md','CHANGELOG.md','index.html','config.js','runtime-config.json','package.json','.gitignore','.nojekyll'}
    manifest={}
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(n for n in tracked if n and (n in names or n.startswith(prefixes))):
            path=ROOT/name
            if path.is_symlink() or not path.is_file() or any(p.startswith('.env') for p in path.parts) or name.endswith(('.pyc','.sqlite3')):
                continue
            data=path.read_bytes();archive.writestr('crypto-panel/'+name,data)
            manifest[name]=hashlib.sha256(data).hexdigest()
        archive.writestr('crypto-panel/deploy/MANIFEST.json',json.dumps(manifest,indent=2))
    print(json.dumps(dict(package=str(output.resolve()),files=len(manifest),bytes=output.stat().st_size)))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    build(p.parse_args().output)
