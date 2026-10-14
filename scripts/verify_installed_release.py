"""Install/start wheel outside checkout. Fresh local public-boundary checks, no deployment."""
from datetime import datetime,timedelta,timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    root=args.output.resolve(); venv=root/'venv';work=root/'empty_workdir';work.mkdir()
    subprocess.run([sys.executable,'-m','venv','--without-pip',str(venv)],check=True,capture_output=True)
    python=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    install=subprocess.run([sys.executable,'-m','pip','--python',str(python),'install','--no-index','--no-deps',str(args.wheel.resolve())],capture_output=True,text=True)
    (root/'installation.txt').write_text(install.stdout+install.stderr,encoding='utf-8')
    if install.returncode:raise RuntimeError('Installation failed')
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    origin='https://cortex.tryout.invalid';host='cortex.tryout.invalid';base=f'http://127.0.0.1:{port}'
    state=root/'persistent_state'
    command=[str(python),'-m','cortex','web','--public','--host','127.0.0.1','--port',str(port),'--origin',origin,'--state-dir',str(state)]
    evidence={'deployed':False,'check':'installed wheel; local HTTP simulates private reverse-proxy boundary',
        'wheel_sha256':hashlib.sha256(args.wheel.read_bytes()).hexdigest(),'results':[]}
    process=None;log=None
    def start(name):
        nonlocal log
        log=(root/name).open('w',encoding='utf-8')
        proc=subprocess.Popen(command,cwd=work,stdout=log,stderr=subprocess.STDOUT)
        for _ in range(150):
            try:
                response=urlopen(Request(base+'/healthz',headers={'Host':host}),timeout=1)
                response.close();return proc
            except (URLError,OSError):
                if proc.poll() is not None:raise RuntimeError('Server exited; see startup log')
                time.sleep(.05)
        proc.terminate();raise RuntimeError('Server start timeout')
    def stop(proc):
        proc.terminate();proc.wait(timeout=10);log.close()
    def request(path,body=None,session=None):
        headers={'Host':host,'Origin':origin,'Content-Type':'application/json'}
        if session:headers.update(Cookie=session['cookie'],**{'X-Cortex-CSRF':session['csrf']})
        with urlopen(Request(base+path,data=None if body is None else json.dumps(body).encode(),headers=headers),timeout=5) as response:
            raw=response.read()
            return json.loads(raw),response.headers
    def session():
        value,headers=request('/api/session',{})
        cookie=headers['Set-Cookie'];assert 'Secure' in cookie and 'HttpOnly' in cookie
        return {'cookie':cookie.split(';')[0],'csrf':value['csrf']}
    def wait(op,who):
        for _ in range(200):
            value,_=request('/api/operations/'+op['operation_id'],session=who)
            if value['state']!='running':
                if value['error']:raise RuntimeError(value['error'])
                return value['job_id']
            time.sleep(.02)
        raise RuntimeError('Operation timeout')
    def reject(path,body=None,who=None):
        try:request(path,body,who)
        except HTTPError as error:
            assert error.code==400;return
        raise AssertionError('Request unexpectedly accepted')
    try:
        process=start('startup.txt')
        reject('/api/jobs')
        first=session();second=session()
        reject('/api/session',{'owner':'not-an-owner-token'})
        meta,_=request('/api/meta',session=first)
        assert meta['public'] and not meta['repositories']
        assert not meta['capabilities']['document']['available']
        assert all(m['provider']=='ollama' for m in meta['models'])
        evidence['results'].append({'public_capabilities':meta['capabilities']})
        reject('/api/prepare',{'request':'Git write','mode':'git'},first)
        reject('/api/prepare',{'request':'Draft','mode':'document','filename':'test.md'},first)
        reject('/api/prepare',{'request':'Read private files','output_dir':'C:/'},first)
        task,_=request('/api/examples/supplier',session=first)
        proposal,_=request('/api/prepare',{'mode':'supplier','request':'Select supplier','task':task,'max_calls':0},first)
        job=wait(proposal,first)
        preview,_=request('/api/jobs/'+job,session=first);assert preview['budget']['units']==0
        reject('/api/jobs/'+job,who=second)
        op,_=request('/api/jobs/'+job+'/resume',{},first);wait(op,first)
        result,_=request('/api/jobs/'+job,session=first)
        output=result['result']['result']['steps'][0]['output']
        assert output['supplier']=='Beta' and output['total']=='11500'
        assert result['budget']['units']==4 and result['budget']['model_calls']==0
        evidence['results'].append({'supplier':output,'budget':result['budget'],'cross_session_read':'rejected'})
        at=(datetime.now(timezone.utc)+timedelta(seconds=1.4)).isoformat()
        proposal,_=request('/api/prepare',{'mode':'reminder','request':'Fresh installed release restart check',
            'message':'Installed Cortex scheduler restart evidence','at':at,'timezone':'UTC','max_calls':0},first)
        reminder_job=wait(proposal,first);op,_=request('/api/jobs/'+reminder_job+'/resume',{},first);wait(op,first)
        stop(process);process=None
        time.sleep(1.5)
        process=start('restart.txt')
        existing,_=request('/api/jobs/'+job,session=first);assert existing['budget']==result['budget']
        delivered=subprocess.run([str(python),'-m','cortex','web-reminders','--state-dir',str(state),'--seconds','1'],cwd=work,capture_output=True,text=True)
        (root/'scheduler.txt').write_text(delivered.stdout+delivered.stderr,encoding='utf-8')
        assert delivered.returncode==0 and 'CORTEX REMINDER: Installed Cortex' in delivered.stdout
        rows,_=request('/api/reminders',session=first)
        assert rows[0]['state']=='delivered' and rows[0]['delivery_attempts']==1
        evidence['results'].append({'reminder':rows[0],'normal_service_restart':'preserved budget, session and pending reminder'})
        reject('/api/reminders/'+rows[0]['reminder_id']+'/cancel',{},second)
        evidence['passed']=True
    except Exception as exc:
        evidence.update(passed=False,error=type(exc).__name__+': '+str(exc));raise
    finally:
        if process:stop(process)
        (root/'evidence.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'passed':True,'deployment':False,'checks':len(evidence['results'])}))


if __name__=='__main__':main()
