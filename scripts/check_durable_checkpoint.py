"""Save actual SQLite lifecycle evidence with mocked HTTP, then regression checks."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cortex.draft_checks import DraftPolicy
from cortex.job_store import JobStore
from cortex.jobs import new_job,resume_job


def reply(text):
    return io.BytesIO(json.dumps({'model':'mock-model','done':True,'done_reason':'stop','message':{'content':text},
                                 'prompt_eval_count':10,'eval_count':5}).encode())


def write(path,value):
    with path.open('x',encoding='utf-8') as output:
        json.dump(value,output,indent=2)
        output.write('\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False)
    config=json.loads((ROOT/'config/cortex.example.json').read_text())
    load=lambda name:json.loads((ROOT/'examples/durable_v1'/name).read_text())
    scenarios=[]
    for name, texts, expected, words, call_limit, dependent in (
        ('word_repair',['one two three four','one two'],'drafted',3,3,False),
        ('currency_repair',['Beta $11500','Beta 11500 unspecified'],'drafted',30,3,False),
        ('budget_prevents_repair',['$1'],'budget_exhausted',30,1,False),
        ('failed_repair_blocks_dependency',['$1','$2'],'plan_failed',30,3,True),
        ('repeated_no_progress',['$1','$1'],'plan_failed',30,3,False)):
        db=args.output_dir/(name+'.sqlite3')
        store=JobStore(db)
        try:
            with patch('urllib.request.urlopen',side_effect=[reply(text) for text in texts]) as http:
                result=new_job(store,'Draft the requested text',config,
                    plan=load('dependent_drafts.json' if dependent else 'single_draft.json'),
                    policy=DraftPolicy.validate({'max_words':words},max_repairs=2 if name=='repeated_no_progress' else 1),
                    constraints=['Be persuasive'],max_model_calls=call_limit)
            assert result['status']==expected,(name,result['status'])
            assert http.call_count==len(texts)
            evidence={'evidence_kind':'mocked_http_actual_sqlite','actual_inference_calls':0,
                      'fixture_telemetry':'10 input/5 output tokens per mocked successful response',
                      'result':result,'stored_job':store.read(result['job_id']),'transitions':store.transitions(result['job_id'])}
            write(args.output_dir/(name+'.json'),evidence)
            scenarios.append({'case':name,'status':result['status'],'budget':result['budget'],'mocked_attempts':http.call_count})
        finally: store.close()
    store=JobStore(args.output_dir/'resume.sqlite3')
    try:
        with patch('urllib.request.urlopen',return_value=reply('Accepted introduction')):
            before=new_job(store,'Draft and summarize',config,plan=load('dependent_drafts.json'),pause_after=1,max_model_calls=2)
        write(args.output_dir/'pause.json',{'evidence_kind':'mocked_http_actual_sqlite','result':before,'stored_job':store.read(before['job_id'])})
        with patch('urllib.request.urlopen',return_value=reply('Accepted summary')) as http:
            after=resume_job(store,before['job_id'])
        assert http.call_count==1 and after['budget']['model_calls_used']==2 and after['status']=='drafted'
        write(args.output_dir/'resume.json',{'evidence_kind':'mocked_http_actual_sqlite','result':after,
             'stored_job':store.read(before['job_id']),'transitions':store.transitions(before['job_id']),
             'accepted_predecessor_regenerated':False,'new_mocked_attempts_on_resume':http.call_count})
        scenarios.append({'case':'pause_resume','status':after['status'],'budget':after['budget'],'accepted_predecessor_regenerated':False})
    finally: store.close()
    store=JobStore(args.output_dir/'unknown.sqlite3')
    try:
        with patch('urllib.request.urlopen',side_effect=KeyboardInterrupt):
            before=new_job(store,'Draft',config,plan=load('single_draft.json'),max_model_calls=2)
        write(args.output_dir/'unknown_attempt.json',{'evidence_kind':'mocked_interrupt_actual_sqlite','result':before,'stored_job':store.read(before['job_id'])})
        with patch('urllib.request.urlopen') as http:
            blocked=resume_job(store,before['job_id'])
        http.assert_not_called()
        with patch('urllib.request.urlopen',return_value=reply('Explicit replacement')):
            after=resume_job(store,before['job_id'],retry_interrupted=True)
        assert after['budget']['model_calls_used']==2
        write(args.output_dir/'unknown_replacement.json',{'evidence_kind':'mocked_http_actual_sqlite','result':after,
            'stored_job':store.read(before['job_id']),'original_charge_retained':True,'explicit_authorization':True,'exactly_once':False})
        scenarios.append({'case':'unknown_replacement','status':after['status'],'budget':after['budget']})
    finally: store.close()
    checks=[]
    temp=ROOT/'.test_tmp'
    for command in (['-m','compileall','-q','cortex','tests','scripts'],['-m','unittest','discover','-s','tests','-q'],
                    ['scripts/verify_selective_baseline.py'],['scripts/verify_product_foundation_baseline.py'],['scripts/verify_extraction_evidence.py']):
        start=time.perf_counter()
        result=subprocess.run([sys.executable,*command],cwd=ROOT,env=dict(os.environ,TMPDIR=str(temp)),capture_output=True,text=True,timeout=60)
        checks.append({'command':command,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'seconds':round(time.perf_counter()-start,3)})
    with zipfile.ZipFile(ROOT/'snapshots/pre_durable_refinement_v1.zip') as old:
        names=[n for n in old.namelist() if n.startswith('runs/') and not n.endswith('/')]
        assert all((ROOT/n).read_bytes()==old.read(n) for n in names)
        assert (ROOT/'EXPERIMENT_LOG.md').read_bytes().startswith(old.read('EXPERIMENT_LOG.md'))
    summary={'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'scenarios':scenarios,'checks':checks,
             'all_checks_passed':all(c['exit_code']==0 for c in checks),'previous_run_files_unchanged':len(names),
             'previous_log_prefix_unchanged':True,'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'cortex').glob('*.py'))}}
    write(args.output_dir/'checks.json',summary)
    print(json.dumps({'all_checks_passed':summary['all_checks_passed'],'scenarios':len(scenarios),'previous_runs_unchanged':len(names),'output':str(args.output_dir)},indent=2))
    return 0 if summary['all_checks_passed'] else 2


if __name__=='__main__': raise SystemExit(main())
