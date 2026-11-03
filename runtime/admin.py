"""Server-local setup. Passwords/API secrets are read with getpass, never CLI arguments."""
import argparse
import getpass
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config, hash_password
from .engine import Authority


def configure(args):
    origin = args.origin.rstrip('/')
    u = urlsplit(origin)
    if u.scheme != 'https' or not u.hostname or u.path or u.username or u.password or u.query or u.fragment:
        raise SystemExit('Use an exact HTTPS origin, for example https://trade.example.com')
    password = getpass.getpass('控制台管理员密码（至少 12 位）: ')
    if password != getpass.getpass('再次输入密码: '): raise SystemExit('Passwords do not match')
    values = dict(PANEL_PUBLIC_ORIGIN=origin,PANEL_PRIVATE_DIR='/var/lib/crypto-panel/private',PANEL_DATA_DIR='/var/lib/crypto-panel/data',
                  PANEL_EXECUTION_ENABLED='0',PANEL_CYCLE_SECONDS='300',PANEL_PASSWORD_HASH=hash_password(password))
    for key in ('OKX_API_KEY','OKX_API_SECRET','OKX_API_PASSPHRASE','PANEL_GITHUB_READ_TOKEN'):
        values[key] = getpass.getpass(key+'（可留空，稍后在服务器配置）: ')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    fd = os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:
        for key,value in values.items(): f.write(key+'='+json.dumps(value)+'\n')
    print('配置文件已创建；交易执行保持关闭。请勿上传配置文件或发送到聊天。')


def import_data(args):
    source, target = args.source.resolve(), args.data.resolve()
    if target.exists() and any(target.iterdir()): raise SystemExit('Target data directory must be empty')
    states = [source/'combined/state.json', source/'okx_demo/state.json']
    for p in states:
        if p.is_symlink() or not p.is_file(): raise SystemExit('Both original account states are required')
        value = json.loads(p.read_text())
        if not isinstance(value,dict): raise SystemExit('Invalid account state')
    if 'mirrored' not in json.loads(states[1].read_text()): raise SystemExit('Invalid OKX mirror state')
    if 'positions' not in json.loads(states[0].read_text()): raise SystemExit('Invalid signal account state')
    target.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (source/'summary.json').is_file() and not (source/'summary.json').is_symlink():
        shutil.copy2(source/'summary.json',target/'summary.json')
    allowed = {'state.json','summary.json','actions.jsonl','trades.csv','equity.csv','events.jsonl'}
    for folder in ('combined','okx_demo'):
        (target/folder).mkdir(exist_ok=True)
        for p in (source/folder).iterdir():
            if p.is_file() and not p.is_symlink() and (p.name in allowed or p.name.startswith('smoke-test-') and p.suffix=='.json'):
                shutil.copy2(p,target/folder/p.name)
    state = json.loads((target/'okx_demo/state.json').read_text()); state['paused']=True
    (target/'okx_demo/state.json').write_text(json.dumps(state,ensure_ascii=False,indent=1))
    summary = target/'okx_demo/summary.json'
    if summary.exists():
        value=json.loads(summary.read_text());value['paused']=True
        summary.write_text(json.dumps(value,ensure_ascii=False,indent=1))
    print('原账户状态已导入，保持暂停；未生成新账户，未下单。')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    setup=commands.add_parser('configure');setup.add_argument('--origin',required=True);setup.add_argument('--output',type=Path,required=True)
    setup.set_defaults(run=configure)
    data=commands.add_parser('import-data');data.add_argument('--source',type=Path,required=True);data.add_argument('--data',type=Path,required=True)
    data.set_defaults(run=import_data)
    check=commands.add_parser('check-authority')
    check.set_defaults(run=lambda args: (Authority('modi-cjh/crypto-panel').ensure(),print('旧执行工作流已停用且无在途任务。')))
    args=parser.parse_args();args.run(args)


if __name__=='__main__': main()
