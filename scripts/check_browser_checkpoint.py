"""Fresh regression checks and export browser evidence; never re-execute saved jobs."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.request import urlopen
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--browser-state',type=Path)
    parser.add_argument('--checks',action='store_true')
    args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False)
    summary={'checkpoint':'browser_product_v1','checks':[], 'evidence':[],
        'external_saved_actions_repeated':False,'public_deployment':False,'live_github':False,'live_cloud_models':False}
    if args.checks:
        env=dict(os.environ,TMPDIR=str(ROOT/'.test_tmp'))
        commands=[['-m','compileall','-q','cortex','scripts'],['-m','unittest','discover','-s','tests','-q'],
            ['scripts/verify_selective_baseline.py'],['scripts/verify_product_foundation_baseline.py']]
        for index,command in enumerate(commands):
            result=subprocess.run([sys.executable,*command],cwd=ROOT,env=env,capture_output=True,text=True)
            (args.output_dir/f'check_{index}.txt').write_text(result.stdout+result.stderr)
            summary['checks'].append({'command':[sys.executable,*command],'returncode':result.returncode,'log':f'check_{index}.txt'})
    if args.browser_state:
        for index,path in enumerate(sorted((args.browser_state/'sessions').glob('*/jobs.sqlite'))):
            with sqlite3.connect(path) as db:
                rows=db.execute('SELECT payload FROM jobs ORDER BY rowid').fetchall()
                for row in rows:
                    value=json.loads(row[0]);identifier=value['job_id']
                    (args.output_dir/(identifier+'.json')).write_text(json.dumps(value,indent=2)+'\n')
                    usage=value['model_usage'];known=all(x.get('prompt_tokens') is not None and x.get('output_tokens') is not None for x in usage)
                    summary['evidence'].append({'job_id':identifier,'request':value['request'],'state':value['state'],
                        'status':(value.get('result') or {}).get('status'),'budget':value['budget'],
                        'prompt_tokens':sum(x['prompt_tokens'] for x in usage) if known else None,
                        'output_tokens':sum(x['output_tokens'] for x in usage) if known else None,
                        'money_usd':None,'inference':'actual Ollama attempts; no mocked browser responses' if usage else 'none; deterministic engine/tools',
                        'latencies_ms':[x.get('latency_ms') for x in usage]})
                with contextlib.closing(sqlite3.connect(args.output_dir/f'session_jobs_{index}.sqlite')) as destination:
                    db.backup(destination)
                if db.execute("SELECT name FROM sqlite_master WHERE name='reminders'").fetchone():
                    reminder_rows=[{**json.loads(payload),'state':state} for payload,state in db.execute('SELECT payload,state FROM reminders')]
                    (args.output_dir/f'reminders_{index}.json').write_text(json.dumps(reminder_rows,indent=2)+'\n')
                    summary.setdefault('reminders',[]).extend(reminder_rows)
            receipts=path.parent/'notifications.jsonl'
            if receipts.exists():(args.output_dir/f'notifications_{index}.jsonl').write_bytes(receipts.read_bytes())
            for p in (path.parent/'files').glob('*'):
                if p.is_file():(args.output_dir/f'generated_{index}_{p.name}').write_bytes(p.read_bytes())
        try:
            with urlopen('http://localhost:11434/api/tags',timeout=3) as response:summary['ollama_models']=json.load(response)
            with urlopen('http://localhost:11434/api/version',timeout=3) as response:summary['ollama_version']=json.load(response)
        except OSError:summary['ollama_metadata']='unavailable'
    summary['source_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'cortex').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    with zipfile.ZipFile(args.output_dir/'source.zip','x',zipfile.ZIP_DEFLATED) as archive:
        for name in summary['source_sha256']:archive.write(ROOT/name,name)
    (args.output_dir/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({'checks':summary['checks'],'jobs':summary['evidence']},indent=2))
    return int(any(x['returncode'] for x in summary['checks']))


if __name__=='__main__':raise SystemExit(main())
