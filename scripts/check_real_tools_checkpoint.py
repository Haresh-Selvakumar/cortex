"""Fresh evidence: mocked draft HTTP, actual files/reminder delivery/disposable Git.

No GitHub/network write. Run outside a restricted Windows MSYS sandbox.
"""
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cortex.draft_checks import DraftPolicy
from cortex.job_store import JobStore
from cortex.jobs import new_job,resume_job
from cortex.reminders import inspect,run_scheduler
from cortex.git_tools import git


def save(path,value):
    with path.open('x',encoding='utf-8') as output:json.dump(value,output,indent=2); output.write('\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    destination=args.output_dir.resolve(); destination.mkdir(parents=True,exist_ok=False)
    disposable=Path(tempfile.mkdtemp(prefix='cortex_tools_demo_',dir=ROOT/'.test_tmp'))
    repo=disposable/'repo'; remote=disposable/'remote.git'
    for arguments in (['init','--initial-branch','submission-demo',str(repo)],['init','--bare',str(remote)]):
        result=subprocess.run(['git',*arguments],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
    for key,value in [('user.name','Cortex Disposable Demo'),('user.email','demo@example.invalid')]:git(repo,'config',key,value)
    (repo/'unrelated-notes.txt').write_text('This unrelated file must never be staged.\n')
    config={'version':1,'output_dir':str(repo),'notification_log':str(destination/'notifications.jsonl'),
        'max_operations':8,'event_requirements':[], 'git_targets':{'disposable':{'path':str(repo),
            'branch':'submission-demo','remote_url':str(remote),'allow_push':True}}}
    plan=json.loads((ROOT/'examples/real_tools_v1/mixed.json').read_text())
    plan['steps'][-1]['tool']['arguments']['at']=(datetime.now(timezone.utc)+timedelta(seconds=12)).isoformat()
    for identifier,name in [('inspect_repository','git_inspect'),('publish_to_disposable','git_commit_push')]:
        arguments={'repository':'disposable'}
        if name=='git_commit_push':arguments.update(branch='submission-demo',files=['cortex-description.md'],message='Add checked project description')
        plan['steps'].append({'id':identifier,'task_type':'analysis','goal':'Use only the explicitly configured disposable repository.',
            'execution_kind':'tool','required_inputs':['user_goal','predecessor_outputs'],'missing_essential_information':[],
            'depends_on':['save'],'required_capabilities':['analysis','github'],'expected_output':'tool_result',
            'completion_checks':['tool_verified'],'tool':{'name':name,'arguments':arguments}})
    save(destination/'tool_config.json',config); save(destination/'mixed_plan.json',plan)
    models=json.loads((ROOT/'config/cortex.example.json').read_text())
    content='Cortex is a Python prototype that routes model and tool work, records budgets, and verifies supplier arithmetic. Prose passes declared checks; event compliance remains unchecked.'
    response=io.BytesIO(json.dumps({'model':'mock-worker','done':True,'message':{'content':content},'prompt_eval_count':10,'eval_count':5}).encode())
    store=JobStore(destination/'mixed.sqlite3')
    try:
        with patch('urllib.request.urlopen',return_value=response) as model_http:
            execution=new_job(store,'Draft and save a project description, schedule review, inspect and push only to a disposable local remote.',
                models,plan=plan,source=(ROOT/'examples/real_tools_v1/source.txt').read_text(),constraints=['Use a friendly tone'],
                policy=DraftPolicy.validate({'max_words':80},max_repairs=0),options={'tool_config':config,'request_contract':'isolated'},max_model_calls=1)
        assert execution['status']=='drafted',execution
        assert model_http.call_count==1
        identifier=inspect(store)[0]['reminder_id']
        scheduler=run_scheduler(store,config['notification_log'],seconds=15)
        reminder=inspect(store,identifier)[0]
        assert reminder['state']=='delivered',reminder
        with patch('urllib.request.urlopen') as model_http:
            resumed=resume_job(store,execution['job_id'])
        model_http.assert_not_called()
        assert resumed['budget']==execution['budget']
        assert git(repo,'ls-tree','--name-only','HEAD').stdout.strip()=='cortex-description.md'
        (destination/'accepted-description.md').write_bytes((repo/'cortex-description.md').read_bytes())
        save(destination/'mixed_execution.json',{'evidence_kind':'actual_local_tools_with_mocked_draft_http',
            'actual_model_inference_calls':0,'mocked_model_attempts':1,'fixture_telemetry':'10/5 tokens, artificial',
            'execution':execution,'stored_job':store.read(execution['job_id']),'transitions':store.transitions(execution['job_id']),
            'reminder':reminder,'scheduler':scheduler,'terminal_resume_reused':True,
            'unrelated_file_unstaged':True,'disposable_repository':str(repo),'disposable_remote':str(remote)})
    finally:store.close()
    checks=[]
    for command in (['-m','compileall','-q','cortex','tests','scripts'],['-m','unittest','discover','-s','tests','-q'],['scripts/verify_selective_baseline.py'],
                    ['scripts/verify_product_foundation_baseline.py'],['scripts/verify_extraction_evidence.py']):
        start=time.perf_counter()
        result=subprocess.run([sys.executable,*command],cwd=ROOT,env=dict(os.environ,TMPDIR=str(ROOT/'.test_tmp')),
            capture_output=True,text=True,timeout=60)
        checks.append({'command':command,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,
                       'seconds':round(time.perf_counter()-start,3)})
    with zipfile.ZipFile(ROOT/'snapshots/pre_real_tools_v1.zip') as old:
        previous=[name for name in old.namelist() if name.startswith('runs/') and not name.endswith('/')]
        assert all((ROOT/name).read_bytes()==old.read(name) for name in previous)
        assert (ROOT/'EXPERIMENT_LOG.md').read_bytes().startswith(old.read('EXPERIMENT_LOG.md'))
        tests=[name for name in old.namelist() if name.startswith('tests/') and name.endswith('.py')]
        assert all((ROOT/name).read_bytes()==old.read(name) for name in tests)
    summary={'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'checks':checks,
        'all_checks_passed':all(row['exit_code']==0 for row in checks),'previous_runs_unchanged':len(previous),
        'original_test_files_unchanged':True,'previous_log_prefix_unchanged':True,
        'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'cortex').glob('*.py'))},
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'git_version':subprocess.run(['git','--version'],capture_output=True,text=True).stdout.strip()}
    save(destination/'checks.json',summary)
    print(json.dumps({'all_checks_passed':summary['all_checks_passed'],'previous_runs_unchanged':len(previous),
        'reminder_delivered':True,'actual_model_calls':0,'tool_operations':execution['tool_accounting']['operations_used'],
        'synthetic_work_units':execution['budget']['work_units_used'],'output':str(destination)},indent=2))
    return 0 if summary['all_checks_passed'] else 2


if __name__=='__main__':raise SystemExit(main())
