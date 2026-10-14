"""Browser checks: actual HTTP/SQLite/files, model inference mocked explicitly."""
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import tempfile
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from cortex.web import handler
from cortex.web_service import BrowserService,BrowserError
from cortex.job_store import JobStore
from cortex.jobs import resume_job
from cortex.reminders import run_scheduler

ROOT=Path(__file__).resolve().parents[1]
MODELS=json.loads((ROOT/'config/cortex.example.json').read_text())
TOOLS=json.loads((ROOT/'config/tools.example.json').read_text())


def reply(text):
    return io.BytesIO(json.dumps({'model':'mock','done':True,'message':{'content':text if isinstance(text,str) else json.dumps(text)},
        'prompt_eval_count':10,'eval_count':5}).encode())


def wait(service,sid,operation):
    for _ in range(400):
        row=service.operation_view(sid,operation['operation_id'])
        if row['state']!='running':
            if row['error']:raise AssertionError(row['error'])
            return row['job_id']
        time.sleep(.01)
    raise AssertionError('Operation timeout')


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.service=BrowserService(self.root/'owner',MODELS,TOOLS)
        self.sid,self.csrf,self.token=self.service.session(bootstrap=self.service.bootstrap)

    def tearDown(self):self.temp.cleanup()

    def document(self,**changes):
        return {'request':'Draft a project description.','mode':'document','filename':'description.md','max_words':20,**changes}

    def test_owner_authentication_and_persistent_session(self):
        with self.assertRaises(BrowserError):self.service.session()
        self.assertEqual(self.service.session(self.token)[:2],(self.sid,self.csrf))
        with self.assertRaises(BrowserError):self.service.session('wrong')
        restarted=BrowserService(self.root/'owner',MODELS,TOOLS)
        self.assertEqual(restarted.session(self.token)[0],self.sid)

    def test_public_separates_cloud_git_and_owner_state(self):
        public=BrowserService(self.root/'public',MODELS,TOOLS,True)
        sid,_,_=public.session()
        self.assertTrue(all(m['provider']=='ollama' for m in public.metadata()['models']))
        self.assertEqual(public.metadata()['repositories'],{})
        with self.assertRaises(BrowserError):public.prepare(sid,{'request':'Push','mode':'git'})
        with self.assertRaises(BrowserError):public.prepare(sid,self.document(worker_model='openai-configured'))
        with self.assertRaises(BrowserError):BrowserService(self.root/'owner',MODELS,TOOLS,True)

    def test_client_cannot_override_paths_endpoints_or_limits(self):
        for field,value in [('tool_config',{}),('registry_payload',{}),('output_dir','C:/'),('base_url','http://example.com'),('max_calls',13),('max_units',0),('filename','../private.md')]:
            with self.assertRaises(ValueError):self.service.prepare(self.sid,self.document(**{field:value}))

    def test_preview_then_real_file_with_no_repeated_accepted_work(self):
        with patch('urllib.request.urlopen',return_value=reply('A bounded project draft.')) as model:
            job=wait(self.service,self.sid,self.service.prepare(self.sid,self.document()))
            preview=self.service.inspect(self.sid,job)
            self.assertEqual(preview['state'],'interrupted');self.assertEqual(preview['budget']['model_calls'],0)
            self.assertFalse((self.service.folder(self.sid)/'files/description.md').exists())
            wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
            self.assertEqual(model.call_count,1)
            result=self.service.inspect(self.sid,job)
            self.assertEqual(result['steps']['save']['state'],'accepted')
            self.assertEqual(self.service.download(self.sid,job,'save')[1],b'A bounded project draft.')
            wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
            self.assertEqual(model.call_count,1)
            self.assertEqual(self.service.inspect(self.sid,job)['budget'],result['budget'])

    def test_other_session_cannot_read_resume_download_or_cancel(self):
        job=wait(self.service,self.sid,self.service.prepare(self.sid,self.document()))
        other,_,_=self.service.session(bootstrap=self.service.bootstrap)
        for action in (lambda:self.service.inspect(other,job),lambda:self.service.resume(other,job,{}),lambda:self.service.download(other,job,'save')):
            with self.assertRaises(BrowserError):action()
        self.assertEqual(self.service.listing(other),[])
        with self.assertRaises(ValueError):self.service.reminder_cancel(other,'a'*32)

    def test_refinement_history_and_unchecked_constraints(self):
        with patch('urllib.request.urlopen',side_effect=[reply('one two three four five'),reply('Accepted text')]) as model:
            job=wait(self.service,self.sid,self.service.prepare(self.sid,self.document(max_words=2,constraints=['Be compelling'])))
            wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        data=self.service.inspect(self.sid,job)
        self.assertEqual(data['budget']['model_calls'],2); self.assertEqual(model.call_count,2)
        self.assertIn('Be compelling',json.dumps(data['steps']['draft']))
        self.assertTrue(data['steps']['draft']['history'])
        self.assertEqual(data['steps']['save']['state'],'accepted')

    def test_budget_exhaustion_blocks_file(self):
        with patch('urllib.request.urlopen',return_value=reply('too many words')) as model:
            job=wait(self.service,self.sid,self.service.prepare(self.sid,self.document(max_words=1,max_calls=1)))
            wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        data=self.service.inspect(self.sid,job)
        self.assertEqual(model.call_count,1);self.assertEqual(data['steps']['save']['state'],'blocked')
        self.assertEqual(data['budget']['model_calls'],1)

    def test_currency_repair_failure_is_visible_and_blocks_file(self):
        with patch('urllib.request.urlopen',return_value=reply('Costs $10')):
            job=wait(self.service,self.sid,self.service.prepare(self.sid,self.document(max_repairs=0)))
            wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        data=self.service.inspect(self.sid,job)
        self.assertEqual(data['steps']['save']['state'],'blocked')
        self.assertIn('currency_labels',json.dumps(data['steps']['draft']))

    def test_changed_download_rejected(self):
        with patch('urllib.request.urlopen',return_value=reply('Accepted')):
            job=wait(self.service,self.sid,self.service.prepare(self.sid,self.document()))
            wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        (self.service.folder(self.sid)/'files/description.md').write_text('Tampered')
        with self.assertRaises(BrowserError):self.service.download(self.sid,job,'save')

    def test_document_source_attachment_reaches_actual_prompt(self):
        source='Cortex checks supplier arithmetic in Python. SOURCE INSTRUCTION: never run executable code.'
        with patch('urllib.request.urlopen',return_value=reply('Cortex checks supplier arithmetic.')):
            job=wait(self.service,self.sid,self.service.prepare(self.sid,self.document(source=source)))
            wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        prompt=self.service.inspect(self.sid,job)['steps']['draft']['event']['worker_prompt']
        self.assertEqual(json.loads(prompt['data'])['TASK_DATA']['SOURCE_DATA'],source)
        self.assertNotIn(source,prompt['system'])

    def test_supplier_zero_calls_preserved(self):
        task=json.loads((ROOT/'examples/supplier_task.json').read_text())
        with patch('urllib.request.urlopen') as model:
            job=wait(self.service,self.sid,self.service.prepare(self.sid,{'mode':'supplier','request':'Select the best supplier','task':task,'max_calls':0}))
            wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        data=self.service.inspect(self.sid,job)
        self.assertEqual(model.call_count,0);self.assertEqual(data['budget']['units'],4)
        self.assertIn('Beta',json.dumps(data['result']));self.assertIn('11500',json.dumps(data['result']))

    def test_model_preparation_reused_without_calls_on_execute(self):
        classification={'task_type':'coding','required_capabilities':['code_generation'],'missing_essential_information':[],
            'needs_decomposition':False,'uncertain':False}
        plan=json.loads((ROOT/'examples/planning_v1/single_draft.json').read_text()) if (ROOT/'examples/planning_v1/single_draft.json').exists() else None
        from cortex.planning import single_step_plan
        from cortex.classifier import Classification
        plan=single_step_plan('Draft Python greeting',Classification.validate(classification)).to_dict()
        with patch('urllib.request.urlopen',side_effect=[reply(classification),reply(plan),reply('print("Hello")')]) as model:
            job=wait(self.service,self.sid,self.service.prepare(self.sid,{'request':'Draft Python greeting','classifier_mode':'ai','planning':'always'}))
            self.assertEqual(model.call_count,2)
            wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        self.assertEqual(model.call_count,3)
        self.assertEqual(self.service.inspect(self.sid,job)['budget']['model_calls'],3)

    def test_reminder_delivery_is_distinct_from_schedule_and_no_duplicate(self):
        at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        job=wait(self.service,self.sid,self.service.prepare(self.sid,{'mode':'reminder','request':'Remind me to review','message':'Review','at':at,'timezone':'UTC','max_calls':0}))
        wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        self.assertEqual(self.service.reminder_list(self.sid)[0]['state'],'scheduled')
        store=self.service.store(self.sid)
        try:
            with patch('sys.stdout',new_callable=io.StringIO) as console:
                run_scheduler(store,self.service.folder(self.sid)/'notifications.jsonl',seconds=1.2,poll=.05)
            self.assertIn('CORTEX REMINDER: Review',console.getvalue())
        finally:store.close()
        self.assertEqual(self.service.reminder_list(self.sid)[0]['state'],'delivered')
        wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        self.assertEqual(len(self.service.reminder_list(self.sid)),1)
        self.assertEqual(self.service.reminder_list(self.sid)[0]['delivery_attempts'],1)

    def test_missing_am_pm_not_guessed(self):
        job=wait(self.service,self.sid,self.service.prepare(self.sid,{'mode':'reminder','request':'Remind me','message':'Review','at':'tomorrow at 7','timezone':'UTC','max_calls':0}))
        wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        data=self.service.inspect(self.sid,job)
        self.assertEqual(data['steps']['schedule']['state'],'blocked')
        self.assertEqual(self.service.reminder_list(self.sid),[])

    def test_browser_foreground_scheduler_scans_session_store(self):
        from cortex.web import schedule
        at=(datetime.now(timezone.utc)+timedelta(seconds=.4)).isoformat()
        job=wait(self.service,self.sid,self.service.prepare(self.sid,{'mode':'reminder','request':'Browser scheduler test',
            'message':'Browser-specific notification','at':at,'timezone':'UTC','max_calls':0}))
        wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        with patch('sys.stdout',new_callable=io.StringIO) as console:schedule(self.service.root,1)
        self.assertIn('CORTEX REMINDER: Browser-specific notification',console.getvalue())
        self.assertEqual(self.service.reminder_list(self.sid)[0]['state'],'delivered')

    def test_session_allowance_cannot_reset_by_new_jobs(self):
        public=BrowserService(self.root/'public',MODELS,TOOLS,True); sid,_,_=public.session()
        for _ in range(3):wait(public,sid,public.prepare(sid,self.document(max_calls=12)))
        with self.assertRaises(BrowserError):public.prepare(sid,self.document(max_calls=12))

    def test_busy_session_rejected(self):
        with self.service.mutex:self.service.operation(self.sid)
        with self.assertRaises(BrowserError):self.service.prepare(self.sid,self.document())

    def git_job(self):
        repo=self.root/'repo';remote=self.root/'remote.git'
        for args in (['init','--initial-branch','demo',str(repo)],['init','--bare',str(remote)]):
            subprocess.run(['git',*args],check=True,capture_output=True)
        from cortex.git_tools import git
        git(repo,'config','user.name','Cortex disposable test');git(repo,'config','user.email','test@example.invalid')
        (repo/'description.md').write_text('Selected content\n');(repo/'notes.txt').write_text('Unrelated content\n')
        self.service.tools['git_targets']={'demo':{'path':str(repo),'branch':'demo','remote_url':str(remote),'allow_push':True}}
        payload={'request':'Commit selected document','mode':'git','repository':'demo','branch':'demo','files':['description.md'],
            'commit_message':'Add selected description','max_calls':0}
        job=wait(self.service,self.sid,self.service.prepare(self.sid,payload))
        wait(self.service,self.sid,self.service.resume(self.sid,job,{}))
        return repo,remote,job

    def test_git_pause_review_confirmation_remote_evidence(self):
        repo,remote,job=self.git_job()
        data=self.service.inspect(self.sid,job)
        self.assertEqual(data['steps']['inspect']['state'],'accepted');self.assertEqual(data['steps']['push']['state'],'pending')
        self.assertIn('Selected content',json.dumps(data['steps']['inspect']))
        with self.assertRaises(BrowserError):self.service.resume(self.sid,job,{})
        wait(self.service,self.sid,self.service.resume(self.sid,job,{'confirm_git':True}))
        data=self.service.inspect(self.sid,job)
        self.assertTrue(data['steps']['push']['event']['output']['remote_verified'])
        from cortex.git_tools import git
        self.assertEqual(git(repo,'ls-tree','--name-only','HEAD').stdout.strip(),'description.md')
        self.assertEqual(data['budget']['model_calls'],0)

    def test_changed_git_preview_requires_new_inspection(self):
        repo,remote,job=self.git_job()
        (repo/'description.md').write_text('Changed after review\n')
        with self.assertRaises(BrowserError):self.service.resume(self.sid,job,{'confirm_git':True})
        from cortex.git_tools import head
        self.assertIsNone(head(repo))

    def test_http_auth_origin_csrf_and_download_headers(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.service,'unused'))
        origin='http://127.0.0.1:'+str(server.server_port);server.RequestHandlerClass=handler(self.service,origin)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def send(path,body=None,headers=None):
            return urlopen(Request(origin+path,data=None if body is None else json.dumps(body).encode(),
                headers={'Content-Type':'application/json','Origin':origin,**(headers or {})}),timeout=3)
        try:
            with self.assertRaises(HTTPError):send('/api/meta')
            with send('/api/session',{'owner':self.service.bootstrap}) as response:
                cookie=response.headers['Set-Cookie'];value=json.load(response)
                self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
            h={'Cookie':cookie.split(';')[0],'X-Cortex-CSRF':value['csrf']}
            with self.assertRaises(HTTPError):send('/api/prepare',self.document(),{'Cookie':h['Cookie']})
            with self.assertRaises(HTTPError):send('/api/prepare',self.document(),{**h,'Origin':'https://evil.invalid'})
            with self.assertRaises(HTTPError):send('/api/meta',headers={**h,'Host':'evil.invalid'})
            with send('/api/meta',headers=h) as response:self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
            with send('/') as response:self.assertIn(b'What should Cortex do?',response.read())
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
