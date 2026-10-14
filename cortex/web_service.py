"""Browser service: session-owned durable jobs; no visitor-selected backend paths."""
import copy
import contextlib
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from .draft_checks import DraftPolicy
from .job_store import JobStore
from .jobs import new_job, resume_job
from .registry import Registry
from .tools import ToolRegistry, DEFINITIONS
from . import reminders


class BrowserError(ValueError):
    pass


def step(identifier, kind, goal, caps, depends=(), tool=None):
    return {'id':identifier, 'task_type':kind, 'goal':goal,
        'execution_kind':'tool' if tool else 'text_draft',
        'required_inputs':['user_goal'] + (['predecessor_outputs'] if depends else []),
        'missing_essential_information':[], 'depends_on':list(depends),
        'required_capabilities':caps, 'expected_output':'tool_result' if tool else 'text_draft',
        'completion_checks':['tool_verified'] if tool else ['text_present'], 'tool':tool}


class BrowserService:
    def __init__(self, root, registry, tools, public=False, check_models=False):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        marker=self.root/'access-mode.json'
        if marker.exists() and json.loads(marker.read_text())!={'public':public}:
            raise BrowserError('Use a separate state directory for public and owner access.')
        if not marker.exists():
            with marker.open('x') as output:json.dump({'public':public},output)
        self.registry=copy.deepcopy(registry)
        self.tools=ToolRegistry(tools).config
        self.public=public
        self.check_models=check_models
        self.inventory_checked_at=0
        self.inventory={}
        if public:
            # Public deployments cannot use developer cloud credentials or Git authority.
            self.registry['providers']={k:v for k,v in self.registry['providers'].items() if k=='ollama'}
            self.registry['models']=[r for r in self.registry['models'] if r['provider']=='ollama']
            ids={r['id'] for r in self.registry['models']}
            self.registry['roles']={k:v if v in ids else None for k,v in self.registry['roles'].items()}
            self.tools['git_targets']={}
        Registry(self.registry)
        self.bootstrap=secrets.token_urlsafe(32)
        self.mutex=threading.RLock()
        self.operations={}
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions
                (id TEXT PRIMARY KEY, token TEXT UNIQUE, csrf TEXT, created REAL, calls INTEGER, units INTEGER, jobs INTEGER);
                CREATE TABLE IF NOT EXISTS browser_jobs (id TEXT PRIMARY KEY, session TEXT, mode TEXT);
                CREATE TABLE IF NOT EXISTS totals (id INTEGER PRIMARY KEY, calls INTEGER, jobs INTEGER);
                INSERT OR IGNORE INTO totals VALUES(1,0,0);''')

    @contextlib.contextmanager
    def db(self):
        connection=sqlite3.connect(self.root/'browser.sqlite', timeout=5)
        try:
            with connection:yield connection
        finally:connection.close()

    def session(self, cookie=None, bootstrap=None):
        if self.public and bootstrap is not None:
            raise BrowserError('Owner access is disabled in public mode.')
        with self.mutex, self.db() as db:
            if cookie:
                row=db.execute('SELECT id,csrf,created FROM sessions WHERE token=?',
                    (hashlib.sha256(cookie.encode()).hexdigest(),)).fetchone()
                if row and time.time()-row[2]<86400:
                    return row[0],row[1],None
                raise BrowserError('Session expired or invalid; clear the site cookie and reconnect.')
            if not self.public and (not isinstance(bootstrap,str) or not secrets.compare_digest(bootstrap,self.bootstrap)):
                raise BrowserError('Open the owner URL printed by the server. Sessions expire after 24 hours.')
            if db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]>=256:
                raise BrowserError('Instance session limit reached.')
            token=secrets.token_urlsafe(32); sid=secrets.token_hex(16); csrf=secrets.token_urlsafe(24)
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,0,0,0)',
                (sid,hashlib.sha256(token.encode()).hexdigest(),csrf,time.time()))
            return sid,csrf,token

    def folder(self, sid):
        if not isinstance(sid,str) or len(sid)!=32 or any(c not in '0123456789abcdef' for c in sid):
            raise BrowserError('Invalid session')
        return self.root/'sessions'/sid

    def store(self,sid):
        return JobStore(self.folder(sid)/'jobs.sqlite')

    def tool_config(self,sid,mode):
        root=self.folder(sid)
        return {**self.tools,'output_dir':str(root/'files'), 'notification_log':str(root/'notifications.jsonl'),
            'git_targets':self.tools['git_targets'] if mode=='git' and not self.public else {}}

    def metadata(self):
        registry=Registry(self.registry)
        capabilities=self.capabilities()
        return {'public':self.public,'models':[m.to_dict() for m in registry.models.values()],
            'capabilities':capabilities,
            'roles':registry.roles, 'repositories':{} if self.public else self.tools['git_targets'],
            'prices':'Unknown prices remain unknown; no measured monetary savings.',
            'limits':{'max_units':40,'max_calls':12,'max_active':2,'public_instance_call_allowance':200},
            'scheduler':'Separate foreground web-reminders process required. Delivery channel: console.',
            'recorded_examples':[{'label':'Recorded real-tools evidence; not this interactive execution',
                'examples':['Live Ollama coding draft: 3 calls, reported tokens 1488/259; code not executed.',
                    'Live Markdown: generation + readback; 1 call, 342/81 tokens.',
                    'Mixed: mocked draft, real local tools; Git remote was disposable local, not GitHub.']}]}

    def capabilities(self):
        from .registry import Runtime
        runtime=Runtime(Registry(self.registry))
        if self.check_models:
            if time.monotonic()-self.inventory_checked_at>30 or not self.inventory:
                runtime.probe()  # Bounded metadata calls, never inference or provider retries.
                self.inventory=runtime.availability
                self.inventory_checked_at=time.monotonic()
            runtime.availability=self.inventory
        eligible=[s for s in runtime.registry.models.values() if 'document_draft' in s.capabilities and runtime.available(s)['available']]
        available=bool(eligible)
        reason=('permitted_model_listed' if available and self.check_models else 'configured_not_probed' if available else 'no_available_permitted_model')
        return {'supplier':{'available':True,'reason':'deterministic Python execution'},
            'reminder':{'available':True,'reason':'schedule stored; delivery needs separate foreground console scheduler'},
            'document':{'available':available,'reason':reason,'connectivity_checked':self.check_models},
            'request':{'available':available,'reason':reason,'connectivity_checked':self.check_models},
            'git':{'available':not self.public and bool(self.tools['git_targets']),'reason':'disabled in public mode' if self.public else 'explicit owner targets required'}}

    def validate_request(self,sid,payload):
        allowed={'request','mode','task','source','constraints','max_words','required_sections','max_repairs',
            'max_units','max_calls','classifier_model','planner_model','worker_model','classifier_mode',
            'planning','filename','message','at','timezone','repository','branch','files','commit_message','extraction_mode'}
        if type(payload) is not dict or set(payload)-allowed:
            raise BrowserError('Unsupported request fields; paths, endpoints and credentials are backend configuration only.')
        mode=payload.get('mode','request')
        if mode not in ('request','supplier','document','reminder','git') or self.public and mode=='git':
            raise BrowserError('Unavailable workflow')
        if self.check_models and mode in ('request','document') and not self.capabilities()[mode]['available']:
            raise BrowserError('AI workflow unavailable: configure a reachable permitted model backend. Supplier and reminders can execute without AI.')
        units,calls=payload.get('max_units',20),payload.get('max_calls',6)
        if type(units) is not int or not 1<=units<=40 or type(calls) is not int or not 0<=calls<=12:
            raise BrowserError('Budget bounds: 1–40 synthetic units and 0–12 model calls.')
        request=payload.get('request','')
        if type(request) is not str or not request.strip():raise BrowserError('A request is required.')
        registry=Registry(self.registry)
        for key in ('classifier_model','planner_model','worker_model'):
            if payload.get(key) is not None and payload[key] not in registry.models:raise BrowserError('Unknown role model')
        classifier_mode=payload.get('classifier_mode','auto'); planning=payload.get('planning','auto')
        if classifier_mode not in ('auto','rules','ai') or planning not in ('auto','always'):
            raise BrowserError('Invalid classifier/planner mode')
        if payload.get('extraction_mode','parser') not in ('parser','llm','parser-first'):raise BrowserError('Invalid extraction mode')
        config=self.tool_config(sid,mode)
        policy=DraftPolicy.validate({'max_words':payload.get('max_words'),'required_sections':payload.get('required_sections',[])},
            (payload.get('task') or {}).get('currency'),payload.get('max_repairs',1))
        plan=None
        if mode=='supplier':
            from .classifier import Classification
            from .planning import single_step_plan
            plan=single_step_plan(request,Classification('analysis',('supplier_analysis',),(),False,False),supplier_context=True).to_dict()
            plan['version']=2
            for declaration in plan['steps']:declaration['tool']=None
        elif mode=='document':
            name=payload.get('filename','')
            plan={'version':2,'steps':[step('draft','documents',request,['document_draft']),
                step('save','documents','Save accepted draft',['file_write'],['draft'],{'name':'write_document',
                'arguments':{'filename':name,'content_from':'draft'}})]}
            if payload.get('source') or payload.get('task'):
                plan['steps'][0]['required_inputs'].append('source_material')
            if payload.get('task'):
                plan['steps'][0]['required_inputs'].append('supplier_task')
        elif mode=='reminder':
            args={k:payload.get(k,'') for k in ('message','at','timezone')}
            plan={'version':2,'steps':[step('schedule','scheduling','Schedule supplied reminder',['reminders'],tool={'name':'reminder_create','arguments':args})]}
        elif mode=='git':
            repo=payload.get('repository'); target=config['git_targets'].get(repo)
            if target is None or payload.get('branch')!=target['branch']:raise BrowserError('Choose an explicitly configured repository and its branch.')
            files=payload.get('files',[])
            plan={'version':2,'steps':[step('inspect','analysis','Inspect intended file changes',['analysis','github'],tool={
                'name':'git_inspect','arguments':{'repository':repo,'files':files}}),
                step('push','analysis','Commit only selected files and verify remote',['analysis','github'],['inspect'],{
                    'name':'git_commit_push','arguments':{'repository':repo,'branch':target['branch'],
                        'files':files,'message':payload.get('commit_message','')}})]}
        if plan:
            from .planning import Plan
            Plan.validate(plan,tool_registry=ToolRegistry(config))
        task=payload.get('task')
        if task is not None:
            from .domain import Task
            Task.from_dict(task)
        if mode=='supplier' and task is None:raise BrowserError('Attach a supplier task JSON.')
        from .prompts import validate_context
        validate_context(request,payload.get('constraints',[]),payload.get('source',''))
        return dict(request=request,registry_payload=self.registry,task=task,plan=plan,
            constraints=payload.get('constraints',[]),source=payload.get('source',''),policy=policy,
            max_units=units,max_model_calls=calls,
            options={'request_contract':'isolated','tool_config':config,'classifier_mode':classifier_mode,
                **({'probe':True} if self.check_models else {}),
                'planning':planning,'extraction_mode':payload.get('extraction_mode','parser'),
                **{k:payload[k] for k in ('classifier_model','planner_model','worker_model') if payload.get(k)}}),mode

    def prepare(self,sid,payload):
        args,mode=self.validate_request(sid,payload)
        with self.mutex, self.db() as db:
            self.check_active(sid)
            used=db.execute('SELECT calls,units,jobs FROM sessions WHERE id=?',(sid,)).fetchone()
            global_used=db.execute('SELECT calls,jobs FROM totals WHERE id=1').fetchone()
            if (used[0]+args['max_model_calls']>(40 if self.public else 1000)
                or used[1]+args['max_units']>(160 if self.public else 2000) or used[2]>=20
                or self.public and (global_used[0]+args['max_model_calls']>200 or global_used[1]>=100)):
                raise BrowserError('Session/instance reservation allowance exhausted. Limits never silently reset on resume.')
            # Reserve the full job allowance, not a claim that all calls were consumed.
            db.execute('UPDATE sessions SET calls=calls+?,units=units+?,jobs=jobs+1 WHERE id=?',
                (args['max_model_calls'],args['max_units'],sid))
            db.execute('UPDATE totals SET calls=calls+?,jobs=jobs+1 WHERE id=1',(args['max_model_calls'],))
            op=self.operation(sid)
        def created(identifier):
            op['job_id']=identifier
            with self.db() as db:db.execute('INSERT INTO browser_jobs VALUES(?,?,?)',(identifier,sid,mode))
        self.launch(op,lambda store:new_job(store,**args,prepare_only=True,on_created=created))
        return {'operation_id':op['id'],'allowance_reserved':{'calls':args['max_model_calls'],'units':args['max_units']}}

    def check_active(self,sid):
        if sum(op['state']=='running' for op in self.operations.values())>=2:
            raise BrowserError('Two executions are active; wait for completion.')
        if any(op['session']==sid and op['state']=='running' for op in self.operations.values()):
            raise BrowserError('This session already has an active operation.')

    def operation(self,sid):
        op={'id':secrets.token_hex(16),'session':sid,'state':'running','job_id':None,'error':None}
        self.operations[op['id']]=op
        return op

    def launch(self,op,action):
        def run():
            store=None
            try:
                store=self.store(op['session']); action(store); op['state']='finished'
            except Exception as exc:
                # Do not return raw provider/server exception strings to public clients.
                op['state']='failed'; op['error']=('Execution failed ('+type(exc).__name__+'); inspect saved job evidence.' if self.public else str(exc))
            finally:
                if store:store.close()
        threading.Thread(target=run,daemon=True).start()

    def owned(self,sid,identifier):
        with self.db() as db:row=db.execute('SELECT mode FROM browser_jobs WHERE id=? AND session=?',(identifier,sid)).fetchone()
        if not row:raise BrowserError('Job not found in this session')
        return row[0]

    def resume(self,sid,identifier,payload):
        if type(payload) is not dict or set(payload)-{'retry_unknown','confirm_git'} or any(type(v) is not bool for v in payload.values()):
            raise BrowserError('Invalid resume options')
        mode=self.owned(sid,identifier)
        pause=None
        if mode=='git':
            store=self.store(sid)
            try:data=store.read(identifier)
            finally:store.close()
            if data['steps'].get('inspect',{}).get('state')!='accepted':pause=1
            elif not payload.get('confirm_git'):raise BrowserError('Review the inspection and explicitly authorize commit/push.')
            elif not any(op['tool_id']=='git_commit_push' for op in data.get('tool_operations',[])):
                # Bind authorization to the reviewed bytes and repository head, not just filenames.
                from .git_tools import safe_files, head
                reviewed=data['steps']['inspect']['event']['output']
                target=data['options']['tool_config']['git_targets']
                args=next(s['tool']['arguments'] for s in data['plan']['steps'] if s.get('tool',{} ) and s['tool']['name']=='git_commit_push')
                actual=safe_files(target[args['repository']]['path'],args['files'])
                if (head(target[args['repository']]['path'])!=reviewed['head'] or
                    [(r['path'],r['sha256'],r['git_blob']) for r in actual]!=
                    [(r['path'],r['sha256'],r['git_blob']) for r in reviewed['intended_changes']]):
                    raise BrowserError('Repository or selected contents changed since inspection; create a fresh preview.')
        with self.mutex:
            self.check_active(sid); op=self.operation(sid); op['job_id']=identifier
        self.launch(op,lambda store:resume_job(store,identifier,retry_interrupted=payload.get('retry_unknown',False),
            pause_after=pause,expected_registry=self.registry))
        return {'operation_id':op['id']}

    def operation_view(self,sid,identifier):
        op=self.operations.get(identifier)
        if not op or op['session']!=sid:raise BrowserError('Operation not found')
        return {k:v for k,v in op.items() if k!='session'}

    def inspect(self,sid,identifier):
        self.owned(sid,identifier); store=self.store(sid)
        try:
            data=store.read(identifier)
            result={k:data.get(k) for k in ('job_id','state','request','classification','plan','steps','budget','trace',
                'model_usage','result','tool_operations','created_at_utc','draft_policy')}
            result['transitions']=store.transitions(identifier)
            result['reminders']=reminders.inspect(store)
        finally:store.close()
        # Keep private bindings/configuration/credentials out of browser serialization.
        if self.public:
            encoded=json.dumps(result).replace(str(self.root).replace('\\','\\\\'),'[session storage]')
            result=json.loads(encoded)
        return result

    def listing(self,sid):
        with self.db() as db:ids=[r[0] for r in db.execute('SELECT id FROM browser_jobs WHERE session=? ORDER BY rowid DESC',(sid,))]
        return [{k:d[k] for k in ('job_id','state','request','budget','created_at_utc')}
            for d in (self.inspect(sid,i) for i in ids)]

    def download(self,sid,identifier,task_id):
        self.owned(sid,identifier);store=self.store(sid)
        try:data=store.read(identifier)
        finally:store.close()
        if data['steps'].get(task_id,{}).get('state')!='accepted':raise BrowserError('File step is not accepted')
        operations=[op for op in data.get('tool_operations',[]) if op['task_id']==task_id and op['tool_id']=='write_document' and op['state']=='completed']
        if len(operations)!=1:raise BrowserError('Verified document not found')
        value=operations[0]['output']; path=Path(value['path'])
        if path.is_symlink() or path.resolve().parent!=(self.folder(sid)/'files').resolve():raise BrowserError('Unsafe file path')
        content=path.read_bytes()
        if len(content)!=value['size'] or hashlib.sha256(content).hexdigest()!=value['sha256']:raise BrowserError('File changed after verification')
        return path.name,content

    def reminder_list(self,sid):
        store=self.store(sid)
        try:return reminders.inspect(store)
        finally:store.close()

    def reminder_cancel(self,sid,identifier):
        store=self.store(sid)
        try:return reminders.cancel(store,identifier)
        finally:store.close()
